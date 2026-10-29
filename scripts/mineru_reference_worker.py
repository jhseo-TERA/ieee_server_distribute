"""Persistent, local-only MinerU worker for a resumable reference experiment.

stdin/stdout carry JSON lines; library diagnostics go to stderr. One PDF is
processed at a time. The caller owns this process and enforces a task timeout.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.serdes.mineru import mineru_environment


def main():
    for name in list(os.environ):
        if name.startswith('MINERU_'):
            del os.environ[name]
    os.environ.update(mineru_environment())
    if version('mineru') != '4.0.10':
        raise RuntimeError('Expected MinerU 4.0.10')
    with redirect_stdout(sys.stderr):
        from mineru.parser import parse
        from mineru.config import VlmConfig
        from mineru.parser.writer import FileBasedDataWriter
    for line in sys.stdin:
        task = json.loads(line)
        started = time.monotonic()
        try:
            start, end = task['start'], task['end']
            if not 1 <= start <= end <= 10000 or end - start >= 300:
                raise ValueError('Invalid page range')
            output = Path(task['output'])
            output.mkdir(parents=True, exist_ok=True)
            with redirect_stdout(sys.stderr):
                result = parse(Path(task['pdf']), tier='basic', image_analysis=False,
                               page_range=f'{start}-{end}', vlm_config=VlmConfig(server_url=''))
                result.save(FileBasedDataWriter(str(output)))
            answer = {'id': task['id'], 'ok': True}
        except Exception as exc:
            # No credentials or model-generated strings in the control protocol.
            answer = {'id': task['id'], 'ok': False, 'error': type(exc).__name__}
            import traceback
            traceback.print_exc(file=sys.stderr)
        answer['seconds'] = round(time.monotonic() - started, 3)
        print('@@REFERENCE_RESULT@@' + json.dumps(answer), flush=True)


if __name__ == '__main__':
    main()
