"""Read-only identity/PDF/favorite/reference snapshots for metadata maintenance."""
import gzip
import hashlib
import json
from pathlib import Path

from scripts.metadata_enrichment import PROTECTED, write_json


def capture(conn, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with conn.cursor() as cur:
        cur.execute('SELECT ' + ','.join(PROTECTED) + ' FROM papers ORDER BY id')
        rows = cur.fetchall()
        cur.execute("SELECT TABLE_NAME,COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                    "WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME='papers'")
        refs = {}
        for table, column in cur.fetchall():
            refs.setdefault(table, []).append(column)
        fingerprints = {}
        for table, columns in sorted(refs.items()):
            cur.execute('SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE '
                        'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND CONSTRAINT_NAME=%s '
                        'ORDER BY ORDINAL_POSITION', (table, 'PRIMARY'))
            cols = list(dict.fromkeys([x[0] for x in cur.fetchall()] + columns))
            select = ','.join('`' + c.replace('`', '``') + '`' for c in cols)
            cur.execute('SELECT ' + select + ' FROM `' + table.replace('`', '``') + '` ORDER BY ' + select)
            data = cur.fetchall()
            fingerprints[table] = {'columns': cols, 'count': len(data),
                                   'sha256': hashlib.sha256(json.dumps(data, default=str).encode()).hexdigest()}
        cur.execute("SELECT COUNT(*),SUM(doi IS NULL OR TRIM(doi)='') FROM papers")
        count, blank = cur.fetchone()
    with gzip.open(directory / 'protected.json.gz', 'wt', encoding='utf-8') as handle:
        json.dump({'fields': PROTECTED, 'rows': rows}, handle, ensure_ascii=False, default=str)
    summary = {'paper_rows': count, 'doi_blank': int(blank), 'references': fingerprints}
    write_json(directory / 'summary.json', summary)
    conn.commit()
    return summary


def compare(before, after):
    def load(path):
        with gzip.open(Path(path) / 'protected.json.gz', 'rt', encoding='utf-8') as handle:
            return {r[0]: r for r in json.load(handle)['rows']}
    old, new = load(before), load(after)
    old_summary = json.loads((Path(before) / 'summary.json').read_text(encoding='utf-8'))
    new_summary = json.loads((Path(after) / 'summary.json').read_text(encoding='utf-8'))
    return {'old_rows': len(old), 'new_rows': len(new), 'added_rows': len(new.keys() - old.keys()),
            'missing_original_ids': sorted(old.keys() - new.keys()),
            'changed_protected_ids': [key for key in old.keys() & new.keys() if old[key] != new[key]],
            'changed_references': [table for table, value in old_summary['references'].items()
                                   if value != new_summary['references'].get(table)]}
