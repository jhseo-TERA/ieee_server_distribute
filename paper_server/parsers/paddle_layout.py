"""Offline PaddleOCR layout cache; the web interpreter never imports Paddle.

Provision the isolated runtime/model explicitly. Missing or failed inference is
diagnostic data and does not prevent the existing PDF extraction path working.
All boxes use unrotated, visible-page PDF coordinates with a bottom-left origin.
"""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

VERSION = 'paddle-layout-v1'
DEFAULT_MODEL = 'PP-DocLayout-L'
MAX_RESULT_BYTES = 8 * 1024 * 1024


def pixel_box_to_pdf(box, pixel_width, pixel_height, width, height):
    """Convert a box from an explicitly unrotated page raster back to PDF."""
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
            or not (pixel_width > 0 and pixel_height > 0 and width > 0 and height > 0)):
        raise ValueError('Invalid layout geometry.')
    l, t, r, b = box
    if not (0 <= l < r <= pixel_width + 2 and 0 <= t < b <= pixel_height + 2):
        raise ValueError('Layout box is outside the page raster.')
    return [round(l / pixel_width * width, 3), round(max(0, height - b / pixel_height * height), 3),
            round(min(width, r / pixel_width * width), 3), round(height - t / pixel_height * height, 3)]


def valid_pages(pages, max_pages):
    if not isinstance(pages, dict) or len(pages) > max_pages:
        return False
    for key, entry in pages.items():
        if not isinstance(key, str) or not key.isdigit() or not 1 <= int(key) <= max_pages:
            return False
        if not isinstance(entry, dict):
            return False
        width, height = entry.get('width'), entry.get('height')
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 < v < 20000 for v in (width, height)):
            return False
        regions = entry.get('regions')
        if not isinstance(regions, list) or len(regions) > 1000:
            return False
        for region in regions:
            if not isinstance(region, dict):
                return False
            box, confidence, label = region.get('bbox'), region.get('confidence'), region.get('label')
            if (not isinstance(box, list) or len(box) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                    or not (0 <= box[0] < box[2] <= width + .01 and 0 <= box[1] < box[3] <= height + .01)
                    or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
                    or not isinstance(label, str) or not 1 <= len(label) <= 80):
                return False
    return True


class PaddleLayoutStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runtime = self.root / '.runtime/document-parsers'
        self.model_name = os.getenv('SERDES_PADDLE_MODEL', DEFAULT_MODEL)
        self.model_dir = Path(os.getenv('SERDES_PADDLE_MODEL_DIR') or
                              str(self.runtime / 'models/paddle/official_models' / self.model_name)).resolve()
        self.python = Path(os.getenv('DOCUMENT_PARSER_PYTHON') or os.getenv('IEEE_DOCUMENT_PARSER_PYTHON') or
                           str(self.runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')))
        self.directory = self.root / 'outputs/document_parsers/paddle_layout'

    def model_signature(self):
        """Invalidate results when the local model/config files change."""
        if not self.model_dir.is_dir():
            return None
        files = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns)
                       for p in self.model_dir.iterdir() if p.is_file())
        if not files:
            return None
        return hashlib.sha256(json.dumps(files).encode()).hexdigest()

    def runtime_versions(self):
        """Read the isolated runtime's metadata without importing its libraries."""
        candidates = [self.python.parent.parent / 'Lib/site-packages']
        candidates.extend((self.python.parent.parent / 'lib').glob('python*/site-packages'))
        versions = {}
        for directory in candidates:
            for package in ('paddleocr', 'paddlepaddle', 'paddlex', 'pypdfium2', 'pillow'):
                for metadata in directory.glob(package + '-*.dist-info'):
                    try:
                        versions[package] = importlib.metadata.Distribution.at(metadata).version
                    except (OSError, ValueError):
                        versions[package] = 'unknown'
        return versions

    def preserve_log(self, log, key):
        """Retain the last 64 KiB per failed source/model, never unbounded logs."""
        try:
            destination = self.directory / 'failures' / (key + '.log')
            destination.parent.mkdir(parents=True, exist_ok=True)
            with log.open('rb') as source:
                source.seek(max(0, log.stat().st_size - 65536))
                destination.write_bytes(source.read(65536))
            return destination.relative_to(self.root).as_posix()
        except OSError:
            return None

    def status(self):
        if os.getenv('SERDES_PADDLE_LAYOUT', '1').lower() in ('0', 'false', 'off'):
            return {'status': 'disabled', 'model': self.model_name}
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', self.model_name):
            return {'status': 'invalid_model', 'model': DEFAULT_MODEL}
        if not self.python.is_file():
            return {'status': 'runtime_unavailable', 'model': self.model_name}
        if not self.model_signature():
            return {'status': 'model_unavailable', 'model': self.model_name}
        return {'status': 'ready', 'model': self.model_name}

    def prepare(self, pdf_path, digest, max_pages=24, retry=False, timeout=180):
        state = self.status()
        if state['status'] != 'ready':
            return {**state, 'pages': {}}
        path = Path(pdf_path).resolve(strict=True)
        if not path.is_relative_to(self.root) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            raise ValueError('Layout source must be a hashed PDF inside the workspace.')
        max_pages = max(1, min(int(max_pages), 24))
        identity = {'version': VERSION, 'pdf_sha256': digest, 'model': self.model_name,
                    'model_signature': self.model_signature(), 'runtime_versions': self.runtime_versions(),
                    'max_pages': max_pages}
        cache_key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = self.directory / f'{cache_key}.json'
        if not retry:
            try:
                if cache.stat().st_size <= MAX_RESULT_BYTES:
                    result = json.loads(cache.read_text(encoding='utf-8'))
                    if (result.get('identity') == identity and result.get('status') == 'ready'
                            and valid_pages(result.get('pages'), max_pages)):
                        return {**result, 'cached': True}
            except (OSError, ValueError, AttributeError):
                pass
        self.directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment.update(PADDLE_PDX_CACHE_HOME=str(self.runtime / 'models/paddle'),
                           PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK='True')
        try:
            with tempfile.TemporaryDirectory(prefix='paddle-', dir=self.directory) as temporary:
                output = Path(temporary) / 'result.json'
                command = [str(self.python), str(self.root / 'scripts/paddle_layout_worker.py'),
                           '--pdf', str(path), '--output', str(output), '--model', self.model_name,
                           '--model-dir', str(self.model_dir), '--max-pages', str(max_pages), '--device', 'cpu']
                log = Path(temporary) / 'worker.log'
                try:
                    with log.open('wb') as stream:
                        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                                   timeout=max(10, min(timeout, 600)), check=False, env=environment,
                                                   cwd=str(self.root), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                except subprocess.TimeoutExpired:
                    return {**state, 'status': 'timeout', 'pages': {},
                            'diagnostic_file': self.preserve_log(log, cache_key)}
                except OSError:
                    return {**state, 'status': 'inference_failed', 'pages': {}, 'reason': 'worker_unavailable',
                            'diagnostic_file': self.preserve_log(log, cache_key)}
                if completed.returncode or not output.is_file() or output.stat().st_size > MAX_RESULT_BYTES:
                    return {**state, 'status': 'inference_failed', 'pages': {}, 'reason': 'worker_failed',
                            'diagnostic_file': self.preserve_log(log, cache_key)}
                try:
                    result = json.loads(output.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    result = None
                if (not isinstance(result, dict) or result.get('pdf_sha256') != digest or result.get('model') != self.model_name
                        or not valid_pages(result.get('pages'), max_pages)):
                    return {**state, 'status': 'inference_failed', 'pages': {}, 'reason': 'invalid_result',
                            'diagnostic_file': self.preserve_log(log, cache_key)}
                if self.model_signature() != identity['model_signature']:
                    return {**state, 'status': 'inference_failed', 'pages': {}, 'reason': 'model_changed'}
                result.update(identity=identity, status='ready', cached=False)
                output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
                output.replace(cache)
                return result
        except (OSError, ValueError, TypeError, AttributeError):
            return {**state, 'status': 'inference_failed', 'pages': {}, 'reason': 'worker_unavailable'}
