"""Run PaddleOCR LayoutDetection only in the isolated document-parser runtime."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.parsers.paddle_layout import DEFAULT_MODEL, VERSION, pixel_box_to_pdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--model-dir', type=Path)
    parser.add_argument('--device', default='cpu', choices=['cpu', 'gpu:0'])
    parser.add_argument('--max-pages', type=int, default=24)
    parser.add_argument('--render-scale', type=float, default=1.5)
    parser.add_argument('--download-model', action='store_true',
                        help='Explicitly allow PaddleOCR to provision its official model cache.')
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 24 or not .5 <= args.render_scale <= 3:
        parser.error('max-pages must be 1..24 and render-scale must be 0.5..3')
    os.environ.setdefault('PADDLE_PDX_CACHE_HOME', str(ROOT / '.runtime/document-parsers/models/paddle'))
    os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'BOS')
    os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')
    model_dir = args.model_dir or Path(os.environ['PADDLE_PDX_CACHE_HOME']) / 'official_models' / args.model
    if not args.download_model and not model_dir.is_dir():
        parser.error('Local model is missing. Provision explicitly with --download-model first.')

    import pypdfium2 as pdfium
    from paddleocr import LayoutDetection
    # Disable oneDNN to retain compatibility with Windows CPU wheels and avoid
    # backend-specific PIR conversion errors; one isolated process holds model.
    model = LayoutDetection(model_name=args.model, model_dir=str(model_dir) if model_dir.is_dir() else None,
                            device=args.device, enable_mkldnn=False, cpu_threads=4)
    digest = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    result = {'version': VERSION, 'pdf_sha256': digest, 'model': args.model, 'pages': {},
              'coordinate_space': 'unrotated_page_local_bottom_left',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'provenance': {'repository': 'https://github.com/PaddlePaddle/PaddleOCR',
                             'paddleocr_version': importlib.metadata.version('paddleocr'),
                             'paddlepaddle_version': importlib.metadata.version('paddlepaddle'),
                             'device': args.device, 'render_scale': args.render_scale,
                             'source_is_original_pdf': True}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='paddle-pages-', dir=args.output.parent) as temporary:
        with pdfium.PdfDocument(str(args.pdf), password='') as document:
            result['page_count'] = len(document)
            result['scanned_pages'] = min(len(document), args.max_pages)
            for index in range(result['scanned_pages']):
                page = document[index]
                try:
                    bounds = page.get_bbox()
                    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
                    if any(not math.isfinite(v) or not 0 < v < 20000 for v in (width, height)):
                        raise ValueError('Unsupported page dimensions.')
                    scale = min(args.render_scale, 2200 / max(width, height))
                    bitmap = page.render(scale=scale, rotation=(-page.get_rotation()) % 360,
                                         draw_annots=False)
                    raster = Path(temporary) / f'page-{index + 1}.png'
                    try:
                        bitmap.to_pil().convert('RGB').save(raster)
                        pixel_width, pixel_height = bitmap.width, bitmap.height
                    finally:
                        bitmap.close()
                    regions = []
                    for prediction in model.predict(str(raster), batch_size=1, layout_nms=True):
                        data = prediction.json
                        if isinstance(data, str):
                            data = json.loads(data)
                        data = data.get('res', data)
                        for detected in data.get('boxes', []):
                            box = [float(v) for v in detected['coordinate']]
                            # Some detectors extend one or two pixels beyond the image.
                            box = [max(0, box[0]), max(0, box[1]), min(pixel_width, box[2]), min(pixel_height, box[3])]
                            if box[2] <= box[0] or box[3] <= box[1]:
                                continue
                            regions.append({'label': str(detected['label']),
                                            'confidence': round(float(detected['score']), 6),
                                            'bbox': pixel_box_to_pdf(box, pixel_width, pixel_height, width, height)})
                    result['pages'][str(index + 1)] = {'width': width, 'height': height, 'regions': regions,
                                                       'raster_width': pixel_width, 'raster_height': pixel_height,
                                                       'render_scale': scale}
                    print(json.dumps({'page': index + 1, 'regions': len(regions)}, ensure_ascii=False), flush=True)
                finally:
                    page.close()
    if hashlib.sha256(args.pdf.read_bytes()).hexdigest() != digest:
        raise RuntimeError('PDF changed during inference.')
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
