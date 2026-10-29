"""Acquire pinned, project-local reference parsing tools; never installs OS features.

GROBID's official CRF image is used only as a verified distribution archive.
No Docker daemon/container is started. Windows raw-reference support is tested
separately; possession of these files does not imply a working PDF service.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import zipfile

import requests

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / '.runtime/reference-tools'
IMAGE_DIGEST = 'sha256:eb306e6d494f6f7e89b35bbaf3b4925afd58c6a5638c775f2a1c35bfd3c5db0d'
GROBID_COMMIT = '523bb8e97f357e93682dc4eaa6d22faa8b5b93de'
MINERU_COMMIT = 'c221cc41bc911ad0df3eaeda97acf6a1bfe9bf93'
OLLAMA_COMMIT = '45e6011815c74acde6a346512856b69dd79dd22b'


def session():
    value = requests.Session()
    value.trust_env = False
    return value


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, path, expected=None, headers=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and (not expected or sha(path) == expected):
        return
    partial = path.with_suffix(path.suffix + '.part')
    with session().get(url, headers=headers, stream=True, timeout=(10, 180)) as response:
        response.raise_for_status()
        with partial.open('wb') as stream:
            for chunk in response.iter_content(1024 * 1024):
                stream.write(chunk)
    if expected and sha(partial) != expected:
        raise ValueError('Distribution checksum mismatch: ' + path.name)
    partial.replace(path)
    print(json.dumps({'downloaded': path.name, 'bytes': path.stat().st_size}), flush=True)


def destination(root, relative):
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in ('..', '') for part in pure.parts):
        raise ValueError('Unsafe archive member')
    target = (root / Path(*pure.parts)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Archive member escapes destination')
    return target


def extract_zip(path, root):
    with zipfile.ZipFile(path) as archive:
        for item in archive.infolist():
            target = destination(root, item.filename)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)


def acquire_java():
    # Recorded from the official Eclipse Temurin/Adoptium distribution metadata.
    checksum = 'f9d6e191ab098c0d416e7d588a24420a8621cd2f4720dab2459b8b7b2d2d8b4e'
    url = ('https://github.com/adoptium/temurin21-binaries/releases/download/'
           'jdk-21.0.12.1%2B1/OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip')
    path = RUNTIME / 'downloads/temurin21-windows.zip'
    download(url, path, checksum)
    if not list((RUNTIME / 'java').glob('*/bin/java.exe')):
        extract_zip(path, RUNTIME / 'java')
    return {'version': '21.0.12.1+1', 'url': url, 'sha256': checksum}


def acquire_grobid_image():
    client = session()
    token_response = client.get('https://auth.docker.io/token', params={
        'service': 'registry.docker.io', 'scope': 'repository:grobid/grobid:pull'}, timeout=30)
    token_response.raise_for_status()
    headers = {'Authorization': 'Bearer ' + token_response.json()['token'],
               'Accept': 'application/vnd.oci.image.manifest.v1+json'}
    base = 'https://registry-1.docker.io/v2/grobid/grobid/'
    response = client.get(base + 'manifests/' + IMAGE_DIGEST, headers=headers, timeout=30)
    response.raise_for_status()
    if 'sha256:' + hashlib.sha256(response.content).hexdigest() != IMAGE_DIGEST:
        raise ValueError('OCI manifest checksum mismatch')
    manifest = response.json()
    (RUNTIME / 'grobid-image-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    selected = [layer for layer in manifest['layers'] if layer['size'] > 100_000_000]
    for layer in selected:
        digest = layer['digest'].split(':')[1]
        path = RUNTIME / 'downloads' / (digest + '.tar.gz')
        download(base + 'blobs/' + layer['digest'], path, digest, headers)
        extracted = 0
        with tarfile.open(path, 'r:gz') as archive:
            for item in archive:
                name = item.name.removeprefix('./')
                # Only application files; never OS paths, devices, links, or shell scripts.
                if not item.isfile() or not name.startswith('opt/grobid/'):
                    continue
                relative = name[len('opt/grobid/'):]
                if not (relative.startswith(('grobid-home/', 'grobid-service/')) or relative.endswith('.jar')):
                    continue
                target = destination(RUNTIME / 'grobid', relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                extracted += 1
        print(json.dumps({'grobid_distribution_files': extracted}), flush=True)
    return {'version': '0.9.1-crf', 'image': 'grobid/grobid', 'digest': IMAGE_DIGEST}


def acquire_source(name, repository, tag, expected_commit, directories):
    target = RUNTIME / 'sources' / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--filter=blob:none', '--depth=1', '--branch', tag,
                        '--sparse', repository, str(target)], check=True)
    commit = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != expected_commit:
        raise ValueError('Unexpected source commit: ' + name)
    subprocess.run(['git', '-C', str(target), 'sparse-checkout', 'set', *directories], check=True)
    return {'repository': repository, 'tag': tag, 'commit': commit, 'path': str(target.relative_to(ROOT))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch', action='store_true', help='Explicitly download official distributions and sources')
    args = parser.parse_args()
    if not args.fetch:
        print(json.dumps({'runtime': str(RUNTIME), 'manifest_present': (RUNTIME / 'acquired.json').is_file()}))
        return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Disjoint downloads/extractions; no installed server environment is modified.
    with ThreadPoolExecutor(max_workers=3) as pool:
        java = pool.submit(acquire_java)
        image = pool.submit(acquire_grobid_image)
        grobid = pool.submit(acquire_source, 'grobid', 'https://github.com/grobidOrg/grobid.git',
            '0.9.1', GROBID_COMMIT, ['grobid-core/src/main', 'grobid-home/lib/win-64'])
        results = {'java': java.result(), 'grobid_distribution': image.result(), 'grobid_source': grobid.result()}
    results['mineru_source'] = acquire_source('mineru', 'https://github.com/opendatalab/MinerU.git',
        'mineru-4.0.10-released', MINERU_COMMIT, ['mineru'])
    native = RUNTIME / 'sources/grobid/grobid-home/lib/win-64/libwapiti.dll'
    target = RUNTIME / 'grobid/grobid-home/lib/win-64/libwapiti.dll'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(native, target)
    results['ollama_source'] = acquire_source('ollama', 'https://github.com/ollama/ollama.git',
        'v0.40.4', OLLAMA_COMMIT, ['api', 'server', 'cmd', 'docs'])
    results['ollama_runtime'] = session().get('http://127.0.0.1:11434/api/version', timeout=5).json()
    (RUNTIME / 'acquired.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2), flush=True)


if __name__ == '__main__':
    main()
