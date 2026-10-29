"""Replay title postprocessing over immutable bulk caches, without inference/SQL."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.parsers.reference_normalization import VERSION, apply_title_normalization
from paper_server.parsers.reference_title_recovery import recover_titles
from paper_server.parsers.references import key
from paper_server.parsers.reference_authority import (
    VERSION as AUTHOR_VERSION, REGISTRY, apply_author_resolution, load_authorities, verified_author_resolution,
)
from scripts.benchmark_reference_bulk import DEFAULT_OUT, read, save, now
from scripts.audit_reference_bulk import audit


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def score(rows_by_pdf, gold):
    fields, mismatches, counts = {}, [], []
    for paper in gold['papers']:
        rows = rows_by_pdf[paper['pdf']]
        counts.append({'pdf': paper['pdf'], 'expected': paper['expected_count'], 'actual': len(rows)})
        by_number = {r['number']: r for r in rows}
        for expected in paper['gold']:
            for name in ('title', 'author', 'year', 'doi'):
                if name not in expected:
                    continue
                row = by_number.get(expected['number'])
                actual = (row.get('title_display', row.get('title')) if name == 'title' else row.get(name)) if row else None
                good = row is not None and key(actual) == key(expected[name])
                item = fields.setdefault(name, {'correct': 0, 'total': 0})
                item['total'] += 1
                item['correct'] += int(good)
                if not good:
                    mismatches.append({'pdf': paper['pdf'], 'number': expected['number'], 'field': name,
                                       'expected': expected[name], 'actual': actual})
    return {'fields': fields, 'correct': sum(f['correct'] for f in fields.values()),
            'total': sum(f['total'] for f in fields.values()), 'mismatches': mismatches,
            'paper_counts': counts, 'count_exact_papers': sum(p['expected'] == p['actual'] for p in counts)}


def run(baseline, out, resolve_authors=False):
    baseline, out = baseline.resolve(), out.resolve()
    if out == baseline or out.is_relative_to(baseline) or baseline.is_relative_to(out):
        raise ValueError('Use a separate sibling output directory; the baseline must stay immutable')
    manifest = read(baseline / 'manifest.json')
    completion = baseline / 'progress.json'
    if not completion.exists():
        completion = baseline / 'revalidation-summary.json'
    if read(completion)['status'] != 'complete':
        raise ValueError('Replay requires a complete baseline batch')
    if out.exists():
        raise FileExistsError('Output directory already exists; select a new output path')
    out.mkdir(parents=True)
    started = time.monotonic()
    artifacts = ['manifest.json', 'summary.json', 'progress.json', 'final-verification.json', 'holdout-score.json',
                 'holdout-audit-gold.json', 'manual-audit-gold.json', 'llm-settings.json', 'quality-review.json',
                 'revalidation-summary.json', 'holdout-comparison.json', 'known-case-comparison.json',
                 'baseline-integrity.json', 'normalization-code-snapshot.json', 'REVALIDATION_REPORT.txt']
    original_hashes = {name: digest(baseline / name) for name in artifacts if (baseline / name).exists()}
    for name in ('manifest.json', 'holdout-audit-gold.json', 'manual-audit-gold.json'):
        shutil.copy2(baseline / name, out / name)
    before, after, changes, diagnostics, author_changes = {}, {}, [], [], []
    authorities = load_authorities() if resolve_authors else []
    invariants, counts = [], Counter()
    for paper in manifest['papers']:
        relative = Path('papers') / paper['id']
        source = baseline / relative / 'result.json'
        original_hashes[(relative / 'result.json').as_posix()] = digest(source)
        original_hashes[(relative / 'prepared.json').as_posix()] = digest(baseline / relative / 'prepared.json')
        original = read(source)
        result = deepcopy(original)
        result['rows'] = [apply_title_normalization(row) for row in original['rows']]
        rows, recovery = recover_titles(result['rows'], ROOT / paper['pdf'], paper['sha256'], out / 'title-pdf-evidence')
        if resolve_authors:
            rows = [apply_author_resolution(row, authorities) for row in rows]
            result['author_resolution_provenance'] = {'version': AUTHOR_VERSION, 'registry_sha256': digest(REGISTRY),
                'code_sha256': digest(ROOT / 'paper_server/parsers/reference_authority.py')}
        result['rows'], result['title_recovery'] = rows, recovery
        result['normalization_provenance'] = {'version': VERSION, 'baseline_result_sha256': digest(source),
            'normalizer_sha256': digest(ROOT / 'paper_server/parsers/reference_normalization.py'),
            'recovery_adapter_sha256': digest(ROOT / 'paper_server/parsers/reference_title_recovery.py'),
            'pdf_worker_sha256': digest(ROOT / 'scripts/reference_title_pdf_worker.py')}
        before[paper['pdf']], after[paper['pdf']] = original['rows'], rows
        counts['papers'] += 1
        counts['references'] += len(rows)
        counts['pdf_recovery_requested'] += recovery['requested']
        counts['pdf_recovered'] += recovery['recovered']
        counts['normalization_unresolved'] += sum(bool(r['normalization_needs_review']) for r in rows)
        if recovery.get('error'):
            diagnostics.append({'pdf': paper['pdf'], **recovery})
        for old, new in zip(original['rows'], rows):
            # Only an explicitly requested, verified surname correction may
            # change an old field. Raw source and all other fields stay fixed.
            author_changed = old.get('author') != new.get('author')
            permitted_author = (resolve_authors and verified_author_resolution(new, authorities)
                and new['author_resolution']['before'] == old.get('author'))
            if any(new.get(k) != v and not (k == 'author' and permitted_author) for k, v in old.items()):
                invariants.append({'pdf': paper['pdf'], 'number': old['number'], 'issue': 'original_field_changed'})
            if author_changed:
                author_changes.append({'pdf': paper['pdf'], 'number': old['number'], 'page': old['page'],
                    'before': old.get('author'), 'after': new.get('author'), 'evidence': new.get('author_resolution')})
            counts['author_corrections'] += int(author_changed)
            counts['normalization_' + new['normalization_status']] += 1
            old_display = old.get('title_display', old.get('title'))
            if re.search(r'\[(?:J|C)\]\s*$', old_display or ''):
                counts['type_tags_before'] += 1
            if re.search(r'\[(?:J|C)\]\s*$', new.get('title_display') or ''):
                counts['type_tags_after'] += 1
            for target, value in [('tex_before', old_display), ('tex_after', new.get('title_display'))]:
                if re.search(r'\$|\\[A-Za-z]+|[_^]\s*\{', value or ''):
                    counts[target] += 1
            if old_display != new.get('title_display'):
                changes.append({'pdf': paper['pdf'], 'number': old['number'], 'page': old['page'],
                    'source_title': old_display, 'title_display': new.get('title_display'),
                    'steps': new['normalization_steps'], 'warnings': new['normalization_warnings']})
        target = out / relative
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(baseline / relative / 'prepared.json', target / 'prepared.json')
        save(target / 'result.json', result)
    source_changes = [name for name, sha in original_hashes.items() if digest(baseline / name) != sha]
    save(out / 'baseline-integrity.json', {'baseline': str(baseline), 'files_checked': len(original_hashes),
         'changed_files': source_changes, 'sha256': original_hashes})
    gold = read(baseline / 'holdout-audit-gold.json')
    comparison = {'before': score(before, gold), 'after': score(after, gold),
        'note': 'Existing 8-paper IEEE fixture reused for regression after error analysis; not a fresh accuracy estimate.'}
    save(out / 'holdout-comparison.json', comparison)
    fixture_path = ROOT / 'config/reference_normalization_cases.json'
    fixture = read(fixture_path)
    case_mismatches = []
    for case in fixture['cases']:
        row = next((r for r in after[case['pdf']] if r['number'] == case['row']['number']), {})
        if row.get('title_display') != case['expected_display'] or row.get('normalization_needs_review'):
            case_mismatches.append({'pdf': case['pdf'], 'number': case['row']['number'],
                'expected': case['expected_display'], 'actual': row.get('title_display')})
    known_cases = {'checked': len(fixture['cases']), 'correct': len(fixture['cases']) - len(case_mismatches),
        'mismatches': case_mismatches, 'fixture_sha256': digest(fixture_path), 'scope': fixture['scope']}
    save(out / 'known-case-comparison.json', known_cases)
    quality = audit(out, verify_pdfs=True)
    for name in ('type_tags_before', 'type_tags_after', 'tex_before', 'tex_after'):
        counts.setdefault(name, 0)
    failed = bool(invariants or source_changes or diagnostics or counts['normalization_unresolved'] or case_mismatches)
    # Previously known extraction problems are reported separately; fail on
    # any additional structural/source problem introduced by this replay.
    previous_issues = read(baseline / 'quality-review.json')['structural_issues']
    new_issues = [issue for issue in quality['structural_issues'] if issue not in previous_issues]
    failed = failed or bool(new_issues)
    report = {'status': 'needs_attention' if failed else 'complete', 'finished_at': now(), 'elapsed_seconds': round(time.monotonic() - started, 3),
        'normalization_version': VERSION, 'counts': dict(counts), 'changes': changes,
        'author_resolution_enabled': resolve_authors, 'author_changes': author_changes,
        'original_field_changes': invariants, 'baseline_file_changes': source_changes, 'recovery_errors': diagnostics,
        'holdout_before': {k:v for k,v in comparison['before'].items() if k not in ('mismatches','paper_counts')},
        'holdout_after': {k:v for k,v in comparison['after'].items() if k not in ('mismatches','paper_counts')},
        'structural_issues': quality['structural_issues'],
        'new_structural_issues': new_issues,
        'known_case_regression': known_cases,
        'new_inference_calls': 0, 'external_api_calls': 0, 'sql_writes': 0,
        'note': 'Experimental postprocessing. Reviewed publisher evidence may resolve surnames when explicitly enabled; extraction failures remain reviewable.'}
    save(out / 'revalidation-summary.json', report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('changes','structural_issues')}, ensure_ascii=True), flush=True)
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--out', type=Path, default=ROOT / 'outputs/reference_bulk_500_normalized_v3_20261017')
    parser.add_argument('--resolve-authors', action='store_true', help='Apply pinned publisher surname evidence, preserving the previous candidate')
    args = parser.parse_args()
    return run(args.baseline, args.out, args.resolve_authors)


if __name__ == '__main__':
    raise SystemExit(main())
