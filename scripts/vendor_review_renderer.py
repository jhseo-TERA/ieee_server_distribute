"""Vendor pinned browser distributions; verify npm tarball integrity, run no installers."""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {'katex':'0.19.1','markdown-it':'15.0.3'}


def vendor():
    versions = {}
    for name,version in PACKAGES.items():
        with urlopen(f'https://registry.npmjs.org/{name}/{version}',timeout=30) as response:
            metadata = json.load(response)
        with urlopen(metadata['dist']['tarball'],timeout=60) as response:
            data = response.read(15*1024*1024)
        algorithm,digest = metadata['dist']['integrity'].split('-',1)
        if algorithm != 'sha512' or base64.b64encode(hashlib.sha512(data).digest()).decode() != digest:
            raise RuntimeError('Package integrity mismatch: '+name)
        target = ROOT / 'web/static/vendor' / name
        target.mkdir(parents=True,exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = member.name.removeprefix('package/')
                keep = path in {'LICENSE','LICENSE.txt','dist/katex.min.js','dist/katex.min.css','dist/browser/markdown-it.umd.min.js'} or path.startswith('dist/fonts/')
                if not keep:
                    continue
                relative = path.removeprefix('dist/')
                if path == 'dist/browser/markdown-it.umd.min.js':
                    relative = 'markdown-it.min.js'
                destination = target / relative
                if not destination.resolve().is_relative_to(target.resolve()):
                    raise RuntimeError('Unsafe archive path')
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
        versions[name] = dict(version=version,license=metadata['license'],integrity=metadata['dist']['integrity'],source=metadata['dist']['tarball'])
    (ROOT/'web/static/vendor/review-renderer.json').write_text(json.dumps(versions,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({name:value['version'] for name,value in versions.items()}))


if __name__=='__main__':
    vendor()
