"""Bounded, parameterized local-AI data access and human-reviewed promotion.

Model output never becomes SQL. Existing papers, measurements, and evidence are
read-only here; an explicit approval creates a new, provenance-bearing point.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote
import uuid

from sqlalchemy import bindparam, text
from account_store import current_account_id, favorite_sql


LATEST_RUNS_SQL = (
    "SELECT MAX(id) FROM serdes_screening_runs "
    "WHERE status='complete' AND scope_name LIKE 'all%' GROUP BY venue"
)
PAPER_COLUMNS = (
    "p.id,p.article_number,p.title,p.authors,p.year,p.source_name,p.source_system,"
    "p.pdf_available,p.pdf_local_path,p.doi,p.url,p.is_favorite"
)


def paper_columns():
    return PAPER_COLUMNS.replace('p.is_favorite', favorite_sql('p') + ' AS is_favorite')
PAPER_JOINS = f"""
 LEFT JOIN serdes_paper_screenings s ON s.paper_id=p.id AND s.run_id IN ({LATEST_RUNS_SQL})
 LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id AND a.is_current=1
 LEFT JOIN serdes_paper_link_media medium ON medium.paper_id=p.id
 LEFT JOIN serdes_paper_subtype_overrides subtype_override ON subtype_override.paper_id=p.id
   AND subtype_override.review_status IN ('verified','reviewed','approved','active')
"""
MEASUREMENT_JOIN = """
 JOIN serdes_implementation_papers ip ON ip.implementation_id=m.implementation_id
 LEFT JOIN serdes_measurement_metric_scopes ms ON ms.measurement_id=m.id
 LEFT JOIN serdes_measurement_scope_overrides so ON so.measurement_id=m.id
   AND so.review_status IN ('verified','reviewed','approved','active')
