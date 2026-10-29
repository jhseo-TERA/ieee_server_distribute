"""Independent structural/source checks of a bulk reference experiment.

Role warnings are review candidates, not automatic accuracy labels. This tool
only reads the experiment/PDF files and writes its own quality-review.json.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_server.parsers.references import FIELDS, digest_file, grounded, key
from paper_server.parsers.reference_normalization import apply_title_normalization, recover_from_pdf, VERSION as NORMALIZATION_VERSION
from paper_server.parsers.reference_authority import load_authorities, verified_author_resolution
from paper_server.parsers.reference_evidence import authenticated_sources
from paper_server.parsers.reference_roles import apply_roles
from paper_server.parsers.reference_source_reviews import apply_source_reviews, verified_source_review
from paper_server.parsers.reference_display import apply_display_formatting
from scripts.benchmark_reference_bulk import DEFAULT_OUT, now, read, save


def audit(out, verify_pdfs=False):
    authorities = load_authorities()
    manifest = read(out / 'manifest.json')
    issues, review, counts = [], [], Counter()
    for paper in manifest['papers']:
        directory = out / 'papers' / paper['id']
        prep_path, result_path = directory / 'prepared.json', directory / 'result.json'
        if verify_pdfs:
            try:
                if digest_file(ROOT / paper['pdf']) != paper['sha256']:
                    issues.append({'pdf': paper['pdf'], 'issue': 'source_hash_changed'})
            except OSError as exc:
                issues.append({'pdf': paper['pdf'], 'issue': 'source_unavailable', 'type': type(exc).__name__})
        if not result_path.exists() or not prep_path.exists():
            counts['pending_papers'] += 1
            continue
        prepared, result = read(prep_path), read(result_path)
        counts['processed_papers'] += 1
        entries, rows = prepared['entries'], result['rows']
        role_sources, role_expected = {}, []
        if any(row.get('role_version') for row in rows):
            try:
                role_sources=authenticated_sources(rows,paper.get('sha256'))
                role_expected=apply_roles([{**r,'author':None} for r in rows],role_sources)
            except (KeyError,OSError,ValueError) as exc:
                issues.append({'pdf':paper['pdf'],'issue':'invalid_role_source_evidence','type':type(exc).__name__})
        if prepared['status'] == 'ready':
            numbers = [r['number'] for r in entries]
            if numbers != list(range(1, len(entries) + 1)):
                issues.append({'pdf': paper['pdf'], 'issue': 'noncontiguous_numbers'})
            if len(rows) != len(entries):
                issues.append({'pdf': paper['pdf'], 'issue': 'result_count_mismatch'})
            if any(not 1 <= r['page'] <= prepared['scan']['page_count'] for r in entries):
                issues.append({'pdf': paper['pdf'], 'issue': 'invalid_original_page'})
        for index,(entry, row, regex, grobid) in enumerate(zip(entries, rows, result.get('regex', []), result.get('grobid', []))):
            counts['references'] += 1
            if any(entry.get(k) != row.get(k) for k in ('number', 'raw', 'page')):
                issues.append({'pdf': paper['pdf'], 'number': entry['number'], 'issue': 'source_identity_changed'})
            checked = grounded({**entry,'raw':role_sources.get(row['number'],entry)['raw']}, row, 'audit')
            original_checked = grounded(entry,row,'audit-original-source')
            # An independent extractor may lose a glyph the original retained.
            # Either authenticated source can support a field; neither can
            # support a fabricated value absent from both strings.
            checked['warnings']=[w for w in checked['warnings'] if w in original_checked['warnings']]
            role_verified=bool(role_expected and role_expected[index].get('author')==row.get('author'))
            source_reviewed=verified_source_review(row,paper.get('sha256'))
            if row.get('source_review_resolution') and not source_reviewed:
                issues.append({'pdf':paper['pdf'],'number':row['number'],'issue':'invalid_source_review'})
            if role_verified or source_reviewed and row['source_review_resolution']['field']=='author':
                checked['warnings']=[w for w in checked['warnings'] if w!='author_not_in_source']
            if checked['warnings']:
                issues.append({'pdf': paper['pdf'], 'number': entry['number'], 'issue': 'unsupported_final_fields',
                               'warnings': checked['warnings']})
            flags = []
            author_verified = verified_author_resolution(row, authorities)
            if row.get('author_resolution') and not author_verified:
                issues.append({'pdf': paper['pdf'], 'number': entry['number'], 'issue': 'invalid_author_resolution'})
            if author_verified:
                counts['verified_author_resolutions'] += 1
            if 'normalization_version' in row:
                alternate=row.get('source_view',{}).get('raw') if row.get('normalization_uses_source_view') else None
                if alternate is not None and row['number'] not in role_sources and alternate!=entry['raw']:
                    issues.append({'pdf':paper['pdf'],'number':row['number'],'issue':'unauthenticated_title_source'})
                    alternate=None
                normalized = apply_title_normalization(row,source_reference=alternate)
                if row.get('title_recovery_evidence'):
                    normalized = recover_from_pdf(normalized, row['title_recovery_evidence'],
                                                   expected_pdf_sha256=paper['sha256'])
                normalized=apply_source_reviews(normalized,paper.get('sha256'))
                if row.get('display_policy_version'):
                    normalized=apply_display_formatting(normalized)
                names = ('title_display', 'title_match_key', 'reference_type', 'normalization_steps',
                         'normalization_warnings', 'normalization_needs_review', 'normalization_status',
                         'normalization_source_sha256', 'needs_review', 'display_policy_version')
                if row['normalization_version'] != NORMALIZATION_VERSION or any(row.get(k) != normalized.get(k) for k in names):
                    issues.append({'pdf': paper['pdf'], 'number': entry['number'], 'issue': 'invalid_title_normalization'})
                if row.get('normalization_needs_review'):
                    flags.extend(row.get('normalization_warnings', []))
            embedded = [int(n) for n in re.findall(r'\[\s*(\d{1,3})\s*\]', entry['raw'])]
            if len(embedded) >= 2 and any(b == a + 1 for a, b in zip(embedded, embedded[1:])):
                flags.append('collapsed_reference_labels')
                issues.append({'pdf': paper['pdf'], 'number': entry['number'],
                               'issue': 'collapsed_reference_labels', 'embedded_numbers': embedded})
            if len(row.get('title') or '') > 400:
                flags.append('possible_body_or_figure_leak')
            if (regex.get('author') and key(regex['author']) == key(grobid.get('author'))
                    and key(row.get('author')) != key(regex['author']) and not author_verified and not role_verified and not source_reviewed):
                flags.append('agreed_author_changed_or_removed')
            if row.get('title') and row.get('journal') and key(row['title']) == key(row['journal']):
                flags.append('title_equals_venue')
            if len(entry['raw']) >= 5000:
                flags.append('source_at_truncation_bound')
            if row.get('llm_status') == 'failed':
                flags.append('llm_failed')
            if flags:
                review.append({'pdf': paper['pdf'], 'id': paper['id'], 'number': entry['number'],
                    'page': entry['page'], 'flags': flags, 'raw': entry['raw'],
                    'regex': {f: regex.get(f) for f in FIELDS}, 'grobid': {f: grobid.get(f) for f in FIELDS},
                    'final': {f: row.get(f) for f in FIELDS}})
                counts.update(flags)
    result = {'created_at': now(), 'pdf_hashes_checked': verify_pdfs, 'counts': dict(counts),
              'structural_issues': issues, 'role_review_candidates': review,
              'note': 'Role warnings require source review. Source grounding alone is not bibliographic accuracy.'}
    save(out / 'quality-review.json', result)
    print(json.dumps({'counts': dict(counts), 'structural_issues': len(issues)}, ensure_ascii=True))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--verify-pdfs', action='store_true')
    args = parser.parse_args()
    result = audit(args.out.resolve(), args.verify_pdfs)
    return 1 if result['structural_issues'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
