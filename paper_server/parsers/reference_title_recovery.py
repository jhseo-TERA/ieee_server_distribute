"""Local, cached PDF text fallback for titles rejected by math normalization."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from paper_server.paths import ROOT
from paper_server.parsers.reference_normalization import recover_from_pdf


def recover_titles(rows, pdf_path, source_sha256, cache_root):
    selected = [{'number': r['number'], 'page': r['page']} for r in rows
                if r.get('normalization_needs_review') and any(
                    w.startswith('math_') for w in r.get('normalization_warnings', []))]
    if not selected:
        return rows, {'requested': 0, 'recovered': 0, 'cache_hit': False}
    worker = ROOT / 'scripts/reference_title_pdf_worker.py'
    normalizer = ROOT / 'paper_server/parsers/reference_normalization.py'
    signature = {'pdf': str(Path(pdf_path).resolve()), 'source_sha256': source_sha256,
        'entries': selected, 'worker_sha256': hashlib.sha256(worker.read_bytes()).hexdigest(),
        'normalizer_sha256': hashlib.sha256(normalizer.read_bytes()).hexdigest()}
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    directory = Path(cache_root) / digest
    directory.mkdir(parents=True, exist_ok=True)
    request, answer = directory / 'request.json', directory / 'evidence.json'
    request.write_text(json.dumps(signature, indent=2), encoding='utf-8')
    cached = answer.exists()
    try:
        # Recheck the live PDF even when reusing text evidence from this cache.
        if hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest() != source_sha256:
            raise ValueError('Source PDF changed')
        if not cached:
            env = dict(os.environ, PYTHONUTF8='1')
            with (directory / 'worker.log').open('w', encoding='utf-8') as log:
                subprocess.run([str(ROOT / '.runtime/document-parsers/Scripts/python.exe'), str(worker),
                    '--request', str(request), '--output', str(answer)], stdout=log, stderr=log,
                    env=env, cwd=ROOT, timeout=90, check=True)
        payload = json.loads(answer.read_text(encoding='utf-8'))
        if payload.get('source_sha256') != source_sha256:
            raise ValueError('PDF evidence source mismatch')
        if not isinstance(payload.get('rows'), list) or any(not isinstance(e, dict) for e in payload['rows']):
            raise ValueError('Invalid PDF evidence rows')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return rows, {'requested': len(selected), 'recovered': 0, 'cache_hit': cached, 'error': type(exc).__name__}
    output, recovered = [], 0
    for row in rows:
        alternatives = [recover_from_pdf(row, e, expected_pdf_sha256=source_sha256) for e in payload['rows']
                        if e.get('number') == row['number'] and e.get('page') == row['page']]
        accepted = [r for r in alternatives if r.get('normalization_status') == 'recovered_from_pdf']
        if len(accepted) == 1:
            output.append(accepted[0])
            recovered += 1
        else:
            output.append(row)
    return output, {'requested': len(selected), 'recovered': recovered, 'cache_hit': cached,
                    'evidence': str(answer.relative_to(ROOT)) if answer.is_relative_to(ROOT) else str(answer)}
