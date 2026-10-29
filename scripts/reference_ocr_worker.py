"""Render only selected weak-text pages and run the existing local OCR runtime."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.parsers.reference_ocr import EXTRACTOR, VERSION, file_hash, ocr_reading_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8'))
    pdf = Path(request['pdf']).resolve()
    if pdf.suffix.lower() != '.pdf' or not any(pdf.is_relative_to((ROOT / p).resolve()) for p in
                                               ('ieee-pdf', 'optica-pdf', 'nature-pdf', 'DesignCon')):
        raise ValueError('PDF outside authorized library')
    if file_hash(pdf) != request['source_sha256'] or pdf.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('PDF hash or size changed')
    native_path = Path(request['native_path'])
    if file_hash(native_path) != request['native_sha256']:
        raise ValueError('Native text evidence changed')
    native = json.loads(native_path.read_text(encoding='utf-8'))
    if native['source_sha256'] != request['source_sha256']:
        raise ValueError('Native evidence belongs to another PDF')
    selected = request['plan']['pages']
    if not 1 <= len(selected) <= 24 or len(selected) != len(set(selected)):
        raise ValueError('Invalid OCR page budget')
    settings = request['settings']
    model = Path(settings['model'])
    if file_hash(model) != settings['model_sha256']:
        raise ValueError('OCR model changed')
    import pypdfium2 as pdfium
    from PIL import Image
    folder = args.output.parent / 'pages'
    folder.mkdir(parents=True, exist_ok=True)
    jobs = []
    with pdfium.PdfDocument(str(pdf)) as document:
        if not 1 <= len(document) <= 300 or any(not 1 <= n <= len(document) for n in selected):
            raise ValueError('Invalid PDF page selection')
        for number in selected:
            page = document[number - 1]
            try:
                width, height = page.get_size()
                if min(width, height) <= 0 or max(width, height) > 20000:
                    raise ValueError('Unsupported PDF page dimensions')
                scale = min(settings['render_scale'], 2800 / max(width, height))
                bitmap = page.render(scale=scale, draw_annots=False)
                image = folder / f'page-{number:03}.png'
                try:
                    bitmap.to_pil().convert('RGB').save(image)
                finally:
                    bitmap.close()
                jobs.append({'page': number, 'image': str(image),
                             'output': str(folder / f'page-{number:03}.json')})
            finally:
                page.close()
    job_path = args.output.parent / 'ocr-jobs.json'
    job_path.write_text(json.dumps({'modelDirectory': str(model.parent), 'pages': jobs}), encoding='utf-8')
    subprocess.run([settings['node'], str(ROOT / 'scripts/reference_ocr.cjs'), str(job_path)],
        env=dict(os.environ, NODE_PATH=settings['node_modules']),
        timeout=min(610, 30 + 24 * len(jobs)), check=True)
    result = deepcopy(native)
    result['extractor'] = EXTRACTOR
    result['ocr'] = {'version': VERSION, 'status': 'complete', **request['plan'], 'settings': settings,
                     'native_path': str(native_path), 'native_sha256': request['native_sha256']}
    by_page = {p['page']: p for p in result['pages']}
    for job in jobs:
        recognized = json.loads(Path(job['output']).read_text(encoding='utf-8'))
        record = by_page[job['page']]
        with Image.open(job['image']) as raster:
            raster_height = raster.height
        text, joined = ocr_reading_text(recognized, raster_height)
        record.update(native_text=record['text'], text=text, literal_text=recognized['text'],
            geometric_exponents=[], ocr={'engine': 'tesseract.js', 'confidence': recognized['confidence'],
                'low_confidence_words': recognized['low_confidence_words'],
                'joined_labels': joined, 'label_repairs': recognized.get('label_repairs', []),
                'raster_path': job['image'], 'raster_sha256': file_hash(job['image']),
                'recognition_path': job['output'], 'recognition_sha256': file_hash(job['output'])})
    if file_hash(pdf) != request['source_sha256']:
        raise ValueError('PDF changed during OCR')
    temporary = args.output.with_suffix('.part')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    temporary.replace(args.output)


if __name__ == '__main__':
    main()
