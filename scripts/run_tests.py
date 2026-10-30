"""Run tests against a source-only temporary copy with disposable credentials."""
import argparse
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--python-only', action='store_true')
    scope.add_argument('--javascript-only', action='store_true')
    args = parser.parse_args()
    node = shutil.which('node')
    if not args.python_only and node is None:
        parser.error('Node.js is required for UI tests; use --python-only to run Python alone.')
    # Include current edits while honoring Git ignores for settings/data/logs.
    result = subprocess.run(['git', 'ls-files', '--cached', '--others',
                             '--exclude-standard', '-z'], cwd=ROOT,
                            capture_output=True, check=True)
    names = sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(('AUTH_', 'ADMIN_', 'DB_', 'YONSEI_', 'ZOTERO_',
                           'IEEE_API_', 'OPENALEX_', 'OBSIDIAN_', 'REVIEW_MEDIA_', 'LOCAL_AI_')):
            env.pop(key, None)
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
               AUTH_USERNAME='viewer', AUTH_PASSWORD_HASH='test-only-hash',
               ADMIN_USERNAME='admin', ADMIN_PASSWORD_HASH='',
               APP_SECRET_KEY=secrets.token_urlsafe(32), ACCOUNT_AUTH_ENABLED='1',
               ACCOUNT_ACTIVITY_ENABLED='0', SESSION_COOKIE_SECURE='0',
               DB_HOST='127.0.0.1', DB_PORT='1', DB_USER='unit-test',
               DB_PASSWORD='test-only', DB_NAME='unit_test', METADATA_MYSQL_TESTS='0',
               CROSSREF_MAILTO='', ACROBAT_EXECUTABLE='')
    with tempfile.TemporaryDirectory(prefix='paper-server-tests-') as directory:
        copy = Path(directory)
        for name in names:
            source = ROOT / name
            if not source.is_file():
                continue
            if name == '.env' or name == 'config/private_sources.json':
                raise RuntimeError('Private settings must not be tracked in Git.')
            target = (copy / name).resolve()
            if not target.is_relative_to(copy.resolve()):
                raise RuntimeError('Invalid source path in Git index.')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        env['PYTHONPATH'] = str(copy)
        if not args.javascript_only:
            status = subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                                     '-s', 'tests', '-t', '.', '-q'], cwd=copy, env=env)
            if status.returncode:
                return status.returncode
        if not args.python_only:
            tests = sorted(str(path.relative_to(copy)) for path in (copy / 'tests').glob('test_*.cjs'))
            return subprocess.run([node, '--test', '--test-timeout=10000', *tests],
                                  cwd=copy, env=env).returncode
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
