"""Server-grounded Survey analysis. No model-written metrics or browser-supplied data."""
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import math
import re
import unicodedata
from statistics import median

from local_ai_service import SYSTEM, validate_job
from serdes_enrichment import pareto_frontier_ids

FILTERS = {
    'rate_mode': ('lane', 'reported', 'aggregate'),
    'evidence_tier': ('structured', 'all', 'verified', 'user_sheet', 'abstract', 'title'),
    'medium': ('all', 'electrical', 'optical', 'unspecified'),
    'subtype': ('all', 'die_to_die', 'memory_io', 'backplane', 'cable', 'chip_to_chip',
                'electrical_other', 'vcsel', 'silicon_photonic', 'eml_dml', 'pon', 'optical_other', 'mixed_or_unknown'),
    'energy_scope': ('all', 'tx', 'rx', 'trx', 'full_link', 'driver_only', 'unknown'),
    'pareto_mode': ('all', 'frontier'),
}
RATE_FIELDS = {'lane': 'lane_rate_gbps', 'reported': 'reported_rate_gbps', 'aggregate': 'aggregate_rate_gbps'}
POINT_FIELDS = ('measurement_id', 'operating_point_key', 'source_kind', 'review_status', 'evidence_tier',
                'lane_rate_gbps', 'reported_rate_gbps', 'aggregate_rate_gbps', 'rate_scope', 'power_scope',
                'energy_pj_bit', 'energy_basis', 'energy_component_scope', 'component_scope', 'process_nm',
                'channel_loss_db', 'loss_frequency_ghz', 'ber', 'ber_scope', 'modulation', 'power_mw')
WARNINGS = [
    '집계는 현재 Performance landscape 필터 기준입니다. 아래 Explorer 검색·목록 미리보기·차트 확대 영역은 반영하지 않습니다.',
    'Energy scope는 에너지 관련 차트에 적용됩니다. 속도-연도 차트는 기존 Survey와 동일하게 전력 범위로 제한하지 않습니다.',
    'Pareto는 같은 매체·전력 범위·속도 분모에서 최소 3점인 그룹만 계산합니다. BER·채널 손실·공정까지 동일함을 뜻하지 않습니다.',
    '집계는 SQL 성능점 전체, AI 해석은 제한된 대표 근거를 사용합니다. 세계 최고 성능이나 인과관계를 입증하지 않습니다.',
    'AI 해석은 검토 의견입니다. 인용 ID의 존재 확인은 수치 해석의 정확성을 보장하지 않으며 DB 값은 변경하지 않습니다.',
]
SURVEY_SCHEMA = {
    'type': 'object', 'properties': {
        'observations': {'type': 'array', 'minItems': 1, 'maxItems': 6, 'items': {
            'type': 'object', 'properties': {
                'source_id': {'type': 'string'},
                'evidence_index': {'type': 'integer', 'minimum': 0, 'maximum': 20},
                'statement': {'type': 'string'},
            }, 'required': ['source_id', 'statement', 'evidence_index'], 'additionalProperties': False,
        }},
        'limitations': {'type': 'array', 'items': {'type': 'string'}},
    }, 'required': ['observations', 'limitations'], 'additionalProperties': False,
}


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip()


def numeric_literals(value):
    value = normalized(value)
    value = re.sub(r'(?<=\d),(?=\d{3}(?:\D|$))', '', value)
    # Compare numeric values, not formatting (2.0 versus 2, 1e-12 versus 1.0e-12).
    return {float(m) for m in re.findall(r'(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', value)}


def quantities(value):
    units = {'gbps': 'gb/s', 'pj/b': 'pj/bit', 'pj/비트': 'pj/bit'}
    value = normalized(value).lower()
    return {(float(n), units.get(u, u)) for n, u in re.findall(
        r'(\d+(?:\.\d+)?(?:e[+-]?\d+)?)\s*(gb/s|gbps|pj/bit|pj/비트|pj/b|nm|db|ghz|mw|tb/s)', value)}


