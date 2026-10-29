"""Run the actual text-first batch flow on both frozen source-reviewed cohorts.

Preparation and enrichment never read gold or previous final rows. Gold is read
only by report(). Exact upstream parser/model caches may be reused, with hashes.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import benchmark_reference_bulk as bulk
from scripts.replay_reference_pipeline import evaluation_field_key

COHORTS = {
    'development100': ('outputs/reference_validation_100_20261017',
                       'outputs/reference_pipeline_v7_micro_u_20261017'),
    'followup100': ('outputs/reference_validation_fresh100_20261017',
                   'outputs/reference_ocr_fallback_20261017/final100-regression'),
}
CACHE_SOURCE = ROOT / 'outputs/reference_bulk_500_20261017'


def log(out, stage, **values):
    data = {'at': datetime.now(timezone.utc).isoformat(), 'stage': stage, **values}
    with (out / 'runtime-events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(data, ensure_ascii=True) + '\n')


def frozen_files():
    paths = list((ROOT / 'paper_server/parsers').glob('reference*.py'))
    paths += [ROOT / p for p in ('scripts/benchmark_reference_bulk.py',
        'scripts/backtest_reference_flow.py', 'scripts/replay_reference_pipeline.py',
        'scripts/reference_evidence_worker.py', 'scripts/reference_ocr_worker.py',
        'scripts/reference_ocr.cjs', 'scripts/mineru_reference_worker.py',
        'scripts/GrobidReferenceBatch.java', 'paper_server/ai/runtime.py',
        'paper_server/citation_fallback.py', 'config/reference_authorities.json',
        'config/reference_source_reviews.json')]
    return {str(p.relative_to(ROOT)): bulk.digest_file(p) for p in paths}


def verify(out):
    snapshot = bulk.read(out / 'pipeline-code.json')
    for name, digest in snapshot.items():
        if bulk.digest_file(ROOT / name) != digest:
            raise ValueError('Frozen pipeline changed: ' + name)
    for validation, _ in COHORTS.values():
        validation = ROOT / validation
        freeze = bulk.read(validation / 'gold-freeze.json')
        for name, digest in freeze['files'].items():
            if bulk.digest_file(validation / name) != digest:
                raise ValueError('Frozen source/gold changed: ' + name)


def setup(out):
    if out.exists():
        raise FileExistsError('Use a new backtest directory')
    out.mkdir(parents=True)
    papers, seen = [], set()
    for cohort, (validation, _) in COHORTS.items():
        manifest = bulk.read(ROOT / validation / 'manifest.json')
        for paper in manifest['papers']:
            if paper['sha256'] in seen:
                raise ValueError('Cohorts overlap')
            seen.add(paper['sha256'])
            papers.append({**paper, 'cohort': cohort})
    bulk.save(out / 'manifest.json', {'version': 'reference-flow-backtest-v1',
        'papers': papers, 'selected': len(papers), 'selection': 'Both fixed non-overlapping source-reviewed cohorts'})
    bulk.save(out / 'pipeline-code.json', frozen_files())
    bulk.save(out / 'policy.json', {'scope': 'Development backtest, not new independent accuracy',
        'actual_flow': 'New prepare -> new GROBID/consensus -> exact-input LLM cache or local LLM -> reconciliation',
        'no_previous_final_rows_as_input': True, 'cohorts': COHORTS,
        'runner_sha256': bulk.digest_file(Path(__file__)), 'external_api_calls': 0, 'sql_writes': 0})
    verify(out)
    caches = []
    for paper in papers:
        directory = CACHE_SOURCE / 'papers' / paper['id'] / 'mineru'
        for name in ('structured_content.json', 'provenance.json'):
            source = directory / name
            if source.exists():
                target = out / 'papers' / paper['id'] / 'mineru' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                caches.append({'kind': 'mineru', 'source': str(source),
                               'target': str(target.relative_to(out)), 'sha256': bulk.digest_file(target)})
    for source in (CACHE_SOURCE / 'llm').glob('*.json'):
        target = out / 'llm' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        caches.append({'kind': 'llm', 'source': str(source),
                       'target': str(target.relative_to(out)), 'sha256': bulk.digest_file(target)})
    bulk.save(out / 'upstream-cache-origins.json', caches)
    log(out, 'setup', papers=len(papers), upstream_cache_files=len(caches))
    print(json.dumps({'stage': 'setup', 'papers': len(papers), 'cache_files': len(caches)}))


def prepare(out):
    verify(out)
    started = time.monotonic()
    log(out, 'prepare_start')
    bulk.prepare(out)
    log(out, 'prepare_finish', seconds=round(time.monotonic() - started, 3))
    verify(out)


def enrich(out):
    verify(out)
    base_client, grobid = bulk.OllamaClient, bulk.grobid_raw
    original_batch = bulk.llm_batch
    class TrackedClient(base_client):
        def chat(self, model, *args, **kwargs):
            started = time.monotonic()
            log(out, 'llm_call_start', model=model)
            try:
                result = super().chat(model, *args, **kwargs)
            except Exception as exc:
                log(out, 'llm_call_finish', model=model, success=False,
                    seconds=round(time.monotonic() - started, 3), error=type(exc).__name__)
                raise
            log(out, 'llm_call_finish', model=model, success=True,
                seconds=round(time.monotonic() - started, 3), usage=result.get('usage'))
            return result
    def tracked_grobid(entries, directory):
        started = time.monotonic()
        log(out, 'grobid_start', references=len(entries))
        result = grobid(entries, directory)
        log(out, 'grobid_finish', references=len(entries), seconds=round(time.monotonic() - started, 3))
        return result
    def tracked_batch(client, guard, batch, target, signature):
        signature = bulk.llm_cache_signature(signature)
        key = hashlib.sha256((bulk.llm_input(batch) + json.dumps(signature, sort_keys=True)).encode()).hexdigest()
        path = target / 'llm' / (key + '.json')
        cached = path.exists()
        started = time.monotonic()
        result = original_batch(client, guard, batch, target, signature)
        log(out, 'llm_batch', cached=cached, references=len(batch), key=key,
            seconds=round(time.monotonic() - started, 3))
        return result
    bulk.OllamaClient, bulk.grobid_raw, bulk.llm_batch = TrackedClient, tracked_grobid, tracked_batch
    started = time.monotonic()
    log(out, 'enrich_start')
    try:
        bulk.enrich(out, follow=False)
    finally:
        bulk.OllamaClient, bulk.grobid_raw, bulk.llm_batch = base_client, grobid, original_batch
        log(out, 'enrich_finish', seconds=round(time.monotonic() - started, 3))
    verify(out)


def value(row, field):
    if row is None:
        return None
    return row.get('title_display', row.get('title')) if field == 'title' else row.get(field)


def report(out):
    verify(out)
    manifest = bulk.read(out / 'manifest.json')
    if any(not (out / 'papers' / p['id'] / 'result.json').exists() for p in manifest['papers']):
        raise ValueError('Not all papers have finished')
    fields = {f: Counter() for f in ('title', 'author', 'year', 'doi')}
    papers, mismatches, regressions, improvements = [], [], [], []
    source_counts, statuses = Counter(), Counter()
    operations = Counter()
    for paper in manifest['papers']:
        validation, previous = COHORTS[paper['cohort']]
        expected = bulk.read(ROOT / validation / 'gold' / (paper['audit_id'] + '.json'))
        result = bulk.read(out / 'papers' / paper['id'] / 'result.json')
        prep = bulk.read(out / 'papers' / paper['id'] / 'prepared.json')
        old = bulk.read(ROOT / previous / 'papers' / paper['id'] / 'result.json')
        actual = {r['number']: r for r in result['rows']}
        before = {r['number']: r for r in old['rows']}
        source_counts[result.get('entry_source', 'none')] += 1
        statuses[result['status']] += 1
        operations['mineru_skipped_papers'] += bool(prep.get('mineru_skipped'))
        operations['mineru_cache_hit_papers'] += bool(prep.get('mineru_cache_hit'))
        if prep.get('ocr', {}).get('status') == 'complete':
            operations['ocr_papers'] += 1
            operations['ocr_pages'] += len(prep['ocr']['pages'])
            operations['ocr_cache_hit_papers'] += bool(prep['ocr'].get('cache_hit'))
        operations['references'] += len(actual)
        operations['routed_to_llm'] += result['routed']
        operations['llm_completed'] += result['llm_completed']
        operations['expected_references'] += expected['expected_count']
        errors = 0
        for reference in expected['references']:
            row = actual.get(reference['number'])
            prior = before.get(reference['number'])
            for field in fields:
                if field not in reference:
                    continue
                correct = row is not None and evaluation_field_key(value(row, field), field) == evaluation_field_key(reference[field], field)
                was_correct = prior is not None and evaluation_field_key(value(prior, field), field) == evaluation_field_key(reference[field], field)
                fields[field]['checked'] += 1
                fields[field]['errors'] += not correct
                case = {'cohort': paper['cohort'], 'audit_id': paper['audit_id'], 'pdf': paper['pdf'],
                    'number': reference['number'], 'field': field, 'expected': reference[field],
                    'actual': value(row, field), 'previous': value(prior, field),
                    'row_missing': row is None, 'review_marked': bool(row and row.get('needs_review'))}
                if not correct:
                    mismatches.append(case); errors += 1
                if was_correct and not correct:
                    regressions.append(case)
                if correct and not was_correct:
                    improvements.append(case)
        papers.append({'cohort': paper['cohort'], 'audit_id': paper['audit_id'], 'pdf': paper['pdf'],
            'expected_count': expected['expected_count'], 'actual_count': len(actual), 'previous_count': len(before),
            'count_match': expected['expected_count'] == len(actual), 'sampled_references': len(expected['references']),
            'field_errors': errors, 'status': result['status'], 'source': result.get('entry_source'),
            'ocr_status': prep.get('ocr', {}).get('status'), 'prepare_seconds': prep['elapsed_seconds']})
    events = [json.loads(line) for line in (out / 'runtime-events.jsonl').read_text(encoding='utf-8').splitlines()]
    operations['new_llm_calls'] = sum(e['stage'] == 'llm_call_start' for e in events)
    operations['new_llm_failed_calls'] = sum(e['stage'] == 'llm_call_finish' and not e['success'] for e in events)
    operations['exact_llm_cache_hits'] = sum(e['stage'] == 'llm_batch' and e['cached'] for e in events)
    operations['new_grobid_calls'] = sum(e['stage'] == 'grobid_start' for e in events)
    checked = sum(f['checked'] for f in fields.values())
    output = {'scope': 'Actual flow backtest on two previously reviewed cohorts; not independent new accuracy',
        'papers': len(papers), 'sampled_references': sum(p['sampled_references'] for p in papers),
        'fields_checked': checked, 'field_errors': len(mismatches),
        'field_error_rate_percent': round(100 * len(mismatches) / checked, 3),
        'count_exact_papers': sum(p['count_match'] for p in papers), 'fields': fields,
        'regressions_vs_previous_accepted_rows': len(regressions), 'improvements_vs_previous_accepted_rows': len(improvements),
        'operations': operations, 'entry_sources': source_counts, 'statuses': statuses,
        'stage_seconds': {stage: round(sum(e['seconds'] for e in events if e['stage'] == stage), 3)
                          for stage in ('prepare_finish', 'enrich_finish', 'grobid_finish', 'llm_call_finish')},
        'cohorts': {c: {'papers': sum(p['cohort'] == c for p in papers),
            'count_exact': sum(p['cohort'] == c and p['count_match'] for p in papers),
            'field_errors': sum(p['cohort'] == c for p in mismatches),
            'regressions': sum(p['cohort'] == c for p in regressions),
            'improvements': sum(p['cohort'] == c for p in improvements)} for c in COHORTS},
        'mismatches': mismatches, 'regressions': regressions, 'improvements': improvements, 'paper_results': papers,
        'external_api_calls': 0, 'sql_writes': 0}
    bulk.save(out / 'backtest-report.json', output)
    print(json.dumps({k: v for k, v in output.items() if k not in ('mismatches', 'regressions', 'improvements', 'paper_results')}, ensure_ascii=True))
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--stage', choices=('setup', 'prepare', 'enrich', 'report'), required=True)
    args = parser.parse_args()
    globals()[args.stage](args.out.resolve())
