"""Read local reference-parser provenance and optionally probe loopback Ollama."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--online', action='store_true', help='Read loopback Ollama status without inference')
    args = parser.parse_args()
    runtime = ROOT / '.runtime/reference-tools'
    lock = json.loads((ROOT / 'config/reference_tools.lock.json').read_text(encoding='utf-8'))
    result = {'sources': {}, 'java_present': bool(list((runtime / 'java').glob('*/bin/java.exe'))),
              'grobid_jars': len(list((runtime / 'grobid/grobid-service/lib').glob('*.jar'))),
              'grobid_windows_native': (runtime / 'grobid/grobid-home/lib/win-64/libwapiti.dll').is_file(),
              'mineru_python_present': (ROOT / '.runtime/document-parsers/Scripts/python.exe').is_file()}
    for name, source in lock['sources'].items():
        path = runtime / 'sources' / name
        commit = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip() if path.exists() else None
        result['sources'][name] = {'commit': commit, 'matches_lock': commit == source['commit']}
    if args.online:
        from paper_server.ai.runtime import OllamaClient
        result['ollama'] = OllamaClient().status()
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if all(row['matches_lock'] for row in result['sources'].values()) and result['java_present'] and result['grobid_windows_native'] and result['grobid_jars'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
