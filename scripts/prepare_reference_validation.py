"""Freeze a fresh validation sample, then prepare source-only PDF evidence.

Extraction deliberately never reads parser predictions. Reference labels here
are navigation aids; reviewed gold, not this detector, supplies the true count.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'reference-source-validation-v1'
DEFAULT = ROOT / 'outputs/reference_validation_100_20261017'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.part')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fresh_sample(papers, excluded, count, seed):
    groups = defaultdict(list)
    for paper in papers:
        if paper['pdf'] not in excluded:
            groups[paper['publisher']].append(paper)
    total = sum(map(len, groups.values()))
    if not 0 < count <= total:
        raise ValueError('Insufficient fresh papers')
    exact = {k: count * len(v) / total for k, v in groups.items()}
    quota = {k: math.floor(v) for k, v in exact.items()}
    for k in sorted(quota, key=lambda k: (-(exact[k] - quota[k]), k))[:count - sum(quota.values())]:
        quota[k] += 1
    rank = lambda p: hashlib.sha256((seed + p['sha256']).encode()).hexdigest()
    chosen = [p for k, rows in groups.items() for p in sorted(rows, key=rank)[:quota[k]]]
    chosen.sort(key=rank)
    if len({p['sha256'] for p in chosen}) != count:
        raise ValueError('Duplicate source hashes')
    return chosen, quota


def select(baseline, out, count, seed, exclude_manifests=()):
    additional = [Path(p).resolve() for p in exclude_manifests]
    if (out / 'manifest.json').exists():
        existing = read(out / 'manifest.json')
        if existing['count'] != count or existing['seed'] != seed or existing['baseline'] != str(baseline):
            raise ValueError('Existing immutable selection differs')
        if existing.get('additional_exclusion_manifests', []) != [str(p) for p in additional]:
            raise ValueError('Existing immutable selection exclusions differ')
        return existing
    exclusions = [baseline / 'holdout-audit-gold.json', ROOT / 'config/reference_normalization_cases.json',
                  baseline / 'manual-audit-gold.json', ROOT / 'config/reference_benchmark.json', *additional]
    excluded = set()
    for path in exclusions:
        item = read(path)
        if 'cases' in item:
            excluded.update(c['pdf'] for c in item['cases'])
        elif 'papers' in item:
            excluded.update(p['pdf'] for p in item['papers'])
        else:
            excluded.add(item['pdf'])
    source_manifest = read(baseline / 'manifest.json')
    chosen, quota = fresh_sample(source_manifest['papers'], excluded, count, seed)
    manifest = {'version': VERSION, 'created_at': datetime.now(timezone.utc).isoformat(),
        'baseline': str(baseline), 'baseline_manifest_sha256': sha(baseline / 'manifest.json'),
        'count': count, 'seed': seed, 'publisher_quotas': quota,
        'selection': 'Publisher-proportional SHA256 ranking from existing 500; previously reviewed source papers excluded; no success-status filtering.',
        'additional_exclusion_manifests': [str(p) for p in additional],
        'exclusion_sources': {str(p.relative_to(ROOT)): sha(p) for p in exclusions},
        'excluded_in_500': [p['pdf'] for p in source_manifest['papers'] if p['pdf'] in excluded], 'papers': []}
    for index, paper in enumerate(chosen, 1):
        source = baseline / 'papers' / paper['id'] / 'result.json'
        target = out / 'predictions' / (paper['id'] + '.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest['papers'].append({**paper, 'audit_id': f'p{index:03}', 'prediction_sha256': sha(source)})
    code = sorted((ROOT / 'paper_server/parsers').glob('reference*.py'))
    code.extend((ROOT / 'scripts/benchmark_reference_bulk.py', ROOT / 'scripts/reference_evidence_worker.py',
                 ROOT / 'config/reference_authorities.json', ROOT / 'config/reference_source_reviews.json'))
    manifest['pipeline_code_sha256'] = {str(p.relative_to(ROOT)): sha(p) for p in code}
    save(out / 'manifest.json', manifest)
    return manifest


HEADING = re.compile(r'(?mi)^\s*(?:(?:[IVX]+\.?\s+)?REFERENCES(?:\s+AND\s+LINKS)?|BIBLIOGRAPHY)\b\s*:?')
PATTERNS = {
    'bracket': r'\[\s*(\d{1,3})\s*\]\s*',
    'dot': r'(?m)^\s*(\d{1,3})\.\s+',
    'bare': r'(?m)^\s*(\d{1,3})\s+(?=[A-Za-z])',
}


def source_inventory(pages):
    text, positions = '', []
    for number, value in pages:
        positions.append((len(text), number))
        text += value + '\n'
    headings = list(HEADING.finditer(text))
    start = headings[-1].end() if headings else 0
    variants = []
    for style, pattern in PATTERNS.items():
        labels = list(re.finditer(pattern, text[start:]))
        # A line-wrapped in-text citation can appear before the bibliography.
        # Find the longest contiguous sequence beginning at 1; keep warnings.
        for begin, label in enumerate(labels):
            if int(label.group(1)) != 1:
                continue
            run = []
            for match in labels[begin:]:
                if int(match.group(1)) != len(run) + 1:
                    break
                run.append(match)
            variants.append((len(run), style, run, labels))
    if not variants:
        return {'heading_found': bool(headings), 'detected_count': None, 'style': None, 'entries': [],
                'warnings': ['no_contiguous_numbered_inventory'], 'heading_start': start, 'page_count': len(pages)}
    _, style, run, all_labels = max(variants, key=lambda v: (v[0], v[1] == 'bracket', v[2][0].start()))
    entries = []
    for index, label in enumerate(run):
        # Leading \s* can consume the previous page's trailing newline. Use
        # the label digits to locate its page, not the whitespace match start.
        absolute = start + label.start(1)
        content = start + label.end()
        stop = start + run[index + 1].start() if index + 1 < len(run) else len(text)
        page = max((n for pos, n in positions if pos <= absolute), default=1)
        entries.append({'number': int(label.group(1)), 'page': page, 'source_start': absolute,
            'content_start': content, 'source_stop': stop, 'text': text[content:stop].strip()})
    warnings = []
    if not headings:
        warnings.append('heading_not_found_check_page_images')
    if len(run) != len(all_labels):
        warnings.append('noncontiguous_labels_require_review')
    return {'heading_found': bool(headings), 'detected_count': len(entries), 'style': style, 'entries': entries,
            'warnings': warnings, 'heading_start': start, 'page_count': len(pages)}


def sample_numbers(count):
    if not count:
        return []
    selected = {1, max(1, count // 3), max(1, 2 * count // 3), count}
    for number in range(1, count + 1):
        if len(selected) >= min(4, count):
            break
        selected.add(number)
    return sorted(selected)


def extract(manifest, out):
    import pypdfium2 as pdfium
    stats = Counter()
    for paper in manifest['papers']:
        directory = out / 'sources' / paper['audit_id']
        if (directory / 'inventory.json').exists():
            continue
        path = ROOT / paper['pdf']
        if sha(path) != paper['sha256']:
            raise ValueError('Source PDF changed: ' + paper['pdf'])
        directory.mkdir(parents=True, exist_ok=True)
        with pdfium.PdfDocument(str(path)) as doc:
            pages = []
            for index in range(len(doc)):
                page = doc[index]
                textpage = page.get_textpage()
                value = textpage.get_text_range()
                textpage.close()
                page.close()
                pages.append((index + 1, value))
                (directory / f'page-{index + 1:03}.txt').write_text(value, encoding='utf-8')
            inventory = source_inventory(pages)
            selected = sample_numbers(inventory['detected_count'])
            inventory.update(audit_id=paper['audit_id'], pdf=paper['pdf'], source_sha256=paper['sha256'],
                             navigation_sample=selected, detector_is_gold=False)
            if inventory['entries']:
                first = min(e['page'] for e in inventory['entries'])
                last = max(e['page'] for e in inventory['entries'])
                render_pages = sorted(set(range(first, last + 1)) | {len(doc)})
            else:
                render_pages = list(range(max(1, len(doc) - 1), len(doc) + 1))
            if len(render_pages) > 14:
                raise ValueError('Unexpectedly long reference span: inspect before rendering')
            inventory['rendered_pages'] = render_pages
            for page_number in render_pages:
                page = doc[page_number - 1]
                page.render(scale=1.7).to_pil().save(directory / f'page-{page_number:03}.png')
                page.close()
            save(directory / 'inventory.json', inventory)
        stats['papers'] += 1
        stats['rendered_pages'] += len(render_pages)
        if int(paper['audit_id'][1:]) % 10 == 0:
            print(json.dumps({'prepared': paper['audit_id'], 'counts': dict(stats)}), flush=True)
    complete = sum((out / 'sources' / p['audit_id'] / 'inventory.json').exists() for p in manifest['papers'])
    save(out / 'source-preparation.json', {'status': 'complete' if complete == manifest['count'] else 'incomplete',
        'papers_prepared': complete, 'count': manifest['count'], 'source_detector': VERSION,
        'note': 'Source navigation only. No parser predictions were read; gold review is still required.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'outputs/reference_bulk_500_author_fixed_v4_20261017')
    parser.add_argument('--out', type=Path, default=DEFAULT)
    parser.add_argument('--count', type=int, default=100)
    parser.add_argument('--seed', default='reference-audit-100-20261017-v1')
    parser.add_argument('--extract', action='store_true')
    parser.add_argument('--exclude-manifest', type=Path, action='append', default=[])
    args = parser.parse_args()
    baseline, out = args.baseline.resolve(), args.out.resolve()
    if out == baseline or out.is_relative_to(baseline) or baseline.is_relative_to(out):
        raise ValueError('Use a separate output folder')
    manifest = select(baseline, out, args.count, args.seed, args.exclude_manifest)
    print(json.dumps({'selected': manifest['count'], 'publishers': manifest['publisher_quotas'],
                      'previously_reviewed_excluded': len(manifest['excluded_in_500'])}), flush=True)
    if args.extract:
        extract(manifest, out)


if __name__ == '__main__':
    main()