def checked_observations(result, sources):
    by_id = {s['source_id']: s for s in sources}
    accepted, rejected = [], 0
    for raw in result.get('observations', [])[:6]:
        item = dict(raw)
        item['source_id'] = item.get('source_id', '').strip('[] ')
        source = by_id.get(item['source_id'])
        statement, quote = item.get('statement', ''), item.get('evidence_quote', '')
        if 'evidence_index' in item:
            index = item['evidence_index']
            lines = source.get('evidence_lines', []) if source else []
            if type(index) is not int or not 0 <= index < len(lines):
                rejected += 1
                continue
            quote = lines[index]
            item['evidence_quote'] = quote
        statement = re.sub(r'\[' + re.escape(item['source_id']) + r'\]', '', statement).strip()
        item['statement'] = statement
        if (not source or not 10 <= len(quote) <= 1200 or not 5 <= len(statement) <= 900
                or ('evidence_index' not in item and normalized(quote) not in normalized(source['text']))
                or numeric_literals(statement) - numeric_literals(quote)
                or quantities(statement) - quantities(quote)
                or re.search(r'평균|\baverage\b|\bmean\b|세계\s*(?:최초|최고)|가장\s*(?:많|높|낮|우수)|world.best', statement, re.I)
                or re.search(r'need\s+json|ensure\s+proper|answer\s+field|\}\s*[.{]', statement, re.I)):
            rejected += 1
            continue
        # The source ID is appended by the server, never accepted from prose.
        if re.search(r'\[S\d+\]', statement):
            rejected += 1
            continue
        if source.get('kind') == 'survey_statistics' and 'evidence_index' in item:
            # Even correct numbers can be paraphrased as the wrong population.
            item['statement'] = quote
            item['basis'] = 'server_statistics'
        accepted.append(item)
    return accepted, rejected


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def number(value, positive=True):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and (not positive or value > 0) else None
    except (TypeError, ValueError):
        return None


def validate_filters(value):
    if not isinstance(value, dict) or set(value) - FILTERS.keys():
        raise ValueError('지원하지 않는 Survey 필터입니다.')
    output = {}
    for key, options in FILTERS.items():
        option = value.get(key, options[0])
        if not isinstance(option, str) or option not in options:
            raise ValueError(f'유효하지 않은 Survey 필터: {key}')
        output[key] = option
    return output


def point_record(point):
    return {**{key: point.get(key) for key in ('article_number', 'title', 'year', 'venue', 'link_medium', 'link_subtype')},
            'performance': {key: point['performance'].get(key) for key in POINT_FIELDS if point['performance'].get(key) is not None}}


def point_line(point):
    m = point['performance']
    fields = [('lane_rate_gbps', 'Gb/s per lane'), ('reported_rate_gbps', 'Gb/s reported'),
              ('aggregate_rate_gbps', 'Gb/s aggregate'), ('energy_pj_bit', 'pJ/bit'),
              ('process_nm', 'nm'), ('channel_loss_db', 'dB'), ('loss_frequency_ghz', 'GHz'),
              ('ber', ''), ('power_mw', 'mW')]
    labels = {'lane_rate_gbps': '레인당 속도', 'reported_rate_gbps': '보고 속도', 'aggregate_rate_gbps': '총합 속도',
              'energy_pj_bit': '비트당 에너지', 'process_nm': '공정 크기', 'channel_loss_db': '채널 손실',
              'loss_frequency_ghz': '손실 측정 주파수', 'ber': 'BER', 'power_mw': '전력'}
    values = '; '.join(f'{labels[key]} {m[key]} {unit}' for key, unit in fields if m.get(key) is not None)
    medium = {'electrical': '전기', 'optical': '광', 'unspecified': '미지정'}.get(point.get('link_medium'), '미지정')
    return (f"{point.get('year')}년 논문 {point.get('article_number')}, measurement_id={m.get('measurement_id')}. "
            f"매체: {medium}; 에너지 회로 범위: {m.get('energy_component_scope', 'unknown')}; "
            f"속도 분모: {m.get('rate_scope', 'unknown')}; 에너지 근거: {m.get('energy_basis', 'unknown')}; "
            f"출처: {m.get('source_kind', 'unknown')}; 검토 상태: {m.get('review_status', 'unknown')}; "
            f"변조: {m.get('modulation', 'unknown')}. {values}. 여기 없는 수치는 미확인입니다.")


