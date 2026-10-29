"""Isolated 2x2 review benchmark. Never changes or overlaps the frozen census.

Scoring labels never enter model messages. Controlled corruptions are derived
from gold and reported separately. A fixed seed is added to every arm,
including its freshly inferred baseline. Cross-document packing tests review of
individual citations, not PDF segmentation or full-bibliography completeness.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import diagnose_reference_corpus as corpus
from scripts.replay_reference_pipeline import evaluation_field_key

CENSUS = ROOT/'outputs/reference_corpus_diagnostic_20261018'
DEFAULT = ROOT/'outputs/reference_review_benchmark_20261019'
FIELDS = ('title', 'author', 'year', 'doi')
ARMS = {'baseline': (False, 5), 'compact': (True, 5),
        'batch10': (False, 10), 'compact_batch10': (True, 10)}
VERSION = 'review-ablation-v1'
ACTIONABLE = {'unsupported_value', 'wrong_role', 'missing_value', 'boundary_error'}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def event(out, stage, **values):
    corpus.log(out, stage, **values)


def original_prompt():
    tree = ast.parse(inspect.getsource(corpus.review_batch))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'messages' for t in n.targets))
    return ast.literal_eval(node.value.elts[0])['content']


def prompt(compact):
    value = original_prompt()
    if compact:
        suffix = 'Return one review per input number in order, with at most four issues and concise reasons.'
        assert value.endswith(suffix)
        value = value[:-len(suffix)] + (
            'Return the JSON object with ok and review arrays. Put an input number in ok ONLY when '
            'its verdict is no_issue_seen and it has no issues. Put every other input in review, '
            'with its number, needs_review or unverifiable verdict, and full issues. '
            'Preserve input order within each array. Cover EVERY input number exactly once across '
            'both arrays. Keep exact evidence quotes and concise reasons, at most four issues per input.')
    return value


def schema(compact):
    if not compact:
        return corpus.review_schema()
    row = deepcopy(corpus.review_schema()['properties']['reviews']['items'])
    row['properties']['verdict']['enum'] = ['needs_review', 'unverifiable']
    return {'type': 'object', 'properties': {
        'ok': {'type': 'array', 'items': {'type': 'integer'}},
        'review': {'type': 'array', 'items': row}},
        'required': ['ok', 'review'], 'additionalProperties': False}


def expand(answer, inputs, compact):
    if not compact:
        return answer
    numbers = [r['number'] for r in inputs]
    ok, flagged = answer['ok'], answer['review']
    all_ids = ok + [r['number'] for r in flagged]
    if (any(type(n) is not int for n in all_ids) or len(all_ids) != len(set(all_ids))
            or set(all_ids) != set(numbers)):
        raise ValueError('Compact response lost, duplicated, or invented identities')
    if ok != [n for n in numbers if n in set(ok)]:
        raise ValueError('Compact ok IDs changed order')
    if [r['number'] for r in flagged] != [n for n in numbers if n not in set(ok)]:
        raise ValueError('Compact review IDs changed order')
    by_id = {r['number']: r for r in flagged}
    return {'reviews': [({'number': n, 'verdict': 'no_issue_seen', 'issues': []}
                         if n in set(ok) else by_id[n]) for n in numbers]}


def pack(inputs, max_items):
    batch, characters = [], 0
    for item in inputs:
        size = len(json.dumps(item, ensure_ascii=False))
        if batch and (len(batch) >= max_items or characters + size > 6000):
            yield batch
            batch, characters = [], 0
        # Keep the production context rejection policy. No truncation.
        if size > 12000:
            raise ValueError('Benchmark input exceeds production context bound')
        batch.append(item)
        characters += size
    if batch:
        yield batch


def make_input(row):
    item = {'number': row['number'], 'raw': row['raw'],
            'source_raw': row.get('source_raw', row['raw']),
            'independent_source': row.get('source_view', {}).get('raw', ''),
            'candidate': {f: row.get('title_display', row.get('title')) if f == 'title'
                          else row.get(f) for f in FIELDS},
            'pipeline_review_marked': bool(row.get('needs_review'))}
    if item['source_raw'] == item['raw']:
        item['source_raw'] = ''
    if item['independent_source'] in (item['raw'], item['source_raw']):
        item['independent_source'] = ''
    return item


def bad_fields(candidate, expected):
    return [f for f in FIELDS if f in expected and
            evaluation_field_key(candidate.get(f), f) != evaluation_field_key(expected[f], f)]


def setup(out):
    if (out/'protocol.json').exists():
        verify(out)
        return
    out.mkdir(parents=True, exist_ok=True)
    corpus.verify(CENSUS)
    manifest = corpus.bulk.read(CENSUS/'manifest.json')
    by_id = {p['id']: p for p in manifest['papers']}
    cases, ledger, excluded = [], {}, []
    for folder in ('reference_validation_100_20261017', 'reference_validation_fresh100_20261017'):
        source = ROOT/'outputs'/folder
        freeze = corpus.bulk.read(source/'gold-freeze.json')
        for name, expected in freeze['files'].items():
            if corpus.digest(source/name) != expected:
                raise ValueError('Source-reviewed evidence changed: '+name)
        if corpus.digest(source/'manifest.json') != freeze['manifest_sha256']:
            raise ValueError('Gold selection changed')
        ledger[str((source/'gold-freeze.json').relative_to(ROOT))] = corpus.digest(source/'gold-freeze.json')
        selected = corpus.bulk.read(source/'manifest.json')
        for paper in selected['papers']:
            directory = CENSUS/'papers'/paper['id']
            result = corpus.bulk.read(directory/'result.json')
            prep = corpus.bulk.read(directory/'prepared.json')
            rows = {r['number']: r for r in result['rows']}
            gold_path = source/'gold'/(paper['audit_id']+'.json')
            ledger[str(gold_path.relative_to(ROOT))] = corpus.digest(gold_path)
            gold = corpus.bulk.read(gold_path)
            ledger[str((directory/'result.json').relative_to(ROOT))] = corpus.digest(directory/'result.json')
            context = corpus.coverage_context(prep, set(rows), paper['sha256'])
            for expected in gold['references']:
                row = rows.get(expected['number'])
                if row is None or corpus.standalone_url(row['raw']):
                    excluded.append({'pdf': paper['pdf'], 'number': expected['number'],
                                     'reason': 'missing_pipeline_row' if row is None else 'URL_only_production_policy'})
                    continue
                item = make_input(row)
                if row['number'] == 1:
                    item['coverage_context'] = context
                truth = {f: expected[f] for f in FIELDS if f in expected}
                cases.append({'kind': 'natural', 'pdf': paper['pdf'], 'paper_id': paper['id'],
                    'original_number': row['number'], 'cohort': folder,
                    'input': item, 'expected': truth,
                    'bad_fields': bad_fields(item['candidate'], truth)})
    natural = list(cases)
    # Controlled errors are separate from naturally observed errors/prevalence.
    for field in FIELDS:
        eligible = []
        for case in natural:
            value = case['expected'].get(field)
            evidence = ' '.join(case['input'].get(k, '') for k in ('raw', 'source_raw', 'independent_source'))
            key = evaluation_field_key(value, field)
            source_key = evaluation_field_key(evidence, field)
            if value is not None and key and str(key) in str(source_key):
                eligible.append(case)
        eligible.sort(key=lambda c: sha([field, c['paper_id'], c['original_number']]))
        for index, base in enumerate(eligible[:16]):
            case = deepcopy(base)
            case['kind'] = 'controlled_missing' if index % 2 else 'controlled_wrong_value'
            case['changed_field'] = field
            case['input']['candidate'] = dict(base['expected'])
            if index % 2:
                replacement = None
            elif field == 'year':
                replacement = int(base['expected'][field]) + 1
            elif field == 'doi':
                value = str(base['expected'][field])
                replacement = value[:-1] + ('1' if value[-1] != '1' else '2')
            else:
                replacement = next(c['expected'][field] for c in eligible
                    if evaluation_field_key(c['expected'][field], field) != evaluation_field_key(base['expected'][field], field)
                    and str(evaluation_field_key(c['expected'][field], field)) not in str(evaluation_field_key(base['input']['raw'], field)))
            case['input']['candidate'][field] = replacement
            case['bad_fields'] = bad_fields(case['input']['candidate'], case['expected'])
            assert case['bad_fields'] == [field]
            cases.append(case)
    # Unlabelled transfer cases only assess schema/grounding and baseline agreement.
    added = Counter()
    for paper in manifest['papers']:
        if paper['cohort'] != 'new_to_pipeline':
            continue
        group = paper['publisher']
        if group not in ('DesignCon', 'nature-pdf', 'optica-pdf') or added[group] >= 8:
            continue
        directory = CENSUS/'papers'/paper['id']
        result = corpus.bulk.read(directory/'result.json')
        for row in result['rows'][:2]:
            if added[group] >= 8 or corpus.standalone_url(row['raw']):
                continue
            cases.append({'kind': 'unlabelled_transfer', 'pdf': paper['pdf'], 'paper_id': paper['id'],
                'original_number': row['number'], 'cohort': 'unlabelled_transfer', 'input': make_input(row),
                'expected': {}, 'bad_fields': []})
            added[group] += 1
        if all(added[g] >= 8 for g in ('DesignCon', 'nature-pdf', 'optica-pdf')):
            break
    random.Random(20261019).shuffle(cases)
    for number, case in enumerate(cases, 1):
        case['input']['number'] = number
        case['id'] = sha([case['paper_id'], case['original_number'], case['kind'], case.get('changed_field')])[:24]
    corpus.save(out/'cases.json', cases)
    corpus.save(out/'source-ledger.json', ledger)
    policy = corpus.bulk.read(CENSUS/'policy.json')
    protocol = {'version': VERSION, 'created_at': now(), 'model': policy['model'],
        'model_digest': policy['model_digest'], 'num_ctx': 8192, 'max_tokens': 2048,
        'temperature': .1, 'thinking': False, 'keep_alive': 120, 'seed': 42,
        'arms': ARMS, 'block_size': 20, 'timing_repeat_blocks': 2, 'timing_repeat_seeds': [53, 67],
        'cases_sha256': corpus.digest(out/'cases.json'), 'ledger_sha256': corpus.digest(out/'source-ledger.json'),
        'cases': len(cases), 'kinds': Counter(c['kind'] for c in cases),
        'natural_source_papers': len({c['paper_id'] for c in natural}), 'source_reviewed_papers': 200,
        'excluded': excluded, 'natural_mismatch_references': sum(bool(c['bad_fields']) for c in natural),
        'natural_mismatch_fields': sum(len(c['bad_fields']) for c in natural),
        'limitations': ['Source gold is assistant reviewed, not independently human certified.',
            'Previous 200-paper development/validation data; not a fresh generalization estimate.',
            'Controlled corruptions assess sensitivity, not real-world error prevalence.',
            'Transfer cases have no gold: agreement/grounding only, never scored as accuracy.',
            'All arms use cross-document packing of sampled citations, not full-PDF completeness testing.',
            'One fixed main seed; two extra timing-panel seeds do not establish general stochastic equivalence.'],
        'no_external_academic_api': True, 'no_sql_writes': True,
        'inference_starts_only_after_census_final_verification': True}
    corpus.save(out/'protocol.json', protocol)
    corpus.save(out/'artifact-freeze.json', {n:corpus.digest(out/n) for n in
        ('protocol.json','cases.json','source-ledger.json')})
    corpus.save(out/'benchmark-code.json', {**corpus.snapshot(CENSUS),
        'scripts/benchmark_reference_review.py': corpus.digest(Path(__file__)),
        'scripts/review_reference_validation.py': corpus.digest(ROOT/'scripts/review_reference_validation.py')})
    verify(out)


def verify(out, full_sources=True):
    corpus.verify(CENSUS)
    for name, expected in corpus.bulk.read(out/'artifact-freeze.json').items():
        if corpus.digest(out/name) != expected:
            raise ValueError('Frozen benchmark artifact changed: '+name)
    for name, expected in corpus.bulk.read(out/'benchmark-code.json').items():
        if corpus.digest(ROOT/name) != expected:
            raise ValueError('Benchmark code changed: '+name)
    policy = corpus.bulk.read(out/'protocol.json')
    if corpus.digest(out/'cases.json') != policy['cases_sha256'] or corpus.digest(out/'source-ledger.json') != policy['ledger_sha256']:
        raise ValueError('Benchmark inputs changed')
    if full_sources:
        for name, expected in corpus.bulk.read(out/'source-ledger.json').items():
            if corpus.digest(ROOT/name) != expected:
                raise ValueError('Scoring source changed: '+name)


class SeededClient(corpus.bulk.OllamaClient):
    def __init__(self, seed):
        super().__init__(deadline_seconds=240)
        self.seed = seed

    def _session(self):
        session = super()._session()
        post = session.post
        def seeded_post(url, **kwargs):
            if 'json' in kwargs:
                payload = dict(kwargs['json'])
                payload['options'] = {**payload.get('options', {}), 'seed': self.seed}
                kwargs['json'] = payload
            return post(url, **kwargs)
        session.post = seeded_post
        return session


def messages(inputs, compact):
    # Evaluation labels/metadata are never passed here.
    return [{'role': 'system', 'content': prompt(compact)},
            {'role': 'user', 'content': '<untrusted_references>\n'+json.dumps(inputs, ensure_ascii=False)+'\n</untrusted_references>'}]


def request(out, client, guard, inputs, compact, policy, calls):
    corpus.wait_for_model(out, client, policy['model_digest'])
    guard.wait()
    record = {'numbers': [r['number'] for r in inputs], 'references': len(inputs), 'started_at': now()}
    started = time.monotonic()
    try:
        response = client.chat(policy['model'], messages(inputs, compact), num_ctx=8192,
            max_tokens=2048, thinking='off', schema=schema(compact), keep_alive=120)
        record['usage'] = response.get('usage', {})
        record['output_characters'] = len(response['content'])
        if response.get('done_reason') == 'length' or response.get('usage', {}).get('prompt_eval_count', 0) > 5900:
            raise ValueError('Output truncated or input exceeds full-review context budget')
        expanded = expand(response['json'], inputs, compact)
        rows = corpus.validate_reviews(expanded, inputs)
    except (ValueError, RuntimeError) as exc:
        record.update(success=False, error=type(exc).__name__, seconds=time.monotonic()-started)
        calls.append(record)
        if len(inputs) > 1:
            middle = len(inputs)//2
            rows = request(out, client, guard, inputs[:middle], compact, policy, calls)
            rows += request(out, client, guard, inputs[middle:], compact, policy, calls)
            return rows
        return [{'number': inputs[0]['number'], 'verdict': 'unverifiable', 'issues': [],
                 'review_error': type(exc).__name__, 'rejected_ungrounded_issues': []}]
    record.update(success=True, seconds=time.monotonic()-started)
    calls.append(record)
    return rows


def schedule(cases, policy):
    blocks = [cases[i:i+policy['block_size']] for i in range(0, len(cases), policy['block_size'])]
    jobs = []
    for phase, seed, selected in [('main', policy['seed'], list(enumerate(blocks)))]+[
            ('repeat'+str(i+1), seed, list(enumerate(blocks[:policy['timing_repeat_blocks']])))
            for i, seed in enumerate(policy['timing_repeat_seeds'])]:
        for block, values in selected:
            names = list(ARMS)
            rotation = (block + seed) % len(names)
            for arm in names[rotation:]+names[:rotation]:
                jobs.append({'phase': phase, 'seed': seed, 'block': block, 'arm': arm,
                             'numbers': [c['input']['number'] for c in values]})
    return jobs


def wilson(k, n):
    if not n:
        return None
    z = 1.959963984540054
    center = (k/n+z*z/(2*n))/(1+z*z/n)
    radius = z*math.sqrt(k/n*(1-k/n)/n+z*z/(4*n*n))/(1+z*z/n)
    return [max(0, center-radius), min(1, center+radius)]


def score(cases, rows):
    by_number = {r['number']: r for r in rows}
    counters = defaultdict(Counter); detected = set(); false_claims = set(); outcomes = {}
    fields = defaultdict(lambda:defaultdict(Counter))
    for case in cases:
        number = case['input']['number']
        row = by_number.get(number)
        group = 'controlled' if case['kind'].startswith('controlled_') else case['kind']
        c = counters[group];c['references'] += 1
        if row is None:
            c['missing_outputs'] += 1
            continue
        c[row['verdict']] += 1;c['review_errors'] += bool(row.get('review_error'))
        c['rejected_ungrounded_claims'] += len(row.get('rejected_ungrounded_issues', []))
        outcomes[number] = row['verdict']
        if not case['expected']:
            continue
        mismatch = set(case['bad_fields'])
        alerted = {i['field'] for i in row['issues']}
        actionable = {i['field'] for i in row['issues'] if i['kind'] in ACTIONABLE}
        c['known_bad_references'] += bool(mismatch)
        c['flagged_bad_references'] += bool(mismatch) and row['verdict'] != 'no_issue_seen'
        c['known_correct_references'] += not mismatch
        c['review_marked_correct_references'] += not mismatch and row['verdict'] != 'no_issue_seen'
        for f in case['expected']:
            fields[group][f]['checked'] += 1
            if f in mismatch:
                c['known_bad_fields'] += 1
                c['localized_bad_fields'] += f in alerted
                fields[group][f]['known_bad'] += 1
                fields[group][f]['detected'] += f in alerted
                if f in alerted:
                    detected.add((number, f))
            else:
                c['known_matching_fields'] += 1
                c['actionable_claims_on_matching_fields'] += f in actionable
                fields[group][f]['matching'] += 1
                fields[group][f]['false_claims'] += f in actionable
                if f in actionable:
                    false_claims.add((number, f))
    for c in counters.values():
        c['field_detection_recall'] = c['localized_bad_fields']/c['known_bad_fields'] if c['known_bad_fields'] else None
        c['recall_wilson_95'] = wilson(c['localized_bad_fields'], c['known_bad_fields'])
    return {'groups': counters, 'fields': fields, 'detected_fields': sorted(detected),
            'false_claim_fields': sorted(false_claims), 'verdicts': outcomes}


def report(out):
    policy = corpus.bulk.read(out/'protocol.json');cases = corpus.bulk.read(out/'cases.json')
    jobs = schedule(cases, policy);arm_data = {};scoring = {};finished = 0
    for arm in ARMS:
        completed = []
        for job in jobs:
            if job['arm'] != arm:
                continue
            path = out/'results'/(sha(job)+'.json')
            if path.exists():
                completed.append(corpus.bulk.read(path));finished += 1
        main = [r for r in completed if r['job']['phase'] == 'main']
        rows = [row for r in main for row in r['rows']]
        counts = Counter();usage = Counter()
        for r in completed:
            counts['wall_seconds'] += r['wall_seconds'];counts['references'] += len(r['rows'])
            counts['fresh_calls'] += len(r['calls']);counts['failed_calls'] += sum(not c['success'] for c in r['calls'])
            counts['output_characters'] += sum(c.get('output_characters', 0) for c in r['calls'])
            for call in r['calls']:
                usage.update({k: v for k, v in call.get('usage', {}).items() if isinstance(v, (int, float))})
        counts['references_per_hour'] = counts['references']*3600/counts['wall_seconds'] if counts['wall_seconds'] else None
        selected = [c for c in cases if c['input']['number'] in {r['number'] for r in rows}]
        scoring[arm] = score(selected, rows)
        arm_data[arm] = {'completed_jobs': len(completed), 'main_references': len(rows),
                         'timing': counts, 'usage': usage, 'quality': scoring[arm]['groups'],
                         'quality_by_field':scoring[arm]['fields']}
    baseline = scoring['baseline']
    paired = {}
    for arm in list(ARMS)[1:]:
        common = set(baseline['verdicts']) & set(scoring[arm]['verdicts'])
        base_detected = {tuple(x) for x in baseline['detected_fields'] if x[0] in common}
        new_detected = {tuple(x) for x in scoring[arm]['detected_fields'] if x[0] in common}
        base_false = {tuple(x) for x in baseline['false_claim_fields'] if x[0] in common}
        new_false = {tuple(x) for x in scoring[arm]['false_claim_fields'] if x[0] in common}
        ratios = [];main_baseline_seconds=main_arm_seconds=0
        for job in jobs:
            if job['arm'] != arm:
                continue
            a=out/'results'/(sha(job)+'.json');b=out/'results'/(sha({**job,'arm':'baseline'})+'.json')
            if a.exists() and b.exists():
                seconds_a=corpus.bulk.read(a)['wall_seconds'];seconds_b=corpus.bulk.read(b)['wall_seconds']
                ratios.append(seconds_b/seconds_a)
                if job['phase']=='main':
                    main_baseline_seconds+=seconds_b;main_arm_seconds+=seconds_a
        review_errors = sum(arm_data[arm]['quality'][g].get('review_errors',0)
                            +arm_data[arm]['quality'][g].get('missing_outputs',0)
                            for g in arm_data[arm]['quality'])
        paired[arm] = {'paired_references': len(common),
            'lost_baseline_detections': sorted(base_detected-new_detected),
            'added_detections': sorted(new_detected-base_detected),
            'added_false_claims': sorted(new_false-base_false),
            'removed_false_claims': sorted(base_false-new_false),
            'verdict_disagreements': sum(baseline['verdicts'][n] != scoring[arm]['verdicts'][n] for n in common),
            'paired_block_speed_factors': ratios,
            'paired_block_speed_median':statistics.median(ratios) if ratios else None,
            'main_paired_speed_factor':main_baseline_seconds/main_arm_seconds if main_arm_seconds else None,
            'meets_strict_observed_quality_gate': (not (base_detected-new_detected) and not (new_false-base_false)
                and not review_errors) if len(common)==len(cases) else None}
    complete = finished == len(jobs)
    value = {'at': now(), 'status': 'complete' if complete else 'running', 'finished_jobs': finished,
             'total_jobs': len(jobs), 'arms': arm_data, 'paired': paired, 'limitations': policy['limitations'],
             'adoption':'No production settings changed. Observed equality is not proof of general noninferiority.'}
    corpus.save(out/'comparison.json', value)
    lines = ['로컬 LLM 참고문헌 재검토: 응답 축약·배치 2x2 비교',
             f'상태: {value["status"]}; 완료 작업 {finished}/{len(jobs)}; 갱신 {value["at"]}',
             '진행 중 수치는 일부 블록의 중간 집계다. 최종 비교와 구분한다.',
             '모든 조건은 동일 원문·모델·8K 문맥·출력 한도·temperature·seed를 사용한다.',
             '자연 자료의 후보는 현재 추출 결과다. 대조군은 정답에서 오류를 주입해 생성하며 별도로 집계한다.',
             '정답·오류 주입 여부 등의 평가 라벨은 모델 메시지에 전달하지 않는다.',
             '자연 오류 / 인위적 오류 / 정답 없는 새 형식 자료를 구분한다.', '']
    for arm, data in arm_data.items():
        speed=data['timing']['references_per_hour']
        lines.append(f'{arm}: 본 평가 {data["main_references"]}항목; 속도 {speed:.1f}항목/시간' if speed else f'{arm}: 대기')
        for group, q in data['quality'].items():
            lines.append(f'  {group}: 오류 필드 지적 {q["localized_bad_fields"]}/{q["known_bad_fields"]}; '
                         f'일치 필드에 대한 오류 주장 {q["actionable_claims_on_matching_fields"]}; '
                         f'미완료 응답 {q["missing_outputs"]+q["review_errors"]}')
    lines += ['', '조건별 전체 지적·누락·근거와 블록별 시간: comparison.json 및 results/',
              '같은 모델의 판정 일치가 실제 정확도를 보장하지 않는다. 검증된 오류 놓침과 오탐을 따로 비교한다.',
              '현재 전수 진단 코드·설정·결과에는 변경을 적용하지 않았다.']
    (out/'COMPARISON_REPORT.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    return value


def run(out):
    setup(out);verify(out)
    if (out/'final-verification.json').exists():
        raise ValueError('Completed benchmark is immutable')
    corpus.save(out/'RUN_STATE.json', {'status': 'waiting_for_census', 'at': now()})
    while not (CENSUS/'final-verification.json').exists():
        verify(out,full_sources=False)
        state = corpus.bulk.read(CENSUS/'RUN_STATE.json')
        if state['status'] == 'failed':
            raise RuntimeError('Census needs attention; benchmark did not start inference')
        time.sleep(15)
    census_final = corpus.bulk.read(CENSUS/'final-verification.json')
    if census_final['status'] != 'complete':
        raise ValueError('Census final verification is not complete')
    policy = corpus.bulk.read(out/'protocol.json');cases = corpus.bulk.read(out/'cases.json')
    by_number = {c['input']['number']:c for c in cases};guard = corpus.bulk.IdleGuard()
    try:
        corpus.save(out/'RUN_STATE.json', {'status': 'running', 'at': now()})
        for index, job in enumerate(schedule(cases, policy), 1):
            path=out/'results'/(sha(job)+'.json')
            if path.exists():
                continue
            # Gold never enters the inference objects.
            for name, expected in corpus.bulk.read(out/'benchmark-code.json').items():
                if corpus.digest(ROOT/name) != expected:
                    raise ValueError('Frozen benchmark code changed')
            inputs = [deepcopy(by_number[n]['input']) for n in job['numbers']]
            compact,max_items=ARMS[job['arm']]
            client=SeededClient(job['seed']);calls=[];rows=[]
            started=time.monotonic()
            event(out,'benchmark_job_start',job=job,index=index)
            for batch in pack(inputs,max_items):
                rows.extend(request(out,client,guard,batch,compact,policy,calls))
            if [r['number'] for r in rows] != job['numbers']:
                raise ValueError('Lost review identity after recovery')
            corpus.save(path,{'job':job,'input_sha256':sha(inputs),'rows':rows,'calls':calls,
                             'wall_seconds':time.monotonic()-started,'finished_at':now()})
            event(out,'benchmark_job_finish',arm=job['arm'],index=index)
            report(out)
        verify(out);summary=report(out)
        corpus.save(out/'final-verification.json',{'status':'complete','at':now(),
            'code_and_input_hashes_verified':True,'finished_jobs':summary['finished_jobs'],
            'census_final_verification_sha256':corpus.digest(CENSUS/'final-verification.json'),
            'production_changes':0})
        corpus.save(out/'RUN_STATE.json',{'status':'complete','at':now()})
    except BaseException as exc:
        corpus.save(out/'RUN_STATE.json',{'status':'failed','error':type(exc).__name__,'message':str(exc)[:400],'at':now()})
        raise
    finally:
        guard.engine.dispose()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=DEFAULT)
    parser.add_argument('--stage',choices=('setup','run','report'),required=True)
    args=parser.parse_args();out=args.out.resolve()
    with corpus.bulk.stage_lock(out,'benchmark-'+args.stage):
        {'setup':setup,'run':run,'report':report}[args.stage](out)
