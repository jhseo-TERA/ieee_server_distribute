"""Local OCR only for pages whose native text cannot support a bibliography."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from paper_server.paths import ROOT
from paper_server.parsers.reference_segmentation import HEADING, segment_references, compact, damaged_characters,uncertain_scientific_text

VERSION = 'reference-text-first-ocr-v3'
EXTRACTOR = 'pypdfium2+tesseract.js'
MAX_PAGES = 24
MIN_NATIVE_CHARACTERS = 80


def order_ocr_lines(lines):
    """Read separated columns left to right when a real empty gutter exists."""
    if len(lines) < 6:
        return sorted(lines, key=lambda line: (line['bbox'][1], line['bbox'][0]))
    intervals = sorted((line['bbox'][0], line['bbox'][2]) for line in lines)
    merged = []
    for left, right in intervals:
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    gaps = [(b[0] - a[1], (b[0] + a[1]) / 2) for a, b in zip(merged, merged[1:])]
    span = max(right for left, right in intervals) - intervals[0][0]
    for width, split in sorted(gaps, reverse=True):
        left = [line for line in lines if line['bbox'][2] <= split]
        right = [line for line in lines if line['bbox'][0] >= split]
        if width >= max(12, span * .025) and min(len(left), len(right)) >= 3:
            return order_ocr_lines(left) + order_ocr_lines(right)
    return sorted(lines, key=lambda line: (line['bbox'][1], line['bbox'][0]))


def ocr_reading_text(recognized, raster_height):
    """Attach detached labels only to nearby text on the same printed baseline."""
    lines = []
    for source in recognized['lines']:
        line = {'text': source['text'].strip(), 'bbox': list(source['bbox'])}
        if line['bbox'][1] < raster_height * .06 and re.fullmatch(
                r'(?:Research Article|Vol\..*(?:Optica|\d{3,4})|.*Vol\..*Optica.*)', line['text'], re.I):
            continue
        lines.append(line)
    removed, joined = set(), []
    for i, label in enumerate(lines):
        if not re.fullmatch(r'(?:\d{1,3}\.|\[\d{1,3}\])', label['text']):
            continue
        l, t, r, b = label['bbox']
        matches = [j for j, body in enumerate(lines) if j != i and j not in removed
            and body['bbox'][0] >= r and 0 <= body['bbox'][0] - r < (b - t) * 3
            and abs((body['bbox'][1] + body['bbox'][3]) / 2 - (t + b) / 2) < (b - t) * .65]
        if len(matches) != 1:
            continue
        j = matches[0]
        body = lines[j]
        body['text'] = label['text'] + ' ' + body['text']
        body['bbox'] = [l, min(t, body['bbox'][1]), body['bbox'][2], max(b, body['bbox'][3])]
        removed.add(i)
        joined.append({'label': label['text'], 'label_bbox': label['bbox'], 'body_bbox': body['bbox']})
    if joined:
        return '\n'.join(line['text'] for line in order_ocr_lines(
            [line for i, line in enumerate(lines) if i not in removed])), joined
    text = recognized['text']
    for source in recognized['lines']:
        if source['text'].strip() and not any(line['text'] == source['text'].strip() for line in lines):
            text = text.replace(source['text'], '', 1)
    return text, []


def ocr_page_plan(data, *, max_pages=MAX_PAGES):
    """Never replace readable text or infer absence from the last few pages."""
    if not 1 <= max_pages <= 24:
        raise ValueError('OCR page budget must be 1..24')
    pages = data['pages']
    native_entries = segment_references([(p['page'], p['text']) for p in pages])['entries']
    native_reference_pages = {entry['page'] for entry in native_entries}
    headings = [p['page'] for p in pages if HEADING.search(p['text'])]
    first = min(headings) if headings else 1
    weak, reasons = [], {}
    for page in pages:
        text = page['text']
        visible = ''.join(c for c in text if not c.isspace())
        bad = len(damaged_characters(compact(text)))
        short = sum(c.isalnum() for c in text) < MIN_NATIVE_CHARACTERS
        damaged_reference = any(e['page'] == page['page'] and damaged_characters(e['raw']) for e in native_entries)
        uncertain_reference = any(e['page']==page['page'] and uncertain_scientific_text(e['raw']) for e in native_entries)
        if page['page'] >= first and (short and page['page'] not in native_reference_pages
                                      or damaged_reference or uncertain_reference or bad > max(3, len(visible) * .02)):
            weak.append(page['page'])
            reasons[str(page['page'])] = ('uncertain_scientific_reference' if uncertain_reference
                else 'damaged_native_text' if bad else 'insufficient_native_text')
    return {'pages': weak[:max_pages], 'candidate_pages': weak,
            'budget_exceeded': len(weak) > max_pages, 'native_heading_found': bool(headings),
            'reasons': reasons}


@lru_cache(maxsize=32)
def _file_hash(path, mtime_ns, size):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def file_hash(path):
    path = Path(path)
    stat = path.stat()
    return _file_hash(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


def ocr_settings():
    node = os.getenv('SERDES_OCR_NODE') or shutil.which('node')
    model = ROOT / 'outputs/serdes_ocr/eng.traineddata'
    if not node or not model.is_file():
        raise FileNotFoundError('Local Node.js and English OCR model are required')
    node = Path(node).resolve()
    modules = node.parent.parent / 'node_modules'
    package = modules / 'tesseract.js/package.json'
    if not package.is_file():
        raise FileNotFoundError('Local tesseract.js is unavailable')
    return {'version': VERSION, 'engine': 'tesseract.js',
        'engine_version': json.loads(package.read_text(encoding='utf-8'))['version'],
        'node': str(node), 'node_modules': str(modules), 'model': str(model),
        'model_sha256': file_hash(model), 'language': 'eng', 'render_scale': 3,
        'page_segmentation_mode': 3,
        'policy_sha256': file_hash(Path(__file__)),
        'worker_sha256': file_hash(ROOT / 'scripts/reference_ocr_worker.py'),
        'engine_script_sha256': file_hash(ROOT / 'scripts/reference_ocr.cjs')}


def with_ocr_fallback(pdf, native, cache_root):
    plan = ocr_page_plan(native)
    if not plan['pages']:
        return {**native, 'ocr': {'status': 'not_needed', **plan}}
    try:
        settings = ocr_settings()
    except (OSError, ValueError, KeyError) as exc:
        return {**native, 'ocr': {'status': 'unavailable', 'error': type(exc).__name__, **plan}}
    request = {'pdf': str(Path(pdf).resolve()), 'source_sha256': native['source_sha256'],
        'native_path': str(Path(native['evidence_path']).resolve()), 'native_sha256': native['evidence_sha256'],
        'settings': settings, 'plan': plan}
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    folder = Path(cache_root) / key
    folder.mkdir(parents=True, exist_ok=True)
    request_path, answer = folder / 'request.json', folder / 'evidence.json'
    request_path.write_text(json.dumps(request), encoding='utf-8')
    cached = answer.exists()
    if not cached:
        try:
            with (folder / 'worker.log').open('w', encoding='utf-8') as log:
                subprocess.run([str(ROOT / '.runtime/document-parsers/Scripts/python.exe'),
                    str(ROOT / 'scripts/reference_ocr_worker.py'), '--request', str(request_path),
                    '--output', str(answer)], cwd=ROOT,
                    env=dict(os.environ, PYTHONUTF8='1', OMP_NUM_THREADS='2'),
                    stdout=log, stderr=log, timeout=min(640, 40 + 25 * len(plan['pages'])), check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            return {**native, 'ocr': {'status': 'failed', 'error': type(exc).__name__,
                'log': str(folder / 'worker.log'), **plan}}
    data = json.loads(answer.read_text(encoding='utf-8'))
    if (data.get('source_sha256') != native['source_sha256'] or data.get('extractor') != EXTRACTOR
            or data.get('ocr', {}).get('settings') != settings):
        raise ValueError('OCR evidence identity rejected')
    data['evidence_path'] = str(answer)
    data['evidence_sha256'] = file_hash(answer)
    data['ocr']['cache_hit'] = cached
    return data
