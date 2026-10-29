"""Draft-assisted, source-only review and separate frozen-prediction scoring.

Drafts are NOT gold. The reviewing assistant must inspect the source text and
page images, adjudicate fields/count, and explicitly approve each paper.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_reference_validation import DEFAULT, read, save, sha, sample_numbers


def compact(value):
    value = unicodedata.normalize('NFKC', str(value or ''))
    value = re.sub(r'(?<=[A-Za-z])[\ufffe\u00ad]\s*(?=[A-Za-z])', '', value)
    return re.sub(r'\s+', ' ', value).strip()


def draft_reference(entry):
    raw = entry['text'][:1500]
    value = compact(raw)
    quoted = re.search(r'[“”"]([^“”"]+)[“”"]', value)
    first = re.split(r'\band\b', value.split(',')[0])[0]
    first = re.sub(r'\bet\s+al\.?', '', first, flags=re.I).strip()
    author = re.sub(r'^(?:[A-Z]\.-?\s*)+', '', first).strip()
    if author == first:
        author = first.split()[-1] if first.split() else None
    years = re.findall(r'\b(?:18|19|20)\d{2}\b', value[quoted.end():] if quoted else value)
    doi = re.search(r'10\.\d{4,9}/\S+', value)
    return {'number': entry['number'], 'title': quoted.group(1).rstrip(' ,;') if quoted else None,
        'author': author, 'year': int(years[0]) if years else None,
        'doi': doi.group().rstrip('.,;') if doi else None, 'source_text': raw, 'page': entry['page']}


def drafts(out):
    for paper in read(out / 'manifest.json')['papers']:
        inventory_path = out / 'sources' / paper['audit_id'] / 'inventory.json'
        inv = read(inventory_path)
        selected = sample_numbers(inv['detected_count'])
        save(out / 'drafts' / (paper['audit_id'] + '.json'), {'audit_id': paper['audit_id'], 'pdf': paper['pdf'],
            'count_candidate': inv['detected_count'], 'navigation_sample': selected,
            'references': [draft_reference(e) for e in inv['entries'] if e['number'] in selected],
            'inventory_sha256': sha(inventory_path), 'reviewed': False})


def approve(out, audit_id, count, edits=None, images=None, notes='', references=None):
    path = out / 'gold' / (audit_id + '.json')
    if path.exists():
        raise FileExistsError('Reviewed gold already exists: ' + audit_id)
    draft = read(out / 'drafts' / (audit_id + '.json'))
    refs = references if references is not None else draft['references']
    refs = json.loads(json.dumps(refs))
    for row in refs:
        row.update((edits or {}).get(str(row['number']), {}))
    if sorted(r['number'] for r in refs) != sample_numbers(count):
        raise ValueError('Reviewed reference selection differs from the fixed sampling rule')
    if not images or any(not (out / 'sources' / audit_id / name).exists() for name in images):
        raise ValueError('Source image evidence required')
    result = {**draft, 'expected_count': count, 'references': refs, 'reviewed': True,
        'reviewed_at': datetime.now(timezone.utc).isoformat(), 'source_images_viewed': images,
        'review_method': 'Assistant source review of PDF text plus page images; draft assisted, not human-certified gold; predictions withheld until comparison.',
        'review_notes': notes, 'draft_sha256': sha(out / 'drafts' / (audit_id + '.json'))}
    save(path, result)


def field_key(value, field):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if field == 'year':
        return value
    value = unicodedata.normalize('NFKD', str(value)).casefold()
    if field == 'doi':
        return re.sub(r'\s+', '', value).removeprefix('https://doi.org/').removeprefix('doi:').rstrip('.,;')
    value = value.translate(str.maketrans({'−': '-', '–': '-', '—': '-'}))
    if field == 'title':
        value = value.replace('×', '*')
        value = re.sub(r'(?<=\d)\s*x\s*(?=\d)', '*', value)
        # A missing space after the preceding data-rate unit does not turn a
        # negative sensitivity into a positive one.
        value = re.sub(r'(?<=[/s])-(?=\d+(?:\.\d+)?\s*-?\s*dbm)', ' -', value)
        # Ignore typography/line hyphens, but retain scientific comparisons,
        # multiplication, roots, exponents, and explicit numeric signs.
        return ''.join(c for i, c in enumerate(value) if c.isalnum() or c in '<>+=±*√^'
            or c == '-' and i + 1 < len(value) and value[i+1].isdigit()
            and (i == 0 or not value[i-1].isalnum() or value[i-1].isdigit()))
    return ''.join(c for c in value if c.isalnum())


def validate_gold(out, paper):
    path = out / 'gold' / (paper['audit_id'] + '.json')
    gold = read(path)
    if gold.get('reviewed') is not True or gold.get('audit_id') != paper['audit_id'] or gold.get('pdf') != paper['pdf']:
        raise ValueError('Unreviewed or mismatched gold: ' + paper['audit_id'])
    count = gold['expected_count']
    if type(count) is not int or count < 0 or sorted(r['number'] for r in gold['references']) != sample_numbers(count):
        raise ValueError('Invalid reviewed reference selection')
    for row in gold['references']:
        if any(field not in row for field in ('title', 'author', 'year', 'doi')) and not row.get('unscored_reason'):
            raise ValueError('Unscored field requires an explicit reason')
    if not gold.get('source_images_viewed'):
        raise ValueError('Source image review evidence required')
    for name in gold['source_images_viewed']:
        if not (out / 'sources' / paper['audit_id'] / name).is_file():
            raise ValueError('Source image missing')
    return gold


def freeze_gold(out):
    manifest = read(out / 'manifest.json')
    hashes = {}
    for paper in manifest['papers']:
        gold = validate_gold(out, paper)
        paths = [out / 'gold' / (paper['audit_id'] + '.json')]
        paths += [out / 'sources' / paper['audit_id'] / name for name in gold['source_images_viewed']]
        for path in paths:
            hashes[path.relative_to(out).as_posix()] = sha(path)
    result = {'created_at': datetime.now(timezone.utc).isoformat(), 'manifest_sha256': sha(out / 'manifest.json'),
        'scorer_sha256': sha(Path(__file__)), 'files': hashes,
        'policy': 'Bibliography count and fixed up-to-four references per paper. Title, first-author family name, publication year, and printed DOI. Missing rows fail even null fields; justified omitted fields are unscored. Typography/case ignored; explicit math operators retained.'}
    path = out / 'gold-freeze.json'
    if path.exists():
        existing = read(path)
        if any(existing[key] != result[key] for key in ('manifest_sha256', 'scorer_sha256', 'files')):
            raise ValueError('Frozen gold/evidence/scorer changed')
        return existing
    save(path, result)
    return result


def score(out):
    manifest = read(out / 'manifest.json')
    fields, errors, paper_results, exclusions = {}, [], [], []
    missing = [p['audit_id'] for p in manifest['papers'] if not (out / 'gold' / (p['audit_id'] + '.json')).exists()]
    if missing:
        raise ValueError('Review incomplete: ' + ', '.join(missing))
    if not (out / 'gold-freeze.json').is_file():
        raise ValueError('Freeze reviewed gold before comparing predictions')
    frozen = freeze_gold(out)
    for name, digest in manifest.get('pipeline_code_sha256', {}).items():
        if sha(ROOT / name) != digest:
            raise ValueError('Pipeline code changed during validation')
    for paper in manifest['papers']:
        gold = validate_gold(out, paper)
        prediction_path = out / 'predictions' / (paper['id'] + '.json')
        if sha(prediction_path) != paper['prediction_sha256'] or sha(ROOT / paper['pdf']) != paper['sha256']:
            raise ValueError('Frozen prediction/source changed')
        result = read(prediction_path)
        actual = {row['number']: row for row in result['rows']}
        paper_errors = []
        for row in gold['references']:
            found = actual.get(row['number'])
            for field in ('title', 'author', 'year', 'doi'):
                if field not in row:
                    exclusions.append({'audit_id': paper['audit_id'], 'number': row['number'], 'field': field,
                                       'reason': row.get('unscored_reason')})
                    continue
                value = (found.get('title_display', found.get('title')) if field == 'title' else found.get(field)) if found else None
                correct = found is not None and field_key(value, field) == field_key(row[field], field)
                item = fields.setdefault(field, Counter())
                item['checked'] += 1
                item['errors'] += not correct
                presence = 'absent' if row[field] is None else 'present'
                item[presence + '_checked'] += 1
                item[presence + '_errors'] += not correct
                if not correct:
                    error = {'audit_id': paper['audit_id'], 'pdf': paper['pdf'], 'number': row['number'],
                             'field': field, 'expected': row[field], 'actual': value, 'row_missing': found is None}
                    errors.append(error)
                    paper_errors.append(error)
        count_match = len(result['rows']) == gold['expected_count']
        sequence_match = sorted(r['number'] for r in result['rows']) == list(range(1, gold['expected_count'] + 1))
        paper_results.append({'audit_id': paper['audit_id'], 'pdf': paper['pdf'], 'publisher': paper['publisher'],
            'expected_count': gold['expected_count'], 'actual_count': len(result['rows']), 'count_match': count_match,
            'number_sequence_match': sequence_match,
            'fields_checked': sum(field in r for r in gold['references'] for field in ('title','author','year','doi')),
            'field_errors': len(paper_errors), 'status': result.get('status'),
            'gold_sha256': sha(out / 'gold' / (paper['audit_id'] + '.json'))})
    for item in fields.values():
        item['error_rate_percent'] = round(100 * item['errors'] / item['checked'], 3) if item['checked'] else None
    checked = sum(f['checked'] for f in fields.values())
    report = {'status': 'complete', 'papers_reviewed': len(paper_results), 'created_at': datetime.now(timezone.utc).isoformat(),
        'references_expected': sum(p['expected_count'] for p in paper_results),
        'references_extracted': sum(p['actual_count'] for p in paper_results),
        'references_field_sample': sum(len(read(out / 'gold' / (p['audit_id'] + '.json'))['references']) for p in manifest['papers']),
        'fields_checked': checked, 'field_errors': len(errors), 'field_error_rate_percent': round(100 * len(errors) / checked, 3),
        'fields': fields, 'count_exact_papers': sum(p['count_match'] for p in paper_results),
        'papers_with_detected_error': sum(not p['count_match'] or not p['number_sequence_match'] or p['field_errors'] > 0 for p in paper_results),
        'gold_freeze_sha256': sha(out / 'gold-freeze.json'), 'scoring_policy': frozen['policy'],
        'papers': paper_results, 'errors': errors, 'unscored_fields': exclusions,
        'limitations': ['Fresh source review of a fixed 100-paper sample from the existing 500.',
            'Field accuracy covers at most four selected references per paper; counts cover the bibliography.',
            'Assistant draft-assisted source review, not an independent human-certified dataset.',
            'Absence fields and missing predicted references are included explicitly; DOI absence is not DOI recall.',
            'No pipeline tuning during the review; no SQL writes or model/API inference.']}
    save(out / 'validation-summary.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('papers','errors','unscored_fields')}, ensure_ascii=True), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT)
    parser.add_argument('--drafts', action='store_true')
    parser.add_argument('--score', action='store_true')
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--show', nargs=2, type=int, metavar=('FIRST', 'LAST'))
    args = parser.parse_args()
    if args.drafts:
        drafts(args.out)
    if args.show:
        for index in range(args.show[0], args.show[1] + 1):
            item = read(args.out / 'drafts' / f'p{index:03}.json')
            for row in item['references']:
                row['source_text'] = row['source_text'][:650]
            print(json.dumps(item, ensure_ascii=True))
    if args.freeze:
        frozen = freeze_gold(args.out)
        print('Reviewed gold/evidence frozen:', len(frozen['files']), 'files')
    if args.score:
        score(args.out)


if __name__ == '__main__':
    main()