def evidence_lines(source, summary):
    if source['kind'] == 'survey_statistics':
        labels = {'rate_year': '속도-연도', 'energy_year': '에너지-연도', 'rate_energy': '속도-에너지',
                  'energy_process': '에너지-공정', 'energy_loss': '에너지-손실', 'frontier': 'Pareto frontier'}
        lines = [f"서버 SQL 집계: 차트에 연결된 논문 {summary['matched_papers']}편. " +
                 '; '.join(f'{labels[k]} 차트 성능점 {v}개' for k, v in summary['chart_counts'].items()) +
                 '. 모든 수치는 개수이며 속도나 에너지 수치가 아닙니다. 전체 서베이 논문 수와도 다릅니다.']
        for group in summary['rate_energy_groups']:
            lines.append('그룹 ' + '/'.join(group['group']) + f"만의 통계: 성능점 {group['n']}개; " +
                         f"속도 최소 {group['rate_min']} Gb/s, 최대 {group['rate_max']} Gb/s; " +
                         f"에너지 최소 {group['energy_min']} pJ/bit, 중앙값 {group['energy_median']} pJ/bit, " +
                         f"최대 {group['energy_max']} pJ/bit; Pareto frontier {group['frontier_count']}개. " +
                         '평균 통계는 없으며, 이 그룹의 수치가 전체 서베이를 대표하지 않습니다.')
        return lines
    record = json.loads(source['text'])
    if 'performance' in record:
        return [point_line(record)]
    lines = [point_line(p) for p in record.get('matching_points', [])]
    if not lines:
        lines = [f"Paper {record['article_number']}: no matching stored operating point under current filters. Numeric comparison is unavailable."]
    if record.get('abstract'):
        lines.append('Stored abstract, not PDF verification: ' + record['abstract'])
    return lines


def build_snapshot(data, filters):
    """Use the same canonical chart sets and Pareto grouping as the Survey UI."""
    points = [p for p in data['points'] if p.get('article_number')
              and (filters['medium'] == 'all' or p.get('link_medium', 'unspecified') == filters['medium'])
              and (filters['subtype'] == 'all' or p.get('link_subtype', 'mixed_or_unknown') == filters['subtype'])]
    by_id = {int(p['performance']['measurement_id']): p for p in points}
    sets = {name: [by_id[int(i)] for i in ids if int(i) in by_id] for name, ids in data['chart_sets'].items()}
    mode, rate_field = filters['rate_mode'], RATE_FIELDS[filters['rate_mode']]
    pairs = [p for p in sets.get(f'{mode}_energy', [])
             if number(p['performance'].get(rate_field)) is not None and number(p['performance'].get('energy_pj_bit')) is not None]

    def group_key(p):
        m = p['performance']
        denominator = m.get('rate_scope', 'unknown') if mode == 'reported' else mode
        scope, medium = m.get('energy_component_scope', 'unknown'), p.get('link_medium')
        if medium not in {'electrical', 'optical'} or scope in {'unknown', '', None} or denominator in {'unknown', '', None}:
            return None
        return medium, scope, denominator

    group_counts = Counter(group_key(p) for p in pairs)
    frontier = pareto_frontier_ids(pairs, id_getter=lambda p: p['performance']['measurement_id'],
                                 x_getter=lambda p: p['performance'].get(rate_field),
                                 y_getter=lambda p: p['performance'].get('energy_pj_bit'),
                                 group_getter=lambda p: group_key(p) if group_counts[group_key(p)] >= 3 else None)
    grouped = defaultdict(list)
    for p in pairs:
        grouped[group_key(p)].append(p)
    groups = []
    for key, rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        energies = [float(p['performance']['energy_pj_bit']) for p in rows]
        rates = [float(p['performance'][rate_field]) for p in rows]
        groups.append({'group': list(key) if key else ['scope_or_medium_unknown'], 'n': len(rows),
                       'rate_min': min(rates), 'rate_max': max(rates), 'energy_min': min(energies),
                       'energy_median': median(energies), 'energy_max': max(energies),
                       'frontier_count': sum(p['performance']['measurement_id'] in frontier for p in rows)})
    plotted_pairs = [p for p in pairs if filters['pareto_mode'] != 'frontier' or p['performance']['measurement_id'] in frontier]
    chart_defs = {'rate_year': (f'{mode}_rate', rate_field), 'energy_year': ('energy', 'energy_pj_bit'),
                  'energy_process': ('energy_process', 'energy_pj_bit'), 'energy_loss': ('energy_loss', 'energy_pj_bit')}
    counts = {}
    for label, (set_name, field) in chart_defs.items():
        rows = [p for p in sets.get(set_name, []) if number(p['performance'].get(field)) is not None]
        if label.endswith('_year'):
            rows = [p for p in rows if number(p.get('year'), positive=False) is not None]
        else:
            other = 'process_nm' if label == 'energy_process' else 'channel_loss_db'
            rows = [p for p in rows if number(p['performance'].get(other), positive=label == 'energy_process') is not None]
        counts[label] = len(rows)
    counts.update(rate_energy=len(plotted_pairs), frontier=len(frontier))
    summary = {'filters': filters, 'matched_papers': len({p['article_number'] for p in points}),
               'stored_points': len(points), 'chart_counts': counts, 'rate_energy_groups': groups,
               'unmatched_reference_excluded': len(data.get('unmatched_points', []))}
    # Spread evidence across scopes and publication years, without merging measurements.
    preferred = plotted_pairs + sets.get('representative', []) + points
    buckets = defaultdict(list)
    for p in {p['performance']['measurement_id']: p for p in preferred}.values():
        buckets[(p.get('link_medium'), p['performance'].get('energy_component_scope'))].append(p)
    ordered = []
    for rows in buckets.values():
        rows.sort(key=lambda p: (p.get('year') or 0, p['performance']['measurement_id']))
        ordered.append([p for pair in zip(rows[::-1], rows) for p in pair])
    sample, seen = [], set()
    for index in range(max((len(rows) for rows in ordered), default=0)):
        for rows in ordered:
            if index < len(rows):
                p = rows[index]
                if p['performance']['measurement_id'] not in seen:
                    seen.add(p['performance']['measurement_id'])
                    sample.append(p)
        if len(sample) >= 12:
            break
    sample = sample[:12]
    return summary, points, sample