"""
GOOD_MEASUREMENT = "m.source_kind<>'title' AND m.review_status NOT IN ('rejected','invalid','needs_review')"
MEDIUM_SQL = "COALESCE(subtype_override.link_medium,medium.link_medium,'unspecified')"
COMPONENT_SQL = "COALESCE(so.energy_component_scope,ms.energy_component_scope,'unknown')"
JOB_STATUSES = {'queued', 'running', 'complete', 'failed', 'cancelled'}
RATE_SCOPES = {'lane', 'aggregate', 'unknown'}
COMPONENT_SCOPES = {'tx', 'rx', 'trx', 'full_link', 'driver_only', 'unknown'}
FIELDS_UNITS = {
    'reported_rate_gbps': 'Gbps', 'reported_rate_min_gbps': 'Gbps',
    'reported_rate_max_gbps': 'Gbps', 'lane_rate_gbps': 'Gbps',
    'aggregate_rate_gbps': 'Gbps', 'symbol_rate_gbaud': 'Gbaud',
    'lane_count': 'lanes', 'power_mw': 'mW', 'energy_pj_bit': 'pJ/bit',
    'process_nm': 'nm', 'channel_loss_db': 'dB', 'loss_frequency_ghz': 'GHz',
    'ber': 'dimensionless', 'active_area_mm2': 'mm2',
}


def json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def _json(value, default=None):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return value if value is not None else default


def _dump(value):
    return json.dumps(json_value(value), ensure_ascii=False, allow_nan=False)


def _limit(value, maximum=100):
    if isinstance(value, bool):
        raise ValueError('limit must be an integer')
    return max(1, min(int(value), maximum))


def _positive(value, name, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a finite positive number')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a finite positive number') from None
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f'{name} must be a finite positive number')
    return number


def _normal_unit(value):
    return str(value or '').lower().replace(' ', '').replace('²', '2').replace('/s', 'ps').replace('/b', '/bit').replace('/bitit', '/bit')


def validate_proposal(raw: dict) -> dict:
    """Recheck staging constraints; valid grounding is supplied by PDF validator.

    This routine intentionally does not infer scopes or calculate a rate/energy.
    A missing operating point becomes a per-field isolated point at promotion.
    """
    if not isinstance(raw, dict):
        raise ValueError('proposal must be an object')
    item = dict(raw)
    field = str(item.get('field') or item.get('field_name') or '')
    if field not in FIELDS_UNITS:
        raise ValueError(f'unsupported numeric field: {field}')
    value = _positive(item.get('value', item.get('proposed_value')), field,
                      allow_zero=field == 'channel_loss_db')
    max_value = {'process_nm': 99999.999, 'channel_loss_db': 999999.9999,
                 'energy_pj_bit': 9999999.999999999}.get(field, 99999999.999999)
    if value > max_value:
        raise ValueError(f'{field} exceeds the storage domain; verify value and unit')
    if field == 'ber' and value > 1:
        raise ValueError('BER must be at most 1')
    if field == 'lane_count' and (value != int(value) or value > 65535):
        raise ValueError('lane_count must be a positive integer <= 65535')
    canonical = FIELDS_UNITS[field]
    unit = _normal_unit(item.get('unit'))
    allowed = {_normal_unit(canonical)}
    if field == 'ber':
        allowed |= {'', '1', 'ber'}
    if field == 'lane_count':
        allowed |= {'', 'lane', 'count', '1'}
    if unit not in allowed:
        raise ValueError(f'{field} requires canonical unit {canonical}; convert before staging')
    page = item.get('page', item.get('source_page'))
    if page is not None and (isinstance(page, bool) or int(page) != float(page) or not 1 <= int(page) <= 100000):
        raise ValueError('page must be a positive one-based integer')
    quote_text = str(item.get('quote') or item.get('evidence_quote') or '').strip()
    if len(quote_text) > 16000:
        raise ValueError('evidence quote exceeds 16000 characters')
    digest = str(item.get('source_sha256') or '').lower()
    if digest and not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('invalid PDF SHA-256')
    for key in ('rate_scope', 'power_scope'):
        scope = item.get(key) or 'unknown'
        if scope not in RATE_SCOPES:
            raise ValueError(f'invalid {key}')
        item[key] = scope
    component = item.get('component_scope') or 'unknown'
    if component not in COMPONENT_SCOPES:
        raise ValueError('invalid component_scope')
    # Legacy energy_scope describes reported/calculated provenance, not blocks.
    energy_scope = item.get('energy_scope') or ('reported' if field == 'energy_pj_bit' else 'unknown')
    if energy_scope not in {'reported', 'unknown'}:
        raise ValueError('AI PDF proposals may contain reported energy only')
    if field == 'lane_rate_gbps' and item['rate_scope'] != 'lane':
        raise ValueError('lane_rate_gbps requires explicit lane rate_scope')
    if field == 'aggregate_rate_gbps' and item['rate_scope'] != 'aggregate':
        raise ValueError('aggregate_rate_gbps requires explicit aggregate rate_scope')
    op = str(item.get('operating_point') or '').strip()
    if len(op) > 160:
        raise ValueError('operating_point exceeds 160 characters')
    status = str(item.get('validation_status') or 'needs_review')
    if status not in {'valid', 'visual_review', 'needs_review', 'invalid'}:
        raise ValueError('invalid validation_status')
    item.update(field=field, value=int(value) if field == 'lane_count' else value,
                unit=canonical, page=int(page) if page is not None else None,
                quote=quote_text, source_sha256=digest, operating_point=op,
                component_scope=component, energy_scope=energy_scope,
                validation_status=status)
    return item


def approval_groups(rows: list[dict], *, confirm_visual=False) -> list[dict]:
    """Pure approval gate, run before any authoritative INSERT.

    Each group preserves one operating point and scope combination. Unknown
    points are isolated, never silently merged into an apparent FoM point.
    """
    groups = defaultdict(list)
    for row in rows:
        if row.get('status') != 'pending':
            raise ValueError('only pending proposals can be reviewed')
        if row.get('job_status') != 'complete':
            raise ValueError('only proposals from completed jobs may be approved')
        if row.get('source_system') != 'ieee' or not row.get('pdf_available'):
            raise ValueError('approval requires a linked IEEE paper with a local PDF')
        candidate = validate_proposal({**_json(row.get('payload_json'), {}),
            'field': row['field_name'], 'value': _json(row['proposed_value']),
            'unit': row['unit'], 'page': row['source_page'],
            'quote': row['evidence_quote'], 'source_sha256': row['source_sha256'],
            'operating_point': row['operating_point'],
            'validation_status': row['validation_status']})
        permitted = {'valid', 'visual_review'} if confirm_visual is True else {'valid'}
        if (candidate['validation_status'] not in permitted or not candidate['page']
                or len(candidate['quote']) < 8 or not candidate['source_sha256']):
            raise ValueError('approval requires validated PDF page, exact quote, and SHA-256')
        op = candidate['operating_point']
        if not op or op.lower() in {'unknown', 'unspecified', 'primary'}:
            op = f"isolated-proposal-{row['id']}"
        key = (row['job_id'], row['paper_id'], op)
        groups[key].append((row, candidate))
    output = []
    for (job_id, paper_id, op), entries in groups.items():
        values = {}
        for _, item in entries:
            field = item['field']
            if field in values and values[field] != item['value']:
                raise ValueError(f'conflicting {field} values within operating point; review separately')
            values[field] = item['value']
        if len({item['source_sha256'] for _, item in entries}) != 1:
            raise ValueError('operating point contains different PDF revisions')
        scopes = {}
        for scope_key in ('rate_scope', 'power_scope', 'component_scope'):
            known = {item[scope_key] for _, item in entries if item[scope_key] != 'unknown'}
            if len(known) > 1:
                raise ValueError(f'conflicting {scope_key}; keep operating points separate')
            scopes[scope_key] = next(iter(known), 'unknown')
        # Unknown reported rate must not inherit a known scope from another field.
        for _, item in entries:
            if item['field'].startswith('reported_rate') and item['rate_scope'] != scopes['rate_scope']:
                raise ValueError('unknown reported-rate scope cannot inherit another field scope')
            if item['field'] == 'power_mw' and item['power_scope'] != scopes['power_scope']:
                raise ValueError('unknown power scope cannot inherit another field scope')
            if item['field'] in {'energy_pj_bit', 'power_mw'} and item['component_scope'] != scopes['component_scope']:
                raise ValueError('unknown energy/power component cannot inherit another field scope')
        output.append({'job_id': job_id, 'paper_id': paper_id, 'operating_point': op,
                       'entries': entries, 'values': values, 'scopes': scopes})
    return output


class LocalAIRepository:
    def __init__(self, engine, root=None):
        self.engine = engine
        self.root = Path(root or Path(__file__).parent).resolve()

    def ensure_schema(self):
        """Explicit startup migration. Only creates the two separate AI tables."""
        sql = (Path(__file__).parent / 'scripts/migrations/008_local_ai.sql').read_text(encoding='utf-8')
        with self.engine.begin() as conn:
            for statement in sql.split(';'):
                if statement.strip():
                    conn.execute(text(statement))

    @staticmethod
    def build_search(query='', scope='serdes', filters=None, limit=12):
        filters = dict(filters or {})
        allowed = {'min_rate_gbps', 'max_energy_pj_bit', 'process_nm_max', 'year_from',
                   'year_to', 'medium', 'venue', 'pdf_only', 'sort', 'rate_scope',
                   'energy_component_scope'}
        if set(filters) - allowed:
            raise ValueError('unsupported search filters: ' + ', '.join(sorted(set(filters) - allowed)))
        if scope not in {'serdes', 'repo', 'all'}:
            raise ValueError('scope must be serdes or repo')
        query = str(query or '').strip()
        if len(query) > 500:
            raise ValueError('query exceeds 500 characters')
        where, params = ['1=1'], {'limit': _limit(limit, 50)}
        if scope == 'serdes':
            where.extend(["p.source_system='ieee'", 's.include_in_survey=1'])
        tokens = query.split()
        if len(tokens) > 12:
            raise ValueError('use at most 12 keyword terms')
        for i, token in enumerate(tokens):
            key = f'q{i}'
            params[key] = '%' + token.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
            where.append(f"(p.title LIKE :{key} ESCAPE '!' OR p.authors LIKE :{key} ESCAPE '!' OR p.doi LIKE :{key} ESCAPE '!' OR a.abstract_text LIKE :{key} ESCAPE '!')")
        for key, operator in [('year_from', '>='), ('year_to', '<=')]:
            if filters.get(key) not in (None, ''):
                value = int(filters[key])
                if not 1800 <= value <= 2200:
                    raise ValueError('year must be between 1800 and 2200')
                params[key] = value
                where.append(f'CAST(p.year AS UNSIGNED) {operator} :{key}')
        if 'year_from' in params and 'year_to' in params and params['year_from'] > params['year_to']:
            raise ValueError('year_from must not exceed year_to')
        if filters.get('pdf_only') not in (None, ''):
            if filters['pdf_only'] not in (True, 1, '1', 'true', False, 0, '0', 'false'):
                raise ValueError('pdf_only must be boolean')
            if filters['pdf_only'] in (True, 1, '1', 'true'):
                where.append('p.pdf_available=1')
        if filters.get('venue'):
            params['venue'] = str(filters['venue'])[:100]
            where.append('p.source_name=:venue')
        if filters.get('medium'):
            if filters['medium'] not in {'electrical', 'optical', 'unspecified'}:
                raise ValueError('invalid medium')
            params['medium'] = filters['medium']
            where.append(f'{MEDIUM_SQL}=:medium')
        rate_scope = filters.get('rate_scope') or 'lane'
        if rate_scope not in {'lane', 'aggregate'}:
            raise ValueError('rate_scope must be lane or aggregate for comparable search')
        rate_expr = f"COALESCE(m.{rate_scope}_rate_gbps,CASE WHEN m.rate_scope='{rate_scope}' THEN m.reported_rate_gbps END)"
        metric = [GOOD_MEASUREMENT]
        for key, expr, op in [('min_rate_gbps', rate_expr, '>='),
                              ('max_energy_pj_bit', 'm.energy_pj_bit', '<='),
                              ('process_nm_max', 'm.process_nm', '<=')]:
            if filters.get(key) not in (None, ''):
                params[key] = _positive(filters[key], key)
                metric.append(f'{expr} {op} :{key}')
        if filters.get('energy_component_scope'):
            if filters['energy_component_scope'] not in COMPONENT_SCOPES:
                raise ValueError('invalid energy_component_scope')
            params['energy_component_scope'] = filters['energy_component_scope']
            metric.append(f'{COMPONENT_SQL}=:energy_component_scope')
        metric_sql = ' AND '.join(metric)
        metric_from = f'FROM serdes_measurements m {MEASUREMENT_JOIN} WHERE ip.paper_id=p.id AND {metric_sql}'
        if len(metric) > 1:
            where.append(f'EXISTS (SELECT 1 {metric_from})')
        sort = filters.get('sort') or 'year'
        if sort not in {'year', 'rate', 'energy'}:
            raise ValueError('sort must be year, rate, or energy')
        order = {
            'year': 'CAST(p.year AS UNSIGNED) DESC,p.id DESC',
            'rate': f'(SELECT MAX({rate_expr}) {metric_from}) DESC,p.id DESC',
            'energy': f'(SELECT MIN(m.energy_pj_bit) {metric_from}) IS NULL,(SELECT MIN(m.energy_pj_bit) {metric_from}) ASC,p.id DESC',
        }[sort]
        sql = f'''SELECT {paper_columns()},a.abstract_text,s.relevance_class,s.include_in_survey,
                  {MEDIUM_SQL} link_medium FROM papers p {PAPER_JOINS}
                  WHERE {' AND '.join(where)} ORDER BY {order} LIMIT :limit'''
        return sql, params, metric_sql, rate_scope

    def search(self, query='', scope='serdes', filters=None, limit=12):
        sql, params, metric_sql, rate_scope = self.build_search(query, scope, filters, limit)
        with self.engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(sql), params).mappings()]
            if rows:
                stmt = text(f'''SELECT m.*,ip.paper_id,{COMPONENT_SQL} energy_component_scope
                    FROM serdes_measurements m {MEASUREMENT_JOIN}
                    WHERE ip.paper_id IN :ids AND {metric_sql} ORDER BY m.id DESC''').bindparams(bindparam('ids', expanding=True))
                grouped = defaultdict(list)
                for item in conn.execute(stmt, {**params, 'ids': [row['id'] for row in rows]}).mappings():
                    point = dict(item)
                    point['comparison_warnings'] = self.comparison_warnings(point)
                    if len(grouped[point['paper_id']]) < 12:
                        grouped[point['paper_id']].append(point)
                for row in rows:
                    row['matching_measurements'] = grouped[row['id']]
                    row['requested_rate_scope'] = rate_scope
        return json_value(rows)

    @staticmethod
    def comparison_warnings(point):
        warnings = []
        if point.get('rate_scope') in (None, '', 'unknown'):
            warnings.append('rate_scope_unknown')
        if point.get('energy_pj_bit') is not None and point.get('energy_component_scope') in (None, '', 'unknown'):
            warnings.append('energy_component_scope_unknown')
        if point.get('power_mw') is not None and point.get('power_scope') in (None, '', 'unknown'):
            warnings.append('power_scope_unknown')
        return warnings

    def get_papers(self, article_numbers):
        if not isinstance(article_numbers, (list, tuple)) or len(article_numbers) > 50:
            raise ValueError('supply at most 50 article numbers')
        keys = list(dict.fromkeys(str(item) for item in article_numbers))
        if any(not item or len(item) > 80 for item in keys):
            raise ValueError('invalid article number')
        if not keys:
            return []
        stmt = text(f'SELECT {paper_columns()},a.abstract_text FROM papers p '
                    'LEFT JOIN paper_current_abstracts a ON a.paper_id=p.id AND a.is_current=1 '
                    'WHERE p.article_number IN :keys').bindparams(bindparam('keys', expanding=True))
        with self.engine.connect() as conn:
            rows = {str(row['article_number']): dict(row) for row in conn.execute(stmt, {'keys': keys}).mappings()}
        return json_value([rows[key] for key in keys if key in rows])

    def paper_evidence(self, article_number):
        papers = self.get_papers([article_number])
        if not papers:
            return {'paper': None, 'measurements': [], 'screening': None}
        paper = papers[0]
        with self.engine.connect() as conn:
            measurements = [dict(row) for row in conn.execute(text(f'''
                SELECT m.*,{COMPONENT_SQL} energy_component_scope
                FROM serdes_measurements m {MEASUREMENT_JOIN}
                WHERE ip.paper_id=:id ORDER BY m.id'''), {'id': paper['id']}).mappings()]
            evidence = conn.execute(text('''SELECT e.* FROM serdes_measurement_evidence e
                JOIN serdes_implementation_papers ip ON ip.paper_id=:id
                JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id AND m.id=e.measurement_id
                ORDER BY e.id'''), {'id': paper['id']}).mappings().all()
            screening = conn.execute(text(f'SELECT * FROM serdes_paper_screenings WHERE paper_id=:id AND run_id IN ({LATEST_RUNS_SQL})'), {'id': paper['id']}).mappings().first()
        grouped = defaultdict(list)
        for row in evidence:
            grouped[row['measurement_id']].append(dict(row))
        for row in measurements:
            row['evidence'] = grouped[row['id']]
            row['comparison_warnings'] = self.comparison_warnings(row)
        return json_value({'paper': paper, 'measurements': measurements,
                           'screening': dict(screening) if screening else None})

    def review_queue(self, limit=30):
        from scripts.build_serdes_review_queue import MEASUREMENT_SQL, EVIDENCE_SQL
        from serdes_review_queue import build_review_package
        limit = _limit(limit, 100)
        with self.engine.connect() as conn:
            measurements = [dict(row) for row in conn.execute(text(MEASUREMENT_SQL)).mappings()]
            evidence = [dict(row) for row in conn.execute(text(EVIDENCE_SQL)).mappings()]
            missing = [dict(row) for row in conn.execute(text(f'''
                SELECT {paper_columns()},a.abstract_text,
                  MAX(m.lane_rate_gbps IS NOT NULL OR m.reported_rate_gbps IS NOT NULL OR m.aggregate_rate_gbps IS NOT NULL) has_rate,
                  MAX(m.energy_pj_bit IS NOT NULL) has_energy,MAX(m.process_nm IS NOT NULL) has_process,
                  MAX(m.channel_loss_db IS NOT NULL) has_loss
                FROM papers p {PAPER_JOINS}
                LEFT JOIN serdes_implementation_papers ip ON ip.paper_id=p.id
                LEFT JOIN serdes_measurements m ON m.implementation_id=ip.implementation_id AND {GOOD_MEASUREMENT}
                WHERE p.source_system='ieee' AND s.include_in_survey=1
                GROUP BY p.id,a.abstract_text
                HAVING NOT(has_rate AND has_energy AND has_process AND has_loss)
                ORDER BY p.pdf_available DESC,CAST(p.year AS UNSIGNED) DESC,p.id DESC LIMIT :limit
            '''), {'limit': limit}).mappings()]
        package = build_review_package(measurements, evidence)
        package['review_queue'] = package['review_queue'][:limit]
        visible = {row['paper_id'] for row in package['review_queue']}
        package['measurement_flags'] = [row for row in package['measurement_flags'] if row['paper_id'] in visible]
        for row in missing:
            row['missing_fields'] = [name for name in ('rate', 'energy', 'process', 'loss') if not row.get('has_' + name)]
        package['missing_fields'] = missing
        package['items'] = [{**row, 'reason': ', '.join(row.get('flag_codes', []))}
                            for row in package['review_queue']]
        seen = {row['article_number'] for row in package['items']}
        package['items'].extend({**row, 'reason': 'Missing: ' + ', '.join(row['missing_fields'])}
                                for row in missing if row['article_number'] not in seen)
        package['items'] = package['items'][:limit]
        return json_value(package)

    @staticmethod
    def _job(row):
        if not row:
            return None
        item = dict(row)
        item['request'] = _json(item.pop('request_json', None), {})
        item['result'] = _json(item.pop('result_json', None))
        return json_value(item)

    def create_job(self, jobdict):
        item = dict(jobdict)
        job_id = str(item.get('id') or uuid.uuid4())
        uuid.UUID(job_id)
        for key, maximum in [('owner', 191), ('kind', 32), ('model', 191)]:
            if not isinstance(item.get(key), str) or not 1 <= len(item[key]) <= maximum:
                raise ValueError(f'invalid job {key}')
        # New requests always enter the queue, regardless of caller-supplied status.
        with self.engine.begin() as conn:
            if current_account_id.get() is not None:
                conn.execute(text('''INSERT INTO ai_jobs(id,owner,account_id,kind,model,status,request_json)
                    VALUES (:id,:owner,:account_id,:kind,:model,'queued',:request_json)'''),
                    {'id': job_id, 'owner': item['owner'], 'account_id': current_account_id.get(),
                     'kind': item['kind'], 'model': item['model'],
                     'request_json': _dump(item.get('request', item.get('request_json', {})))})
                return job_id
            conn.execute(text('''INSERT INTO ai_jobs(id,owner,kind,model,status,request_json)
                VALUES (:id,:owner,:kind,:model,'queued',:request_json)'''),
                {'id': job_id, 'owner': item['owner'], 'kind': item['kind'], 'model': item['model'],
                 'request_json': _dump(item.get('request', item.get('request_json', {})))})
        return job_id

    def get_job(self, job_id, owner=None):
        sql = 'SELECT * FROM ai_jobs WHERE id=:id'
        params = {'id': job_id}
        if owner is not None:
            if current_account_id.get() is not None:
                sql += ' AND account_id=:account'
                params['account'] = current_account_id.get()
            else:
                sql += ' AND owner=:owner'
                params['owner'] = owner
        with self.engine.connect() as conn:
            return self._job(conn.execute(text(sql), params).mappings().first())

    def list_jobs(self, owner, limit=20):
        with self.engine.connect() as conn:
            if current_account_id.get() is not None:
                return [self._job(row) for row in conn.execute(text('SELECT * FROM ai_jobs '
                    'WHERE account_id=:account ORDER BY created_at DESC LIMIT :limit'),
                    {'account': current_account_id.get(), 'limit': _limit(limit, 100)}).mappings()]
            return [self._job(row) for row in conn.execute(text('SELECT * FROM ai_jobs WHERE owner=:owner ORDER BY created_at DESC LIMIT :limit'), {'owner': owner, 'limit': _limit(limit, 100)}).mappings()]

    def update_job(self, job_id, **fields):
        if set(fields) - {'status', 'result', 'error'} or not fields:
            raise ValueError('only job status, result, and error can change')
        if 'status' in fields and fields['status'] not in JOB_STATUSES:
            raise ValueError('invalid job status')
        values = {'result_json' if key == 'result' else key: _dump(value) if key == 'result' else value for key, value in fields.items()}
        # A cancellation can race the final model response. Never resurrect a
        # cancelled (or already terminal) job by publishing late worker output.
        guard = " AND status='running'" if fields.get('status') in {'complete', 'failed'} else ''
        with self.engine.begin() as conn:
            result = conn.execute(text('UPDATE ai_jobs SET ' + ','.join(f'{key}=:{key}' for key in values) + ',updated_at=CURRENT_TIMESTAMP(6) WHERE id=:id' + guard), {**values, 'id': job_id})
        return result.rowcount

    def claim_job(self, job_id):
        """Atomic claim: a queued job can be run by at most one worker."""
        with self.engine.begin() as conn:
            result = conn.execute(text("UPDATE ai_jobs SET status='running',updated_at=CURRENT_TIMESTAMP(6) WHERE id=:id AND status='queued'"), {'id': job_id})
        return result.rowcount == 1

    def recover_interrupted_jobs(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE ai_jobs SET status='failed',error='Server restarted during generation; submit again.',updated_at=CURRENT_TIMESTAMP(6) WHERE status='running'"))
            return [self._job(row) for row in conn.execute(text("SELECT * FROM ai_jobs WHERE status='queued' ORDER BY created_at")).mappings()]

    def add_proposals(self, job_id, article_number, proposals):
        if not isinstance(proposals, list) or len(proposals) > 100:
            raise ValueError('supply at most 100 proposals')
        candidates = [validate_proposal(row) for row in proposals]
        ids = []
        with self.engine.begin() as conn:
            job = conn.execute(text('SELECT id FROM ai_jobs WHERE id=:id'), {'id': job_id}).first()
            paper = conn.execute(text('SELECT id FROM papers WHERE article_number=:key'), {'key': article_number}).mappings().first()
            if not job or not paper:
                raise ValueError('job and linked paper must exist')
            for item in candidates:
                result = conn.execute(text('''INSERT INTO ai_proposals(job_id,paper_id,field_name,
                    proposed_value,unit,source_page,evidence_quote,source_sha256,operating_point,
                    validation_status,payload_json) VALUES (:job_id,:paper_id,:field_name,
                    :proposed_value,:unit,:source_page,:evidence_quote,:source_sha256,:operating_point,
                    :validation_status,:payload_json)'''), {
                    'job_id': job_id, 'paper_id': paper['id'], 'field_name': item['field'],
                    'proposed_value': _dump(item['value']), 'unit': item['unit'],
                    'source_page': item['page'], 'evidence_quote': item['quote'],
                    'source_sha256': item['source_sha256'] or None,
                    'operating_point': item['operating_point'],
                    'validation_status': item['validation_status'], 'payload_json': _dump(item)})
                ids.append(int(result.lastrowid))
        return ids

    def list_proposals(self, job_id=None, status='pending', limit=100):
        where, params = [], {'limit': _limit(limit, 500)}
        if status not in {None, 'all', 'pending', 'approved', 'rejected'}:
            raise ValueError('invalid proposal status')
        if status not in {None, 'all'}:
            where.append('q.status=:status')
            params['status'] = status
        if job_id:
            where.append('q.job_id=:job_id')
            params['job_id'] = job_id
        with self.engine.connect() as conn:
            rows = conn.execute(text('SELECT q.*,p.article_number,p.title,p.source_system,j.model,j.owner,j.status job_status '
                'FROM ai_proposals q JOIN papers p ON p.id=q.paper_id JOIN ai_jobs j ON j.id=q.job_id '
                'WHERE ' + (' AND '.join(where) or '1=1') + ' ORDER BY q.id DESC LIMIT :limit'), params).mappings().all()
        output = []
        for row in rows:
            item = dict(row)
            item['value'] = _json(item['proposed_value'])
            item['payload'] = _json(item.pop('payload_json'), {})
            item['field'] = item['field_name']
            item['page'] = item['source_page']
            item['quote'] = item['evidence_quote']
            for key in ('rate_scope', 'power_scope', 'component_scope', 'energy_scope', 'validation_reasons'):
                item[key] = item['payload'].get(key)
            item['source_url'] = '/pdf/' + quote(str(item['article_number']), safe='') + '#page=' + str(item['page'] or 1)
            output.append(item)
        return json_value(output)

    def _verify_pdf_revision(self, row):
        """Revalidate approval against the current, source-root-confined PDF."""
        source = self.root / 'ieee-pdf'
        if not source.resolve().is_relative_to(self.root):
            raise ValueError('approval PDF directory is outside the repository')
        relative = str(row.get('pdf_local_path') or '').replace('\\', '/')
        candidate = (self.root / relative).resolve() if relative else (source / (str(row['article_number']) + '.pdf')).resolve()
        if not candidate.is_relative_to(source.resolve()) or candidate.suffix.lower() != '.pdf':
            raise ValueError('approval PDF path is outside the IEEE source directory')
        path = str(candidate)
        if os.name == 'nt' and not path.startswith('\\\\?\\'):
            path = '\\\\?\\' + path
        digest = hashlib.sha256()
        try:
            with open(path, 'rb') as handle:
                if handle.read(5) != b'%PDF-':
                    raise ValueError('approval source is not a PDF')
                handle.seek(0)
                total = 0
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ValueError('approval PDF exceeds the 64 MiB local AI limit')
                    digest.update(chunk)
        except OSError as exc:
            raise ValueError('approval PDF is unavailable; extract again after restoring it') from exc
        if digest.hexdigest() != row.get('source_sha256'):
            raise ValueError('PDF changed after extraction; extract and verify the new revision')

    def decide_proposals(self, ids, decision, reviewer, confirm_visual=False):
        if decision not in {'approved', 'rejected', 'approve', 'reject'}:
            raise ValueError('decision must be approved or rejected')
        decision = {'approve': 'approved', 'reject': 'rejected'}.get(decision, decision)
        if not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 191:
            raise ValueError('an explicit human reviewer is required')
        if not isinstance(ids, list) or not ids or len(ids) > 100:
            raise ValueError('supply between 1 and 100 proposal ids')
        if any(isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0 for value in ids):
            raise ValueError('proposal ids must be positive integers')
        ids = sorted(set(int(value) for value in ids))
        stmt = text('''SELECT q.*,p.article_number,p.title,p.year,p.source_name,p.source_system,
                    p.pdf_available,p.pdf_local_path,j.model,j.status job_status FROM ai_proposals q JOIN papers p ON p.id=q.paper_id
                    JOIN ai_jobs j ON j.id=q.job_id WHERE q.id IN :ids ORDER BY q.id FOR UPDATE''').bindparams(bindparam('ids', expanding=True))
        measurement_ids = []
        with self.engine.begin() as conn:
            rows = [dict(row) for row in conn.execute(stmt, {'ids': ids}).mappings()]
            if len(rows) != len(ids) or any(row['status'] != 'pending' for row in rows):
                raise ValueError('all requested proposals must exist and remain pending')
            if decision == 'rejected':
                groups = []
            else:
                groups = approval_groups(rows, confirm_visual=confirm_visual)
                verified = set()
                for row in rows:
                    revision = (row['paper_id'], row['source_sha256'])
                    if revision not in verified:
                        self._verify_pdf_revision(row)
                        verified.add(revision)
            measurement_by_proposal = {}
            for group in groups:
                entries = group['entries']
                paper = entries[0][0]
                # A separate implementation preserves all old data and revision history.
                # UUID also makes a partial approval a separate, explicit operating point.
                key = 'ai-approved:' + str(uuid.uuid4())
                result = conn.execute(text('''INSERT INTO serdes_implementations
                    (implementation_key,canonical_paper_id,canonical_title,canonical_year,
                     canonical_venue,dedup_method,dedup_confidence,review_status,notes)
                    VALUES (:key,:paper,:title,:year,:venue,'human_ai_review',1,'verified',:notes)'''),
                    {'key': key, 'paper': paper['paper_id'], 'title': paper['title'],
                     'year': int(paper['year']) if str(paper['year']).isdigit() else None,
                     'venue': paper['source_name'],
                     'notes': _dump({'job_id': group['job_id'], 'model': paper['model'],
                                     'reviewer': reviewer, 'operating_point': group['operating_point']})})
                impl_id = int(result.lastrowid)
                conn.execute(text('INSERT INTO serdes_implementation_papers (implementation_id,paper_id,relation_type) VALUES (:impl,:paper,\'primary\')'), {'impl': impl_id, 'paper': paper['paper_id']})
                measurement = {'implementation_id': impl_id, 'operating_point_key': 'human-reviewed',
                    'reference_title': paper['title'], 'publication_name': paper['source_name'],
                    'publication_year': int(paper['year']) if str(paper['year']).isdigit() else None,
                    **group['values'], **group['scopes'], 'source_kind': 'pdf',
                    'review_status': 'verified', 'overall_confidence': 1.0,
                    'energy_scope': 'reported' if 'energy_pj_bit' in group['values'] else 'unknown'}
                if 'energy_pj_bit' in group['values']:
                    measurement['energy_basis'] = 'reported_energy'
                # Every identifier below is a code-owned allowlisted dictionary key.
                cols = ','.join(measurement)
                binds = ','.join(':' + name for name in measurement)
                result = conn.execute(text(f'INSERT INTO serdes_measurements ({cols}) VALUES ({binds})'), measurement)
                mid = int(result.lastrowid)
                measurement_ids.append(mid)
                for row, item in entries:
                    evidence_sha = hashlib.sha256(item['quote'].encode()).hexdigest()
                    evidence_key = hashlib.sha256(f"{mid}|{item['field']}|{row['id']}".encode()).hexdigest()
                    conn.execute(text('''INSERT INTO serdes_measurement_evidence
                        (evidence_key,measurement_id,field_name,paper_id,source_kind,source_url,
                         source_locator,evidence_text,source_sha256,evidence_sha256,extraction_method,
                         extractor_version,confidence,review_status)
                        VALUES (:key,:mid,:field,:paper,'pdf',:url,:locator,:quote,:sha,:evidence_sha,
                         'local_ai_human_approved','local-ai-1.0',1,'verified')'''),
                        {'key': evidence_key, 'mid': mid, 'field': item['field'], 'paper': paper['paper_id'],
                         'url': '/pdf/' + quote(str(paper['article_number']), safe='') + '#page=' + str(item['page']),
                         'locator': f"page {item['page']}; proposal {row['id']}; model {paper['model']}"[:120], 'quote': item['quote'],
                         'sha': item['source_sha256'], 'evidence_sha': evidence_sha})
                    measurement_by_proposal[row['id']] = mid
                component = group['scopes']['component_scope']
                conn.execute(text('''INSERT INTO serdes_measurement_metric_scopes
                    (measurement_id,energy_component_scope,scope_source,scope_confidence,reason_codes,
                     evidence_text,classifier_version,classified_at)
                    VALUES (:mid,:scope,'human_ai_review',:confidence,:reasons,:evidence,'local-ai-1.0',NOW(6))'''),
                    {'mid': mid, 'scope': component, 'confidence': 1 if component != 'unknown' else 0,
                     'reasons': _dump(['human_approved_pdf_scope' if component != 'unknown' else 'scope_unresolved']),
                     'evidence': _dump({'job_id': group['job_id'], 'model': paper['model'],
                                        'reviewer': reviewer, 'proposal_ids': [row['id'] for row, _ in entries]})})
            for row in rows:
                conn.execute(text('''UPDATE ai_proposals SET status=:status,reviewer=:reviewer,
                    reviewed_at=NOW(6),measurement_id=:mid WHERE id=:id AND status='pending' '''),
                    {'status': decision, 'reviewer': reviewer, 'mid': measurement_by_proposal.get(row['id']), 'id': row['id']})
        return {'decision': decision, 'proposal_ids': ids, 'measurement_ids': measurement_ids}
