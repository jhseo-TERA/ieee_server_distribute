"""Cache source evidence by PDF AND worker hashes in an isolated runtime."""
from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import subprocess
from functools import lru_cache

from paper_server.paths import ROOT
from paper_server.parsers.reference_segmentation import segment_references, entry_quality
from paper_server.parsers.reference_ocr import with_ocr_fallback, EXTRACTOR as OCR_EXTRACTOR


def pdf_evidence(pdf, source_sha256, cache_root, *, ocr_fallback=True):
    worker = ROOT / 'scripts/reference_evidence_worker.py'
    request = {'pdf': str(Path(pdf).resolve()), 'source_sha256': source_sha256,
               'worker_sha256': hashlib.sha256(worker.read_bytes()).hexdigest()}
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    folder = Path(cache_root) / key
    folder.mkdir(parents=True, exist_ok=True)
    request_path, answer = folder / 'request.json', folder / 'evidence.json'
    if hashlib.sha256(Path(pdf).read_bytes()).hexdigest() != source_sha256:
        raise ValueError('Source PDF changed')
    request_path.write_text(json.dumps(request), encoding='utf-8')
    if not answer.exists():
        with (folder/'worker.log').open('w',encoding='utf-8') as log:
            subprocess.run([str(ROOT/'.runtime/document-parsers/Scripts/python.exe'),str(worker),
                '--request',str(request_path),'--output',str(answer)], cwd=ROOT,
                env=dict(os.environ,PYTHONUTF8='1'),stdout=log,stderr=log,timeout=90,check=True)
    data = json.loads(answer.read_text(encoding='utf-8'))
    if data.get('source_sha256') != source_sha256:
        raise ValueError('PDF evidence hash mismatch')
    data['evidence_path'] = str(answer)
    data['evidence_sha256'] = hashlib.sha256(answer.read_bytes()).hexdigest()
    return with_ocr_fallback(pdf, data, Path(cache_root).parent / 'reference-ocr-cache') if ocr_fallback else data


def bounded_evidence(data):
    result = segment_references([(p['page'],p['text']) for p in data['pages']])
    ocr = data.get('ocr', {})
    use_ocr = data['extractor'] == OCR_EXTRACTOR
    if use_ocr:
        native = segment_references([(p['page'],p.get('native_text',p['text'])) for p in data['pages']])
        if len(native['entries']) > len(result['entries']):
            result = native
            result['warnings'].append('ocr_bibliography_boundary_regression')
            use_ocr = False
    for entry in result['entries']:
        entry['extractor'] = OCR_EXTRACTOR if use_ocr else 'pypdfium2'
        if use_ocr:
            entry['ocr_required_review'] = True
    result['ocr'] = ocr
    if ocr.get('status') in ('failed', 'unavailable'):
        result['warnings'].append('ocr_' + ocr['status'])
    if ocr.get('budget_exceeded'):
        result['warnings'].append('ocr_page_budget_exceeded')
    return result


def align_sources(entries, bounded):
    """An ordinal alone is insufficient to move metadata between citations."""
    source = {e['number']:e for e in bounded['entries']}
    aligned = {}
    for entry in entries:
        candidate = source.get(entry['number'])
        if not candidate:
            continue
        original_defects = set(entry_quality([entry])) - {'noncontiguous_labels'}
        candidate_defects = set(entry_quality([candidate])) - {'noncontiguous_labels'}
        if candidate_defects and not original_defects:
            continue
        if candidate.get('ocr_required_review') and not entry.get('ocr_required_review'):
            # OCR on a neighboring entry or an image caption cannot override
            # this readable native/structured citation, including its year.
            if entry.get('native_text_preferred') or not original_defects:
                continue
        a, b = entry['raw'].casefold(), candidate['raw'].casefold()
        prefix_agrees = min(len(a),len(b)) >= 100 and SequenceMatcher(None,a[:180],b[:180],autojunk=False).ratio() >= .94
        if SequenceMatcher(None,a,b,autojunk=False).ratio() >= .82 or prefix_agrees:
            aligned[entry['number']] = candidate
    return aligned


@lru_cache(maxsize=256)
def _verified_payload(path, digest, source_sha256, mtime_ns, size):
    content=Path(path).read_bytes()
    if hashlib.sha256(content).hexdigest()!=digest:
        raise ValueError('Source evidence file changed')
    data=json.loads(content)
    if data.get('source_sha256')!=source_sha256 or data.get('extractor') not in ('pypdfium2', OCR_EXTRACTOR):
        raise ValueError('Source evidence identity rejected')
    return bounded_evidence(data)


def authenticated_sources(rows, source_sha256):
    """Reconstruct, align, and authenticate the views retained on final rows."""
    aligned={}
    for row in rows:
        view=row.get('source_view',{})
        if view.get('extractor')=='original_reference':
            if view.get('raw')!=row['raw']:
                raise ValueError('Original source view changed')
            continue
        proof=row.get('source_evidence',{})
        if proof.get('pdf_sha256')!=source_sha256:
            raise ValueError('PDF source identity differs')
        path=Path(proof['path']);stat=path.stat()
        bounded=_verified_payload(str(path),proof['sha256'],source_sha256,stat.st_mtime_ns,stat.st_size)
        found=align_sources([row],bounded).get(row['number'])
        if not found or view.get('raw')!=found['raw'] or view.get('page')!=found['page']:
            raise ValueError('Unaligned source view')
        if hashlib.sha256(view['raw'].encode()).hexdigest()!=view.get('sha256'):
            raise ValueError('Source view text hash differs')
        aligned[row['number']]=found
    return aligned