class SurveyAIService:
    def __init__(self, worker, performance_builder):
        self.worker, self.performance_builder = worker, performance_builder

    def submit(self, payload, owner, role):
        if not isinstance(payload, dict) or set(payload) - {'kind', 'question', 'article_numbers', 'filters'}:
            raise ValueError('지원하지 않는 Survey AI 요청입니다.')
        kind = payload.get('kind', 'analyze')
        if not isinstance(kind, str) or kind not in {'analyze', 'compare'}:
            raise ValueError('분석 또는 비교를 선택하세요.')
        filters = validate_filters(payload.get('filters', {}))
        req = validate_job({'kind': 'ask', 'question': payload.get('question') or '현재 조건의 연구 경향과 비교 시 주의점을 설명해 주세요.',
                            'article_numbers': payload.get('article_numbers', []), 'num_ctx': 16384,
                            'max_tokens': 2048, 'thinking': 'low'}, role)
        keys = req['article_numbers']
        if kind == 'compare' and not 2 <= len(keys) <= 5:
            raise ValueError('비교할 논문을 2~5편 선택하세요.')
        if kind == 'analyze' and keys:
            raise ValueError('현재 조건 분석에는 선택 논문을 전달하지 않습니다.')
        data = self.performance_builder(filters, strict=True)
        summary, points, sample = build_snapshot(data, filters)
        papers = self.worker.repo.get_papers(keys) if keys else []
        if kind == 'compare' and (len(papers) != len(keys) or any(p.get('source_system') != 'ieee' for p in papers)):
            raise ValueError('DB에 있는 IEEE Survey 논문만 비교할 수 있습니다.')
        if kind == 'analyze' and not points:
            raise ValueError('현재 조건에서 분석할 성능점이 없습니다. 필터를 완화해 주세요.')
        sources = [{'source_id': 'S1', 'kind': 'survey_statistics', 'title': '서버 계산: 현재 조건의 성능 집계',
                    'article_number': None, 'page': None, 'source_url': '/serdes', 'text': dump(summary)}]
        warnings = list(WARNINGS)
        comparison = []
        if kind == 'compare':
            for paper in papers:
                matched = [p for p in points if p['article_number'] == paper['article_number']
                           and (filters['energy_scope'] == 'all' or p['performance'].get('energy_component_scope') == filters['energy_scope'])]
                matched.sort(key=lambda p: (p['performance'].get('review_status') == 'verified', p['performance']['measurement_id']), reverse=True)
                record = {'article_number': paper['article_number'], 'title': paper['title'],
                          'year': paper.get('year'), 'pdf_available': bool(paper.get('pdf_available')),
                          'matching_points': [point_record(p) for p in matched[:2]], 'omitted_points': max(0, len(matched) - 2),
                          'abstract': str(paper.get('abstract_text') or '')[:600]}
                comparison.append(record)
                if not matched:
                    warnings.append(f"{paper['article_number']}: 현재 필터에 맞는 대표 성능점이 없어 수치 비교에서 제외합니다.")
                sources.append({'source_id': f'S{len(sources)+1}', 'kind': 'database', 'title': paper['title'],
                                'article_number': paper['article_number'], 'page': None,
                                'source_url': f"/ai?paper={paper['article_number']}", 'text': dump(record)})
            warnings.append('선택 논문의 저장된 초록과 필터에 맞는 최대 2개 대표 동작점을 비교합니다. 이 작업은 PDF 전체를 새로 읽지 않습니다.')
        else:
            for p in sample:
                sources.append({'source_id': f'S{len(sources)+1}', 'kind': 'database', 'title': p['title'],
                                'article_number': p['article_number'], 'page': None,
                                'source_url': f"/ai?paper={p['article_number']}", 'text': dump(point_record(p))})
        for source in sources:
            source['evidence_lines'] = evidence_lines(source, summary)
        req.update(kind=f'survey_{kind}', filters=filters,
                   survey_snapshot={'summary': summary, 'sources': sources, 'warnings': warnings,
                                    'comparison': comparison, 'sample_count': len(sources)-1,
                                    'captured_at': datetime.now().isoformat(timespec='seconds')})
        req['snapshot_hash'] = hashlib.sha256(dump(req['survey_snapshot']).encode()).hexdigest()
        return self.worker.enqueue(req, owner)

    def run(self, job_id, req, event):
        from local_ai_runtime import OllamaError
        snap = req['survey_snapshot']
        self.worker._progress(job_id, '서버 집계와 조건별 근거로 Survey 분석을 생성하고 있습니다.')
        task = ('선택 논문 각각의 저장된 성능과 서로 다른 조건을 설명하세요. 각 논문당 관찰을 최소 하나 작성하세요.' if req['kind'] == 'survey_compare'
                else 'S1의 전체 집계에 대한 관찰 하나와 개별 대표 연구에 대한 관찰 2~3개를 작성하세요. 연도별 추세 통계는 없으므로 개선 추세를 추론하지 마세요.')
        body = {'task': task + ' 총 3~5개 observation만 작성하세요. 각 observation은 source_id 하나에만 근거해야 합니다. S1은 전체 통계 전용이며 개별 논문 설명에 인용하지 마세요. '
                'statement는 한국어 한 문장(150자 이하), source_id는 별도 필드에 작성하고 statement에 [S1] 등을 쓰지 마세요. '
                'evidence_index는 해당 source의 evidence_lines에서 선택한 줄의 0부터 시작하는 번호입니다. '
                'statement의 모든 숫자와 범위는 선택한 줄 안에 있어야 합니다. 서로 다른 줄/그룹을 결합하지 마세요. '
                '평균 통계는 없습니다. energy_median은 반드시 중앙값이라고 쓰세요. '
                '출처에 없는 수치, 단위 변환, 다른 동작점의 결합, 새로운 계산을 금지합니다. '
                '한 논문의 값을 전체 집단이나 다른 논문으로 일반화하지 마세요. PDF를 읽었다고 말하지 마세요.',
                'question': req['question'],
                'sources': [{k: s[k] for k in ('source_id', 'evidence_lines')} for s in snap['sources']],
                'limitations': snap['warnings']}
        observations, rejected, attempts, usage = [], 0, 0, []
        schema = json.loads(json.dumps(SURVEY_SCHEMA))
        schema['properties']['observations']['minItems'] = 3
        schema['properties']['observations']['items']['properties']['source_id']['enum'] = [s['source_id'] for s in snap['sources']]
        system = SYSTEM.replace('Cite supplied source IDs as [S1] etc beside claims.', 'Use the separate source_id field for each observation.')
        for attempt in range(2):
            if event.is_set():
                raise RuntimeError('분석이 취소되었습니다.')
            attempts += 1
            try:
                generated, run_usage = self.worker._chat(req, [{'role': 'system', 'content': system},
                    {'role': 'user', 'content': dump(body)}], schema, event)
                usage.append(run_usage)
                observations, rejected = checked_observations(generated, snap['sources'])
                if observations:
                    break
            except (ValueError, OllamaError) as exc:
                if attempt or (isinstance(exc, OllamaError) and not re.search('json|schema', str(exc), re.I)):
                    raise
            body['retry_note'] = '이전 응답은 형식 또는 근거 검증에 실패했습니다. 올바른 evidence_index를 선택하고 해당 줄의 수치만 사용해 짧게 설명하세요.'
            self.worker._progress(job_id, '응답 형식·제공 근거·수치·단위를 재확인하며 한 번 다시 생성하고 있습니다.')
        if not observations:
            raise ValueError('제공 근거·수치·단위 대조를 통과한 설명이 없습니다. 집계만 확인하고 더 좁은 조건으로 다시 요청하세요.')
        result = {'answer': '\n\n'.join(f"{o['statement']} [{o['source_id']}]" for o in observations),
                  'citations': sorted({o['source_id'] for o in observations}), 'observations': observations,
                  'validation': {'accepted': len(observations), 'rejected': rejected, 'attempts': attempts},
                  'limitations': []}
        warnings = list(snap['warnings'])
        warnings.append('표시 문장은 출처 ID·제공 근거 줄·숫자·단위 대조를 통과했습니다. 범위·논리 해석의 정확성까지 검증한 것은 아닙니다. 근거 줄은 DB 값을 정리한 것이며 PDF 원문 발췌가 아닙니다.')
        if rejected:
            warnings.append(f'근거 대조를 통과하지 못한 설명 {rejected}개는 표시하지 않았습니다.')
        if req['kind'] == 'survey_compare':
            missing = {s['source_id'] for s in snap['sources'] if s['article_number']} - set(result['citations'])
            if missing:
                warnings.append('일부 선택 논문에 대해 검증 가능한 AI 설명이 없습니다. 위 서버 비교표를 확인하세요: ' + ', '.join(sorted(missing)))
        if re.search(r'world.best|state.of.the.art|세계\s*최초|가장\s*(?:낮|높|좋|우수)|최고\s*(?:성능|효율)', result['answer'], re.I):
            warnings.append('비교 우위 표현은 제한된 표본에 대한 AI 의견이며 별도 원문 검토가 필요합니다.')
        result.update(sources=snap['sources'], summary=snap['summary'], comparison=snap['comparison'],
                      captured_at=snap['captured_at'], sample_count=snap['sample_count'], usage={'runs': usage},
                      grounding='needs_review', snapshot_hash=req['snapshot_hash'])
        result['limitations'] = result.get('limitations', []) + warnings
        return result

    def gaps(self):
        package = self.worker.repo.review_queue(limit=100)
        grouped = {}
        for raw in package.get('review_queue', []) + package.get('missing_fields', []):
            key = str(raw.get('article_number') or '')
            if not key:
                continue
            row = grouped.setdefault(key, {'article_number': key, 'flags': [], 'missing_fields': [], 'priority': 'P2'})
            row['flags'] = sorted(set(row['flags'] + raw.get('flag_codes', [])))
            row['missing_fields'] = sorted(set(row['missing_fields'] + raw.get('missing_fields', [])))
            row['priority'] = min(row['priority'], raw.get('priority', 'P2'))
        keys = list(grouped)
        papers = []
        for start in range(0, len(keys), 50):
            papers.extend(self.worker.repo.get_papers(keys[start:start+50]))
        items = [{**grouped[p['article_number']], **{k: p.get(k) for k in ('article_number', 'title', 'year', 'source_name', 'is_favorite', 'pdf_available')}} for p in papers]
        items.sort(key=lambda p: (p['priority'], not p['pdf_available'], not p['is_favorite'], -len(p['missing_fields']), -int(p.get('year') or 0)))
        return {'items': items[:30], 'candidate_count': len(items), 'returned_count': min(30, len(items)),
                'basis': 'Core + Adjacent 전체의 규칙 기반 충돌·누락 후보입니다. 차트/Explorer 필터와 무관하며 즐겨찾기도 포함합니다.',
                'priority_note': 'P0 충돌 → P1 검토 → P2 누락 순, 같은 등급에서는 PDF·즐겨찾기 우선. 각 후보군 최대 100건에서 30편 표시.'}
