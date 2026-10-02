#!/usr/bin/env python
"""Exercise real MySQL approval SQL and always roll back the transaction.

Only existing, complete extraction jobs with valid pending proposals are used.
No visual confirmation is supplied, no schema migration runs, and no commit is
available to the repository adapter. Fresh-connection checks verify that every
selected proposal and touched SerDes table returned to its original state.

InnoDB AUTO_INCREMENT counters may advance despite rollback, leaving harmless
ID gaps. This script guarantees row/state rollback, not gapless sequences.

Examples:
  python scripts/verify_ai_approval_rollback.py --proposal-ids 12 13
  python scripts/verify_ai_approval_rollback.py --job-id <completed-extract-uuid>
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys

from dotenv import load_dotenv
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_ai_repository import LocalAIRepository, approval_groups, json_value


SERDES_TABLES = (
    'serdes_implementations', 'serdes_implementation_papers',
    'serdes_measurements', 'serdes_measurement_evidence',
    'serdes_measurement_metric_scopes',
)
TOUCHED_TABLES = SERDES_TABLES + ('ai_proposals',)
REQUIRED_TRANSACTIONAL_TABLES = TOUCHED_TABLES + ('ai_jobs', 'papers')
PROPOSAL_SELECT = '''SELECT q.*,p.article_number,p.title,p.year,p.source_name,
    p.source_system,p.pdf_available,p.pdf_local_path,j.model,j.status job_status,j.kind job_kind
    FROM ai_proposals q JOIN papers p ON p.id=q.paper_id JOIN ai_jobs j ON j.id=q.job_id'''


class GuardedConnection:
    """Minimal execute-only interface: no commit, DDL, or external operations."""
    def __init__(self, connection):
        self._connection = connection

    def execute(self, statement, parameters=None):
        sql = str(statement).strip()
        if sql.upper().startswith('SELECT '):
            pass
        elif match := re.match(r'INSERT\s+INTO\s+([a-z_]+)\s*\(', sql, re.I):
            if match.group(1).lower() not in SERDES_TABLES:
                raise RuntimeError('Rollback verifier blocked an unexpected insert target')
        elif not re.match(r'UPDATE\s+ai_proposals\s+SET\b', sql, re.I):
            raise RuntimeError('Rollback verifier blocked an unexpected statement')
        if ';' in sql:
            raise RuntimeError('Rollback verifier does not allow multi-statements')
        return self._connection.execute(statement, parameters or {})


class RollbackOnlyEngine:
    """Reuse a caller-owned outer transaction, with no implicit commit on exit."""
    def __init__(self, connection):
        self._guarded = GuardedConnection(connection)

    @contextmanager
    def begin(self):
        yield self._guarded


def table_counts(connection):
    return {table: int(connection.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one())
            for table in TOUCHED_TABLES}


def proposal_state(connection, ids, *, locked=False):
    statement = text('SELECT id,status,reviewer,reviewed_at,measurement_id FROM ai_proposals '
                     'WHERE id IN :ids ORDER BY id' + (' FOR UPDATE' if locked else ''))
    statement = statement.bindparams(bindparam('ids', expanding=True))
    return json_value([dict(row) for row in connection.execute(statement, {'ids': ids}).mappings()])


def assert_transactional_schema(connection):
    statement = text('SELECT TABLE_NAME table_name,ENGINE engine FROM information_schema.tables '
                     'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN :tables')
    statement = statement.bindparams(bindparam('tables', expanding=True))
    engines = {row['table_name']: row['engine'] for row in connection.execute(
        statement, {'tables': list(REQUIRED_TRANSACTIONAL_TABLES)}).mappings()}
    if any(str(engines.get(name, '')).lower() != 'innodb' for name in REQUIRED_TRANSACTIONAL_TABLES):
        raise RuntimeError('All approval tables must exist and use InnoDB before rollback verification')
    triggers = connection.execute(text('SELECT COUNT(*) FROM information_schema.triggers '
        'WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE IN :tables').bindparams(
        bindparam('tables', expanding=True)), {'tables': list(TOUCHED_TABLES)}).scalar_one()
    if triggers:
        raise RuntimeError('Unexpected table triggers require review before rollback verification')


def select_proposals(connection, proposal_ids=None, job_id=None, limit=4):
    if proposal_ids:
        ids = sorted(set(proposal_ids))
        statement = text(PROPOSAL_SELECT + ' WHERE q.id IN :ids ORDER BY q.id').bindparams(
            bindparam('ids', expanding=True))
        rows = [dict(row) for row in connection.execute(statement, {'ids': ids}).mappings()]
        if len(rows) != len(ids):
            raise ValueError('Every explicitly selected proposal must exist')
    else:
        rows = [dict(row) for row in connection.execute(text(PROPOSAL_SELECT +
            " WHERE q.job_id=:job_id AND q.status='pending' AND q.validation_status='valid' "
            "AND j.status='complete' AND j.kind='extract' ORDER BY q.id LIMIT 100"),
            {'job_id': job_id}).mappings()]
        if not rows:
            raise ValueError('No valid pending proposals in this completed extraction job')
    if any(row['job_kind'] != 'extract' or row['validation_status'] != 'valid' for row in rows):
        raise ValueError('Only valid proposals from extraction jobs may be tested')
    groups = approval_groups(rows, confirm_visual=False)
    if not groups:
        raise ValueError('No coherent valid proposal group exists')
    if proposal_ids:
        # Explicit selections may cover several independently validated points.
        # decide_proposals keeps these groups separate rather than mixing values.
        return rows
    selected = sorted(groups, key=lambda group: (-len(group['entries']), group['paper_id']))[0]
    return [row for row, _ in selected['entries']][:limit]


def verify_approval(engine, *, proposal_ids=None, job_id=None, limit=4):
    before, state_before, result, inside, state_inside = None, None, None, None, None
    failure = None
    ids = []
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            assert_transactional_schema(connection)
            rows = select_proposals(connection, proposal_ids, job_id, limit)
            ids = [int(row['id']) for row in rows]
            state_before = proposal_state(connection, ids, locked=True)
            before = table_counts(connection)
            repository = LocalAIRepository(RollbackOnlyEngine(connection), root=ROOT)
            result = repository.decide_proposals(
                ids, 'approve', reviewer='local-smoke-verifier', confirm_visual=False,
            )
            inside = table_counts(connection)
            state_inside = proposal_state(connection, ids)
            if any(row['status'] != 'approved' or row['measurement_id'] is None for row in state_inside):
                raise AssertionError('Approval did not create linked proposal states inside the transaction')
            if inside['serdes_measurements'] - before['serdes_measurements'] != len(result['measurement_ids']):
                raise AssertionError('Unexpected measurement insert count')
            if inside['serdes_measurement_evidence'] - before['serdes_measurement_evidence'] != len(ids):
                raise AssertionError('Approval did not write one exact evidence row per proposal')
        except Exception as exc:
            failure = exc
        finally:
            # There is no success path that commits. Connection context exit is
            # also rollback-safe should an exception interrupt this statement.
            transaction.rollback()

    unchanged = False
    if before is not None:
        with engine.connect() as fresh:
            after = table_counts(fresh)
            state_after = proposal_state(fresh, ids)
        unchanged = after == before and state_after == state_before
        if not unchanged:
            raise AssertionError('Fresh-connection state differs after rollback; check concurrent writes')
    if failure:
        raise failure
    return {
        'ok': True, 'rollback_verified': unchanged, 'proposal_ids': ids,
        'job_id': rows[0]['job_id'], 'article_number': rows[0]['article_number'],
        'temporary_measurement_ids': result['measurement_ids'],
        'temporary_inserts': {name: inside[name] - before[name] for name in TOUCHED_TABLES},
        'persistent_row_changes': 0,
        'note': 'All row changes rolled back. InnoDB AUTO_INCREMENT gaps may remain.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--proposal-ids', type=int, nargs='+')
    selection.add_argument('--job-id')
    parser.add_argument('--limit', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.limit <= 8 or (args.proposal_ids and (
            len(args.proposal_ids) > 8 or any(value <= 0 for value in args.proposal_ids))):
        parser.error('Choose 1–8 positive proposal IDs and a limit of 1–8')
    load_dotenv(ROOT / '.env')
    url = URL.create('mysql+pymysql', username=os.getenv('DB_USER', 'root'),
                     password=os.getenv('DB_PASSWORD', ''), host=os.getenv('DB_HOST', '127.0.0.1'),
                     port=int(os.getenv('DB_PORT', '3306')), database=os.getenv('DB_NAME', 'ieee_repo'),
                     query={'charset': 'utf8mb4'})
    engine = create_engine(url, pool_pre_ping=True)
    try:
        result = verify_approval(engine, proposal_ids=args.proposal_ids, job_id=args.job_id, limit=args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
