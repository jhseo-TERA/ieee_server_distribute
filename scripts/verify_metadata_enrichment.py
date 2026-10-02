"""Read-only final integrity and identifier verification for an enrichment run."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.metadata_enrichment import EDITABLE, INSERT_FIELDS, write_json
from scripts.metadata_integrity import capture, compare


def load_audited_expectations(run):
    """Replay committed inserts and field patches in audit write order.

    Current audit records have no committed_at field. Their final atomic write
    follows the database commit, so use the file modification time (nanoseconds)
    and a deterministic path tie-breaker. Retain this ordering in the report.
    """
    run = Path(run)
    paths = list(run.rglob('changes.json')) + list(run.rglob('batch_*.json'))
    paths.sort(key=lambda path: (path.stat().st_mtime_ns, str(path.relative_to(run))))
    expected, inserted_keys, patched_keys = {}, set(), set()
    uncommitted_inserts, uncommitted_patches, ordered_audits = [], [], []
    patched_field_updates = 0
    for path in paths:
        record = json.loads(path.read_text(encoding='utf-8'))
        is_patch = path.name.startswith('batch_')
        if record.get('status') != 'committed':
            (uncommitted_patches if is_patch else uncommitted_inserts).append(str(path))
            continue
        ordered_audits.append({'path': str(path.relative_to(run)),
                               'modified_ns': path.stat().st_mtime_ns,
                               'kind': 'patch' if is_patch else 'insert'})
        if is_patch:
            for change in record['changes']:
                key = change['article_number']
                if not change['new'] or set(change['new']) - EDITABLE:
                    raise ValueError(f'Invalid audited patch fields: {path}')
                row = expected.setdefault(key, {'id': change['id'], 'article_number': key})
                if row['id'] != change['id']:
                    raise ValueError(f'Audited paper identity changed: {key}')
                row.update(change['new'])
                patched_keys.add(key)
                patched_field_updates += len(change['new'])
        else:
            by_key = {row['article_number']: row for row in record['new_records']}
            for identity in record['inserted']:
                key = identity['article_number']
                candidate = by_key[key]
                if key in expected and expected[key]['id'] != identity['id']:
                    raise ValueError(f'Audited paper identity changed: {key}')
                expected[key] = {field: candidate.get(field) for field in INSERT_FIELDS}
                expected[key]['id'] = identity['id']
                inserted_keys.add(key)
    return {
        'expected': expected,
        'inserted_keys': inserted_keys,
        'patched_keys': patched_keys,
        'patched_field_updates': patched_field_updates,
        'uncommitted_insert_audits': uncommitted_inserts,
        'uncommitted_patch_audits': uncommitted_patches,
        'audit_order_basis': 'file_modified_time_ns_then_relative_path',
        'ordered_audits': ordered_audits,
    }


def compare_expected_rows(expected, actual):
    """Report document keys and field names, without printing field contents."""
    mismatches = []
    for key, fields in expected.items():
        found = actual.get(key)
        changed = (['row_missing'] if found is None else
                   sorted(field for field, value in fields.items() if found.get(field) != value))
        if changed:
            mismatches.append({'article_number': key, 'fields': changed})
    return mismatches


def main():
    import pymysql
    from scripts.import_excel_to_db import DB
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    audited = load_audited_expectations(args.run)
    expected = audited['expected']
    invalid_audits = audited['uncommitted_insert_audits'] + audited['uncommitted_patch_audits']
    with pymysql.connect(**DB) as conn:
        summary = capture(conn, args.output)
        integrity = compare(args.run / 'before', args.output)
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute('SELECT id,article_number,title,doi,source_name,year,citation_source '
                        'FROM papers WHERE article_number IN (%s,%s,%s)', ('5872030','8877951','8902616'))
            key_papers = cur.fetchall()
            cur.execute("SELECT source_name,COUNT(*) n FROM papers WHERE doi IS NULL OR TRIM(doi)='' GROUP BY source_name")
            blanks = cur.fetchall()
            cur.execute("SELECT article_number,title,doi,source_name FROM papers WHERE title LIKE 'Recovered IEEE document%%' "
                        "OR title LIKE 'Paper Title%%' OR title REGEXP '\\.(indd|docx?)$'")
            placeholders = cur.fetchall()
            cur.execute("SELECT LOWER(doi) doi,COUNT(*) n FROM papers WHERE doi IS NOT NULL AND TRIM(doi)<>'' "
                        'GROUP BY LOWER(doi) HAVING COUNT(*)>1')
            duplicates = cur.fetchall()
            cur.execute('SELECT source_name,COUNT(*) n,SUM(pdf_available=1) pdfs FROM papers GROUP BY source_name')
            sources = cur.fetchall()
            fields = ('title', 'authors', 'year', 'source_name', 'source_type', 'url', 'doi', 'issue')
            cur.execute('SELECT ' + ','.join(
                f"SUM(`{field}` IS NULL OR TRIM(`{field}`)='') AS `{field}`" for field in fields
            ) + ' FROM papers')
            blank_fields = {field: int(count or 0) for field, count in cur.fetchone().items()}
            mismatches = []
            keys = list(expected)
            columns = ('id', *INSERT_FIELDS)
            for offset in range(0, len(keys), 1000):
                chunk = keys[offset:offset+1000]
                cur.execute('SELECT ' + ','.join(columns) + ' FROM papers WHERE article_number IN ('
                            + ','.join(['%s']*len(chunk)) + ')', chunk)
                actual = {r['article_number']:r for r in cur.fetchall()}
                mismatches.extend(compare_expected_rows({key: expected[key] for key in chunk}, actual))
    result = {'summary': summary, 'integrity': integrity,
              'inserted_audit_rows': len(audited['inserted_keys']),
              'patched_audit_rows': len(audited['patched_keys']),
              'patched_field_updates': audited['patched_field_updates'],
              'verified_audit_rows': len(expected),
              'metadata_mismatch_count': len(mismatches),
              'metadata_mismatches': mismatches,
              'metadata_mismatch_samples': mismatches[:20],
              'inserted_mismatches': [r['article_number'] for r in mismatches if r['article_number'] in audited['inserted_keys']],
              'patched_mismatches': [r['article_number'] for r in mismatches if r['article_number'] in audited['patched_keys']],
              'uncommitted_audits': invalid_audits,
              'uncommitted_insert_audits': audited['uncommitted_insert_audits'],
              'uncommitted_patch_audits': audited['uncommitted_patch_audits'],
              'audit_order_basis': audited['audit_order_basis'],
              'ordered_audits': audited['ordered_audits'],
              'key_papers': key_papers, 'doi_blanks_by_source': blanks, 'remaining_placeholder_titles': placeholders,
              'duplicate_dois': duplicates, 'sources': sources, 'blank_fields': blank_fields}
    write_json(args.output/'verification.json', result)
    print(json.dumps({k:v for k,v in result.items() if k not in (
        'summary', 'sources', 'ordered_audits', 'metadata_mismatches',
        'inserted_mismatches', 'patched_mismatches',
    )}, ensure_ascii=False, default=str))
    assert not mismatches and not invalid_audits and not duplicates
    assert not integrity['missing_original_ids'] and not integrity['changed_protected_ids'] and not integrity['changed_references']


if __name__ == '__main__':
    main()
