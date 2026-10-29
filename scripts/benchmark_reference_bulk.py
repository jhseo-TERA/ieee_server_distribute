"""Resumable, held-out PDF reference experiment; no live SQL/graph writes.

Select first, then run --prepare and --enrich --follow concurrently if desired.
Only loopback Ollama and a read-only check of active server AI jobs use sockets.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.ai.runtime import OllamaClient
from paper_server.citation_fallback import reference_entry
from paper_server.parsers.references import (
    FIELDS, VERSION, consensus, digest_file, grounded, grobid_raw,
    llm_fields, llm_schema, mineru_references, pdf_pages, split_numbered,
)
from paper_server.serdes.mineru import _settings, mineru_environment
from paper_server.parsers.reference_normalization import VERSION as NORMALIZATION_VERSION, apply_title_normalization
from paper_server.parsers.reference_title_recovery import recover_titles
from paper_server.parsers.reference_segmentation import choose_entries, segment_references, entry_quality
from paper_server.parsers.reference_evidence import pdf_evidence, bounded_evidence
from paper_server.parsers.reference_pipeline import reconcile_rows
from paper_server.parsers.reference_urls import write_url_log
from paper_server.parsers.reference_ocr import ocr_settings
from paper_server.parsers.reference_authority import (
    VERSION as AUTHOR_RESOLUTION_VERSION, REGISTRY as AUTHOR_REGISTRY, apply_author_resolution, load_authorities,
)

MODEL = 'qwen3.6:35b-a3b-mtp-q4_K_M'
BULK_VERSION = 'reference-bulk-v1'
DEFAULT_OUT = ROOT / 'outputs/reference_bulk_500_20261017'
PUBLISHERS = ('ieee-pdf', 'optica-pdf', 'nature-pdf')


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.part')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def event(**data):
    print(json.dumps({'at': now(), **data}, ensure_ascii=True), flush=True)


@contextmanager
def stage_lock(out, name):
    import msvcrt
    out.mkdir(parents=True, exist_ok=True)
    with (out / (name + '.lock')).open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def allocate(counts, limit):
    total = sum(counts.values())
    if limit > total or limit < 1:
        raise ValueError('Not enough distinct PDF files for the requested sample')
    exact = {name: limit * n / total for name, n in counts.items()}
    result = {name: math.floor(n) for name, n in exact.items()}
    for name in sorted(counts, key=lambda n: (-(exact[n] - result[n]), n))[:limit - sum(result.values())]:
        result[name] += 1
    return result


def select(out, limit, seed):
    manifest_path = out / 'manifest.json'
    if manifest_path.exists():
        existing = read(manifest_path)
        if existing['requested'] != limit or existing['seed'] != seed:
            raise ValueError('Existing immutable manifest has different selection settings')
        event(selection='reused', count=len(existing['papers']))
        return
    pilot = read(ROOT / 'config/reference_benchmark.json')['papers']
    excluded = {digest_file(ROOT / row['pdf']) for row in pilot}
    groups, seen, duplicates, errors = defaultdict(list), set(), [], []
    for publisher in PUBLISHERS:
        for path in sorted((ROOT / publisher).rglob('*.pdf')):
            try:
                sha = digest_file(path)
                relative = path.relative_to(ROOT).as_posix()
                if sha in seen:
                    duplicates.append(relative)
                    continue
                seen.add(sha)
                if sha not in excluded:
                    groups[publisher].append({'pdf': relative, 'sha256': sha, 'publisher': publisher,
                                              'bytes': path.stat().st_size})
            except OSError as exc:
                errors.append({'path': str(path.relative_to(ROOT)), 'error': type(exc).__name__})
    counts = {name: len(rows) for name, rows in groups.items()}
    quotas = allocate(counts, limit)
    selected = []
    rank = lambda row: hashlib.sha256((seed + row['sha256']).encode()).hexdigest()
    for name, rows in groups.items():
        selected.extend(sorted(rows, key=rank)[:quotas[name]])
    selected.sort(key=rank)
    for row in selected:
        row['id'] = row['sha256'][:20]
    save(manifest_path, {'version': BULK_VERSION, 'created_at': now(), 'seed': seed, 'requested': limit,
        'selection': 'SHA256 deduplicated; publisher-proportional deterministic hash ranking; pilot excluded',
        'eligible_by_publisher': counts, 'selected_by_publisher': quotas, 'duplicate_paths': duplicates,
        'inventory_errors': errors, 'pilot_excluded_hashes': sorted(excluded), 'papers': selected})
    event(selection='created', count=len(selected), publishers=quotas, duplicates=len(duplicates))


def scan_pages(pages):
    """Locate the reference tail, preserving all subsequent original pages.

    If a heading is unavailable, scan the whole document (at most 300 pages).
    A boundary is never inferred from the last N pages alone.
    """
    headings = []
    for number, text in pages:
        if re.search(r'(?im)^\s*(?:(?:[IVX]+|\d+)\.?\s+)?(?:REFERENCES|BIBLIOGRAPHY)\s*$', text):
            headings.append(number)
    start = headings[0] if headings else 1
    return {'page_count': len(pages), 'start': start, 'end': len(pages),
            'heading_found': bool(headings), 'heading_pages': headings,
            'text_characters': sum(len(text) for _, text in pages)}


class MinerWorker:
    def __init__(self, out):
        env = mineru_environment()
        env.update({'OMP_NUM_THREADS': '4', 'MKL_NUM_THREADS': '4', 'OPENBLAS_NUM_THREADS': '4'})
        self.log = (out / 'mineru-worker.log').open('a', encoding='utf-8')
        self.process = subprocess.Popen([str(ROOT / '.runtime/document-parsers/Scripts/python.exe'),
            str(ROOT / 'scripts/mineru_reference_worker.py')], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding='utf-8', bufsize=1)
        self.answers = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            if line.startswith('@@REFERENCE_RESULT@@'):
                self.answers.put(json.loads(line[len('@@REFERENCE_RESULT@@'):]))
            else:
                self.log.write(line)
                self.log.flush()
        self.answers.put({'error': 'WorkerExited', 'ok': False})

    def run(self, task):
        self.process.stdin.write(json.dumps(task) + '\n')
        self.process.stdin.flush()
        try:
            answer = self.answers.get(timeout=600)
        except queue.Empty:
            self.close()
            raise TimeoutError('MinerU PDF task exceeded 600 seconds')
        if answer.get('id') != task['id'] or not answer['ok']:
            raise RuntimeError('MinerU worker failed: ' + answer.get('error', 'protocol mismatch'))
        return answer

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.log.close()


def prepare(out):
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    manifest = read(out / 'manifest.json')
    signature = {'version': BULK_VERSION, 'parser_version': VERSION,
        'preparation_code': digest_file(Path(__file__)),
        'references_code': digest_file(ROOT / 'paper_server/parsers/references.py'),
        'boundary_code': digest_file(ROOT / 'paper_server/parsers/reference_segmentation.py'),
        'role_url_code': digest_file(ROOT / 'paper_server/parsers/reference_urls.py'),
        'evidence_worker': digest_file(ROOT / 'scripts/reference_evidence_worker.py'),
        'ocr_policy_code': digest_file(ROOT / 'paper_server/parsers/reference_ocr.py'),
        'ocr_worker_code': digest_file(ROOT / 'scripts/reference_ocr_worker.py'),
        'ocr_engine_code': digest_file(ROOT / 'scripts/reference_ocr.cjs'),
        'worker_code': digest_file(ROOT / 'scripts/mineru_reference_worker.py'),
        'mineru': _settings('basic', 300, 1)}
    signature['mineru'].pop('start_page')
    signature['mineru'].pop('max_pages')
    try:
        signature['ocr_runtime'] = ocr_settings()
    except (OSError, ValueError, KeyError) as exc:
        signature['ocr_runtime'] = {'status': 'unavailable', 'error': type(exc).__name__}
    worker, started = None, time.monotonic()
    save(out / 'prepare-status.json', {'status': 'running', 'started_at': now(), 'pid': os.getpid()})
    try:
        for index, paper in enumerate(manifest['papers'], 1):
            target = out / 'papers' / paper['id']
            saved = target / 'prepared.json'
            if saved.exists() and read(saved).get('signature') == signature:
                continue
            result = {'id': paper['id'], 'source': paper, 'signature': signature, 'started_at': now(),
                      'entries': [], 'warnings': [], 'errors': []}
            task_start = time.monotonic()
            try:
                pdf = ROOT / paper['pdf']
                if digest_file(pdf) != paper['sha256']:
                    raise ValueError('PDF changed since manifest selection')
                if paper['bytes'] > 64 * 1024 * 1024:
                    raise ValueError('PDF exceeds experiment 64 MiB bound')
                pages = pdf_pages(pdf)
                scan = scan_pages(pages)
                result['scan'] = scan
                if not 1 <= scan['page_count'] <= 300:
                    raise ValueError('PDF page count outside experiment bounds')
                native_boundaries = segment_references(pages)
                baseline = native_boundaries['entries']
                result['baseline_entries'] = baseline
                evidence = pdf_evidence(pdf,paper['sha256'],ROOT/'.runtime/reference-evidence-cache')
                bounded = bounded_evidence(evidence)
                entries,boundary = choose_entries(baseline,bounded)
                boundary['warnings'] = sorted(set(boundary['warnings'] + [w for w in native_boundaries['warnings']
                    if w in ('possible_omitted_references','unresolved_additional_references')]))
                result['ocr'] = evidence.get('ocr', {})
                needs_layout = not entries or bool(set(boundary['warnings']) & {
                    'possible_omitted_references', 'damaged_reference_text',
                    'non_bibliographic_tail', 'collapsed_reference_labels', 'source_at_truncation_bound',
                    'uncertain_scientific_notation','unresolved_additional_references'}) or bool(entry_quality(entries))
                result['mineru_skipped'] = not needs_layout
                result['entry_source'] = 'native_text' if entries else 'none'
                if needs_layout:
                    native_entries, native_boundary = entries, boundary
                    output = target / 'mineru'
                    # PDFium may locate a bibliography when pypdf cannot read
                    # its font. Use those actual page bounds, with neighbors.
                    known_pages = [e['page'] for e in entries]
                    layout_start = max(1,min(known_pages)-1) if known_pages else scan['start']
                    layout_end = min(scan['page_count'],max(known_pages)+1) if known_pages else scan['end']
                    if set(boundary['warnings']) & {'possible_omitted_references','unresolved_additional_references'}:
                        layout_end = scan['page_count']
                    result['layout_window'] = {'start':layout_start,'end':layout_end,
                        'source':'bounded_reference_pages' if known_pages else 'native_heading_or_full_pdf'}
                    provenance = {'source_sha256': paper['sha256'], 'start': layout_start, 'end': layout_end,
                        'signature': {'worker_code':signature['worker_code'],'mineru':signature['mineru']}}
                    metadata = output / 'provenance.json'
                    stored = read(metadata) if metadata.exists() else {}
                    cached = ((output / 'structured_content.json').exists() and stored.get('source_sha256')==paper['sha256']
                        and stored.get('signature')==provenance['signature']
                        and stored.get('start',10001)<=layout_start and stored.get('end',0)>=layout_end)
                    if not cached:
                        if worker is None:
                            worker = MinerWorker(out)
                        answer = worker.run({'id': paper['id'], 'pdf': str(pdf), 'output': str(output),
                            'start': layout_start, 'end': layout_end})
                        result['mineru_seconds'] = answer['seconds']
                        save(metadata, provenance)
                    result['mineru_cache_hit'] = cached
                    result['mineru_cache_provenance'] = stored if cached else provenance
                    try:
                        layout_payload = read(output / 'structured_content.json')
                        layout_payload = {**layout_payload,'pages':[p for p in layout_payload.get('pages',[])
                            if layout_start <= p['page_idx']+1 <= layout_end]}
                        layout_entries = mineru_references(layout_payload, recover_adjacent=True)
                    except ValueError:
                        layout_entries = []
                        result['warnings'].append('mineru_noncontiguous_labels')
                    # A layout parser may resolve a premature flat-text stop.
                    # It must retain the valid prefix and extend it, rather than
                    # replace a longer native bibliography with a shorter one.
                    hard_defects = set(entry_quality(layout_entries)) - {'damaged_reference_text','uncertain_scientific_notation'}
                    if layout_entries and len(layout_entries) >= len(native_entries) and not hard_defects:
                        entries, boundary = choose_entries(layout_entries,bounded)
                        result['entry_source'] = 'mineru'
                        boundary['warnings'] = sorted(set(boundary['warnings'] + entry_quality(entries)))
                        if len(entries) > len(native_entries):
                            boundary['warnings'] = [w for w in boundary['warnings'] if w != 'possible_omitted_references']
                            boundary['resolved_warnings'] = sorted(set(boundary.get('resolved_warnings', [])
                                + ['possible_omitted_references']))
                    else:
                        entries, boundary = native_entries, native_boundary
                        boundary['warnings'] = sorted(set(boundary['warnings'] + ['layout_fallback_unresolved']))
                result['boundary_resolution'] = boundary
                result['pdf_evidence'] = {'path':evidence['evidence_path'],'sha256':evidence['evidence_sha256']}
                result['warnings'].extend(boundary['warnings'])
                if boundary['source']=='bounded_pdf_text':
                    result['entry_source']='bounded_pdf_ocr' if evidence.get('ocr',{}).get('status')=='complete' else 'bounded_pdf_text'
                if len(baseline) > len(entries):
                    result['warnings'].append('mineru_fewer_than_pypdf')
                result['entries'] = entries
                result['status'] = 'ready' if entries else 'extraction_unresolved'
                if not entries:
                    result['warnings'].append('no_supported_contiguous_numbered_references')
                if len(entries) > 500:
                    raise ValueError('Reference count exceeds experiment 500 per PDF bound')
            except Exception as exc:
                result['status'] = 'error'
                result['errors'].append({'type': type(exc).__name__, 'message': str(exc)[:300]})
                if worker is not None:
                    worker.close()
                    worker = None
            result['elapsed_seconds'] = round(time.monotonic() - task_start, 3)
            result['finished_at'] = now()
            save(saved, result)
            event(stage='prepare', done=index, total=len(manifest['papers']), paper=paper['pdf'],
                  status=result['status'], references=len(result['entries']), seconds=result['elapsed_seconds'])
            save(out / 'prepare-progress.json', {'done': index, 'total': len(manifest['papers']), 'last': paper['id'], 'at': now()})
    finally:
        if worker is not None:
            worker.close()
    save(out / 'prepare-status.json', {'status': 'complete', 'finished_at': now(),
                                     'elapsed_seconds': round(time.monotonic() - started, 3)})


def batches(entries, max_items=5, max_characters=6000):
    current, size = [], 0
    for entry in entries:
        length = len(entry['raw'])
        if current and (len(current) >= max_items or size + length > max_characters):
            yield current
            current, size = [], 0
        current.append(entry)
        size += length
    if current:
        yield current


class IdleGuard:
    """Read-only production job check; never import the web app or change jobs."""
    def __init__(self):
        from dotenv import dotenv_values
        from sqlalchemy import create_engine
        from sqlalchemy.engine import URL
        v = dotenv_values(ROOT / '.env')
        self.engine = create_engine(URL.create('mysql+pymysql', username=v.get('DB_USER', 'root'),
            password=v.get('DB_PASSWORD', ''), host=v.get('DB_HOST', '127.0.0.1'),
            port=int(v.get('DB_PORT', 3306)), database=v.get('DB_NAME', 'ieee_repo')),
            connect_args={'connect_timeout': 5, 'read_timeout': 10}, pool_pre_ping=True)

    def wait(self):
        from sqlalchemy import text
        reported = False
        while True:
            with self.engine.connect() as connection:
                count = connection.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued','running')")).scalar()
            if not count:
                return
            if not reported:
                event(stage='enrich', status='waiting_for_server_ai_jobs', active=count)
                reported = True
            time.sleep(10)


def extraction_cache_signature(signature):
    return {k: v for k, v in signature.items() if not k.startswith(('normalization_', 'author_resolution_', 'role_', 'source_review_'))}


def llm_cache_signature(signature):
    return {k: v for k, v in extraction_cache_signature(signature).items()
            if k not in ('grobid_adapter','version','parser_version','references_code','boundary_code','evidence_worker')}


def llm_input(batch):
    return json.dumps([{'number':r['number'],'raw':r['raw']} for r in batch],sort_keys=True,ensure_ascii=False)


def llm_batch(client, guard, batch, out, signature):
    # GROBID's adapter cannot affect the model request or source grounding.
    # Keep its version in final-result provenance, independently of LLM caches.
    signature = llm_cache_signature(signature)
    input_text = llm_input(batch)
    sha = hashlib.sha256((input_text + json.dumps(signature, sort_keys=True)).encode()).hexdigest()
    path = out / 'llm' / (sha + '.json')
    if path.exists():
        previous = read(path)
        if previous['signature'] == signature and previous['input'] == input_text:
            inputs={entry['number']:entry for entry in batch}
            numbers=[row['number'] for row in previous['rows']]
            if len(numbers)!=len(set(numbers)) or any(number not in inputs for number in numbers):
                raise ValueError('Cached model reference identities differ')
            rebound=[]
            for row in previous['rows']:
                checked=grounded(inputs[row['number']],row,'local-llm-cache')
                checked['warnings']=list(dict.fromkeys(checked['warnings']+row.get('warnings',[])))
                rebound.append(checked)
            return rebound, previous['timings'], previous.get('errors', [])
    guard.wait()
    try:
        rows, timing = llm_fields(client, MODEL, batch)
        timings, errors = [timing], []
    except (ValueError, RuntimeError) as exc:
        if len(batch) > 1:
            middle = len(batch) // 2
            a, at, ae = llm_batch(client, guard, batch[:middle], out, signature)
            b, bt, be = llm_batch(client, guard, batch[middle:], out, signature)
            rows, timings, errors = a + b, at + bt, ae + be
            errors = [{'type': type(exc).__name__, 'recovered_by_split': not errors, 'numbers': [e['number'] for e in batch]}] + errors
        else:
            rows, timings = [], []
            errors = [{'type': type(exc).__name__, 'recovered_by_split': False, 'numbers': [batch[0]['number']]}]
    save(path, {'signature': signature, 'input': input_text, 'rows': rows, 'timings': timings, 'errors': errors})
    return rows, timings, errors


def migrate_llm_caches(out, signature):
    """Reuse exact model inputs when only the upstream GROBID adapter changed."""
    desired = llm_cache_signature(signature)
    count = 0
    for path in list((out / 'llm').glob('*.json')):
        value = read(path)
        old = value.get('signature', {})
        if llm_cache_signature(old) != desired:
            continue
        source_input = llm_input(json.loads(value['input']))
        if old==desired and source_input==value['input']:
            continue
        sha = hashlib.sha256((source_input + json.dumps(desired, sort_keys=True)).encode()).hexdigest()
        target = out / 'llm' / (sha + '.json')
        if not target.exists():
            save(target, {**value, 'signature': desired, 'input':source_input,'reused_from': path.name})
            count += 1
    event(stage='llm_cache', reused_exact_inputs=count)


def enrich_group(papers, out, client, guard, signature):
    authorities = load_authorities()
    prepared = [read(out / 'papers' / paper['id'] / 'prepared.json') for paper in papers]
    flat = [entry for item in prepared if item['status'] == 'ready' for entry in item['entries']]
    grobid_rows = []
    grobid_error = None
    elapsed = 0
    if flat:
        # A single JVM handles this group, avoiding model/class loading per PDF.
        group_sha = hashlib.sha256(json.dumps(flat, sort_keys=True).encode()).hexdigest()
        group = out / 'grobid' / group_sha
        cache = group / 'parsed.json'
        grobid_signature = extraction_cache_signature(signature)
        if cache.exists() and extraction_cache_signature(read(cache).get('signature', {})) == grobid_signature:
            stored = read(cache)
            grobid_rows, elapsed = stored['rows'], stored['seconds']
        else:
            try:
                grobid_rows, elapsed = grobid_raw(flat, group)
                save(cache, {'rows': grobid_rows, 'seconds': elapsed, 'signature': grobid_signature})
            except Exception as exc:
                grobid_error = type(exc).__name__
                grobid_rows = [grounded(e, {}, 'grobid-unavailable') for e in flat]
    cursor = 0
    for paper, item in zip(papers, prepared):
        target = out / 'papers' / paper['id'] / 'result.json'
        result = {'id': paper['id'], 'source': paper, 'signature': signature, 'prepared_status': item['status'],
            'entry_source': item.get('entry_source'), 'scan': item.get('scan'), 'warnings': item['warnings'],
            'ocr': item.get('ocr', {}),
            'errors': list(item['errors']), 'finished_at': now(), 'rows': [], 'llm_timings': [], 'llm_errors': [],
            'grobid_group_seconds': elapsed, 'routed': 0, 'llm_completed': 0}
        if item['status'] == 'ready':
            entries = item['entries']
            grobid = grobid_rows[cursor:cursor + len(entries)]
            cursor += len(entries)
            regex = [grounded(e, reference_entry(e['raw']), 'regex') for e in entries]
            bases = [consensus(e, r, g) for e, r, g in zip(entries, regex, grobid)]
            selected = [row for row in bases if row['needs_llm']]
            answers = {}
            for batch in batches(selected):
                rows, timings, errors = llm_batch(client, guard, batch, out, signature)
                answers.update({row['number']: row for row in rows})
                result['llm_timings'].extend(timings)
                result['llm_errors'].extend(errors)
            result['routed'], result['llm_completed'] = len(selected), len(answers)
            result['regex'], result['grobid'], result['before_llm'] = regex, grobid, bases
            result['rows'] = [dict(consensus(e, r, g, answers.get(e['number'])),
                llm_status=('completed' if e['number'] in answers else 'failed' if b['needs_llm'] else 'not_needed'))
                for e, r, g, b in zip(entries, regex, grobid, bases)]
            evidence = None
            if item.get('pdf_evidence'):
                evidence=pdf_evidence(ROOT/paper['pdf'],paper['sha256'],ROOT/'.runtime/reference-evidence-cache')
            result['rows'] = reconcile_rows(result['rows'],bounded=bounded_evidence(evidence) if evidence else None,
                evidence=evidence,source_sha256=paper.get('sha256'))
            if any(row['normalization_needs_review'] for row in result['rows']):
                result['rows'], result['title_recovery'] = recover_titles(
                    result['rows'], ROOT / paper['pdf'], paper['sha256'], out / 'title-pdf-evidence')
            result['rows'] = [apply_author_resolution(row, authorities) for row in result['rows']]
            result['status'] = 'complete' if len(answers) == len(selected) and not grobid_error else 'needs_review'
            if any(row['normalization_needs_review'] for row in result['rows']):
                result['status'] = 'needs_review'
            if item.get('boundary_resolution',{}).get('warnings') or any(row.get('needs_review') for row in result['rows']):
                result['status'] = 'needs_review'
            if grobid_error:
                result['errors'].append({'stage': 'grobid', 'type': grobid_error})
        else:
            result['status'] = item['status']
        result['finished_at'] = now()
        write_url_log(result, target.parent)
        save(target, result)
        event(stage='enrich', paper=paper['pdf'], status=result['status'], references=len(result['rows']),
              routed=result['routed'], llm_completed=result['llm_completed'],
              llm_seconds=round(sum(t['elapsed_seconds'] for t in result['llm_timings']), 2))


def enrich(out, follow):
    manifest = read(out / 'manifest.json')
    client, guard = OllamaClient(deadline_seconds=240), IdleGuard()
    status = client.status()
    digest = next((m['digest'] for m in status['models'] if m['name'] == MODEL), None)
    if not status['available'] or not digest:
        raise ValueError('Required local Ollama model unavailable')
    prompt = inspect.getsource(llm_fields) + inspect.getsource(llm_schema)
    signature = {'version': BULK_VERSION, 'parser_version': VERSION, 'model': MODEL, 'model_digest': digest,
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), 'num_ctx': 8192, 'max_tokens': 2048,
        'temperature': .1, 'thinking': False, 'keep_alive_seconds': 120, 'max_batch_items': 5,
        'max_batch_source_characters': 6000, 'references_code': digest_file(ROOT / 'paper_server/parsers/references.py'),
        'grobid_adapter': digest_file(ROOT / 'scripts/GrobidReferenceBatch.java'), 'consolidation': 0,
        'normalization_version': NORMALIZATION_VERSION,
        'role_code': digest_file(ROOT/'paper_server/parsers/reference_roles.py'),
        'role_identifier_code': digest_file(ROOT/'paper_server/parsers/reference_identifiers.py'),
        'role_scope_code': digest_file(ROOT/'paper_server/parsers/reference_scope.py'),
        'role_url_code': digest_file(ROOT/'paper_server/parsers/reference_urls.py'),
        'role_pipeline_code': digest_file(ROOT/'paper_server/parsers/reference_pipeline.py'),
        'role_evidence_code': digest_file(ROOT/'paper_server/parsers/reference_evidence.py'),
        'role_display_code': digest_file(ROOT/'paper_server/parsers/reference_display.py'),
        'role_ocr_code': digest_file(ROOT/'paper_server/parsers/reference_ocr.py'),
        'source_review_code': digest_file(ROOT/'paper_server/parsers/reference_source_reviews.py'),
        'source_review_registry': digest_file(ROOT/'config/reference_source_reviews.json'),
        'normalization_code': digest_file(ROOT / 'paper_server/parsers/reference_normalization.py'),
        'normalization_recovery_code': digest_file(ROOT / 'paper_server/parsers/reference_title_recovery.py'),
        'normalization_pdf_worker_code': digest_file(ROOT / 'scripts/reference_title_pdf_worker.py'),
        'author_resolution_version': AUTHOR_RESOLUTION_VERSION,
        'author_resolution_code': digest_file(ROOT / 'paper_server/parsers/reference_authority.py'),
        'author_resolution_registry': digest_file(AUTHOR_REGISTRY)}
    save(out / 'llm-settings.json', {'signature': signature, 'status_before': status, 'started_at': now(),
        'execution': 'loopback only; one inference at a time; source grounding; no live SQL writes'})
    migrate_llm_caches(out, signature)
    started = time.monotonic()
    save(out / 'enrich-status.json', {'status': 'running', 'started_at': now(), 'pid': os.getpid()})
    while True:
        pending, ready = [], []
        for paper in manifest['papers']:
            directory = out / 'papers' / paper['id']
            result = directory / 'result.json'
            if result.exists() and read(result).get('signature') == signature:
                continue
            pending.append(paper)
            if (directory / 'prepared.json').exists():
                ready.append(paper)
        if not pending:
            break
        if ready:
            enrich_group(ready[:25], out, client, guard, signature)
            summarize(out)
            continue
        prepared_status = out / 'prepare-status.json'
        if not follow or (prepared_status.exists() and read(prepared_status)['status'] == 'complete'):
            break
        time.sleep(5)
    guard.engine.dispose()
    save(out / 'enrich-status.json', {'status': 'complete' if not pending else 'waiting_for_prepare',
                                    'finished_at': now(), 'elapsed_seconds': round(time.monotonic() - started, 3)})
    summarize(out)


def summarize(out):
    manifest = read(out / 'manifest.json')
    statuses, sources, warnings, errors, fields, llm_status = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    refs = routed = completed = batches_count = grounding_rejections = 0
    llm_seconds = 0
    details = []
    prepared_count = 0
    for paper in manifest['papers']:
        directory = out / 'papers' / paper['id']
        if (directory / 'prepared.json').exists():
            prepared_count += 1
        path = directory / 'result.json'
        if not path.exists():
            continue
        item = read(path)
        statuses[item['status']] += 1
        sources[item.get('entry_source') or 'none'] += 1
        warnings.update(item['warnings'])
        errors.update(error['type'] for error in item['errors'])
        rows = item['rows']
        refs += len(rows)
        routed += item['routed']
        completed += item['llm_completed']
        batches_count += len(item['llm_timings'])
        llm_seconds += sum(t['elapsed_seconds'] for t in item['llm_timings'])
        for row in rows:
            fields.update(field for field in FIELDS if row.get(field) is not None)
            grounding_rejections += sum(w.endswith('_not_in_source') for w in row.get('warnings', []))
            llm_status[row.get('llm_status', 'unknown')] += 1
        details.append({'id': paper['id'], 'pdf': paper['pdf'], 'status': item['status'], 'references': len(rows),
                        'routed': item['routed'], 'warnings': item['warnings'], 'errors': item['errors']})
    result = {'version': BULK_VERSION, 'updated_at': now(), 'selected': len(manifest['papers']),
        'prepared': prepared_count, 'processed': sum(statuses.values()), 'statuses': dict(statuses),
        'sources': dict(sources), 'references': refs, 'routed_to_llm': routed, 'llm_completed': completed,
        'llm_batches': batches_count, 'llm_seconds': round(llm_seconds, 3), 'llm_status': dict(llm_status),
        'non_null_fields': dict(fields), 'grounding_rejections': grounding_rejections,
        'warnings': dict(warnings), 'errors': dict(errors), 'papers': details,
        'accuracy_note': 'Operational counts and source-substring checks only; not a 500-paper gold-standard accuracy score.'}
    save(out / 'summary.json', result)
    event(summary={k: v for k, v in result.items() if k != 'papers'})
    return result


def require_mutable_output(out):
    if any((out/name).exists() for name in ('final-verification.json','revalidation-summary.json','replay-summary.json','gold-freeze.json')):
        raise ValueError('Completed experiment is immutable; select a new --out directory')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--select', action='store_true')
    parser.add_argument('--limit', type=int, default=500)
    parser.add_argument('--seed', default='reference-bulk-20261017-heldout-v1')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--enrich', action='store_true')
    parser.add_argument('--follow', action='store_true')
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    if args.prepare or args.enrich:
        require_mutable_output(out)
    if args.select:
        with stage_lock(out, 'select'):
            select(out, args.limit, args.seed)
    if args.prepare:
        with stage_lock(out, 'prepare'):
            prepare(out)
    if args.enrich:
        with stage_lock(out, 'enrich'):
            enrich(out, args.follow)
    if args.summary:
        summarize(out)


if __name__ == '__main__':
    main()
