"""Repeatable local-only PDF reference benchmark. Results never enter the live DB.

Run --prepare once, then --models MODEL ... to compare optional local LLMs.
Source-file hashes, raw outputs, grounding rejections and timings are retained.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from paper_server.ai.runtime import OllamaClient
from paper_server.citation_fallback import extract_references, reference_entry
from paper_server.parsers.references import (
    FIELDS, VERSION, consensus, digest_file, grounded, grobid_raw, key,
    llm_fields, mineru_references, pdf_pages, split_numbered,
)
from paper_server.serdes.mineru import mineru_environment

OUT = ROOT / 'outputs/reference_benchmark'
CONFIG = ROOT / 'config/reference_benchmark.json'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')


def heuristic(entries):
    return [grounded(entry, reference_entry(entry['raw']), 'regex') for entry in entries]


def mineru_path(paper, source_hash):
    experiment = OUT / 'mineru' / paper['id'] / 'structured_content.json'
    provenance = experiment.parent / 'benchmark-source.json'
    expected = {'sha256': source_hash, 'start': paper['start'], 'pages': paper['pages'], 'version': '4.0.10'}
    if experiment.exists() and provenance.exists():
        if json.loads(provenance.read_text(encoding='utf-8')) == expected:
            return experiment, True
    existing = list((ROOT / 'outputs/serdes_mineru' / source_hash).glob('*/parsed/structured_content.json'))
    if existing:
        return existing[0], True
    return experiment, False


def prepare(paper):
    target = OUT / 'papers' / paper['id']
    target.mkdir(parents=True, exist_ok=True)
    path = ROOT / paper['pdf']
    source_hash = digest_file(path)
    result = {'source_sha256': source_hash, 'experiment_version': VERSION, 'pipelines': {}, 'timings': {}}
    started = time.monotonic()
    original = extract_references(path)
    result['timings']['pypdf'] = round(time.monotonic() - started, 3)
    result['pipelines']['pypdf_regex'] = heuristic(original)
    robust = split_numbered(pdf_pages(path))
    result['pipelines']['pypdf_grobid'], seconds = grobid_raw(robust, target / 'grobid-pypdf') if robust else ([], 0)
    result['timings']['pypdf_grobid'] = seconds + result['timings']['pypdf']
    parsed_path, cached = mineru_path(paper, source_hash)
    started = time.monotonic()
    if not cached:
        python = ROOT / '.runtime/document-parsers/Scripts/python.exe'
        env = mineru_environment()
        env.update({'OMP_NUM_THREADS': '4', 'MKL_NUM_THREADS': '4'})
        with (target / 'mineru.log').open('w', encoding='utf-8') as log:
            subprocess.run([str(python), str(ROOT / 'scripts/mineru_worker.py'), '--pdf', str(path),
                '--output', str(parsed_path.parent), '--start-page', str(paper['start']),
                '--max-pages', str(paper['pages'])], stdout=log, stderr=log, env=env, cwd=ROOT, timeout=600, check=True)
        save(parsed_path.parent / 'benchmark-source.json', {'sha256': source_hash, 'start': paper['start'],
                                                           'pages': paper['pages'], 'version': '4.0.10'})
    mineru_seconds = round(time.monotonic() - started, 3) if not cached else None
    payload = json.loads(parsed_path.read_text(encoding='utf-8'))
    result['pipelines']['mineru_native_regex'] = heuristic(mineru_references(payload))
    raw = mineru_references(payload, recover_adjacent=True)
    result['timings']['mineru'] = {'seconds': mineru_seconds, 'cache_hit': cached, 'source': str(parsed_path.relative_to(ROOT))}
    result['pipelines']['mineru_regex'] = heuristic(raw)
    if raw:
        result['pipelines']['mineru_grobid'], seconds = grobid_raw(raw, target / 'grobid-mineru')
    else:
        result['pipelines']['mineru_grobid'], seconds = [], 0
    result['timings']['grobid_on_mineru'] = seconds
    result['pipelines']['mineru_consensus'] = [consensus(entry, regex, grobid) for entry, regex, grobid in
        zip(raw, result['pipelines']['mineru_regex'], result['pipelines']['mineru_grobid'])]
    result['mineru_entries'] = raw
    save(target / 'result.json', result)
    print(json.dumps({'prepared': paper['id'], 'counts': {k: len(v) for k, v in result['pipelines'].items()},
                      'mineru_seconds': mineru_seconds}), flush=True)


def evaluate(papers):
    records = [(paper, json.loads((OUT / 'papers' / paper['id'] / 'result.json').read_text(encoding='utf-8')))
               for paper in papers]
    names = sorted(set(name for _, result in records for name in result['pipelines']))
    summary = {'experiment_version': VERSION, 'papers': len(papers),
               'expected_references': sum(paper['expected_count'] for paper in papers),
               'gold_references': sum(len(paper['gold']) for paper in papers), 'pipelines': {}}
    for name in names:
        stats = {'reference_items': 0, 'count_exact_papers': 0, 'fields': {field: {'correct': 0, 'total': 0} for field in FIELDS},
                 'grounding_rejections': 0, 'review_flags': 0, 'failures': [], 'paper_counts': {}, 'pending_papers': []}
        for paper, result in records:
            if name not in result['pipelines']:
                stats['pending_papers'].append(paper['id'])
                continue
            rows = result['pipelines'].get(name, [])
            stats['reference_items'] += len(rows)
            stats['paper_counts'][paper['id']] = len(rows)
            stats['count_exact_papers'] += len(rows) == paper['expected_count']
            by_number = {row['number']: row for row in rows}
            for gold in paper['gold']:
                row = by_number.get(gold['number'], {})
                stats['grounding_rejections'] += sum(w.endswith('_not_in_source') for w in row.get('warnings', []))
                stats['review_flags'] += len(row.get('warnings', []))
                # DOI absence is tested for every gold item in this source-only fixture.
                for field, expected in {'doi': None, **gold}.items():
                    if field == 'number':
                        continue
                    stats['fields'][field]['total'] += 1
                    actual = row.get(field)
                    correct = bool(row) and ((actual is None) if expected is None else key(actual) == key(expected))
                    stats['fields'][field]['correct'] += correct
                    if not correct:
                        stats['failures'].append({'paper': paper['id'], 'number': gold['number'],
                                                  'field': field, 'expected': expected, 'actual': actual})
        summary['pipelines'][name] = stats
    save(OUT / 'summary.json', summary)
    print(json.dumps({'summary': str(OUT / 'summary.json'), 'papers': summary['papers'],
        'gold_references': summary['gold_references'], 'pipelines': {
            name: {'items': item['reference_items'], 'fields': item['fields']}
            for name, item in summary['pipelines'].items()}}, ensure_ascii=True), flush=True)
    return summary


def model_cache_signature(client, model):
    status = client.status()
    digest = next((row.get('digest') for row in status['models'] if row['name'] == model), None)
    if not digest:
        raise ValueError('The requested local model is not installed')
    prompt_hash = hashlib.sha256((inspect.getsource(llm_fields) + inspect.getsource(llm_fields.__globals__['llm_schema'])).encode()).hexdigest()
    return {'model_digest': digest, 'prompt_hash': prompt_hash}


def with_llm(papers, model):
    client = OllamaClient(deadline_seconds=240)
    signature = model_cache_signature(client, model)
    slug = model.replace(':', '_')
    for paper in papers:
        target = OUT / 'papers' / paper['id'] / 'result.json'
        result = json.loads(target.read_text(encoding='utf-8'))
        if result['source_sha256'] != digest_file(ROOT / paper['pdf']):
            raise ValueError('Source PDF changed; rerun --prepare')
        selected = [entry for entry in result['mineru_entries'] if entry['number'] in {g['number'] for g in paper['gold']}]
        cache = OUT / 'llm' / slug / (paper['id'] + '.json')
        input_text = json.dumps(selected, sort_keys=True)
        cached = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else None
        if (cached and cached.get('input') == input_text and cached.get('version') == VERSION
                and all(cached.get(k) == v for k, v in signature.items())):
            rows, timing = cached['rows'], cached['timing']
        else:
            rows, timing = llm_fields(client, model, selected) if selected else ([], {'elapsed_seconds': 0})
            save(cache, {'version': VERSION, 'input': input_text, 'rows': rows, 'timing': timing, 'model': model, **signature})
        completed = deepcopy(result['pipelines']['mineru_regex'])
        by_number = {row['number']: row for row in rows}
        for i, row in enumerate(completed):
            if row['number'] in by_number:
                completed[i] = by_number[row['number']]
        result['pipelines']['mineru_llm:' + model] = completed
        result['pipelines']['mineru_consensus'] = [consensus(entry, regex, grobid)
            for entry, regex, grobid in zip(result['mineru_entries'], result['pipelines']['mineru_regex'],
                                           result['pipelines']['mineru_grobid'])]
        result['pipelines']['mineru_hybrid:' + model] = [consensus(entry, regex, grobid, by_number.get(entry['number']))
            for entry, regex, grobid in zip(result['mineru_entries'], result['pipelines']['mineru_regex'],
                                           result['pipelines']['mineru_grobid'])]
        result['timings']['llm:' + model] = timing
        save(target, result)
        print(json.dumps({'model': model, 'paper': paper['id'], 'seconds': timing.get('elapsed_seconds'),
                          'rejections': sum(len(row['warnings']) for row in rows)}), flush=True)


def adaptive(papers, model):
    """Measure the proposed router on ALL extracted items, not only scored gold."""
    client = OllamaClient(deadline_seconds=240)
    model_signature = model_cache_signature(client, model)
    for paper in papers:
        path = OUT / 'papers' / paper['id'] / 'result.json'
        result = json.loads(path.read_text(encoding='utf-8'))
        bases = [consensus(entry, regex, grobid) for entry, regex, grobid in
                 zip(result['mineru_entries'], result['pipelines']['mineru_regex'], result['pipelines']['mineru_grobid'])]
        selected = [row for row in bases if row['needs_llm']]
        answers, timings, errors = {}, [], []
        for start in range(0, len(selected), 5):
            batch = selected[start:start + 5]
            cache = OUT / 'adaptive' / model.replace(':', '_') / f'{paper["id"]}-{start}.json'
            signature = json.dumps(batch, sort_keys=True)
            previous = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else None
            try:
                if (previous and previous.get('input') == signature and previous.get('version') == VERSION
                        and all(previous.get(k) == v for k, v in model_signature.items())):
                    rows, timing = previous['rows'], previous['timing']
                else:
                    rows, timing = llm_fields(client, model, batch)
                    save(cache, {'version': VERSION, 'input': signature, 'rows': rows, 'timing': timing, **model_signature})
                answers.update({row['number']: row for row in rows})
                timings.append(timing)
            except (ValueError, RuntimeError) as exc:
                errors.append({'start': start, 'error': type(exc).__name__})
        result['pipelines']['mineru_adaptive:' + model] = [consensus(entry, regex, grobid, answers.get(entry['number']))
            for entry, regex, grobid in zip(result['mineru_entries'], result['pipelines']['mineru_regex'],
                                           result['pipelines']['mineru_grobid'])]
        result['timings']['adaptive:' + model] = {'routed_items': len(selected), 'total_items': len(bases),
            'elapsed_seconds': round(sum(item['elapsed_seconds'] for item in timings), 3),
            'batches': len(timings), 'errors': errors}
        save(path, result)
        print(json.dumps({'adaptive': model, 'paper': paper['id'], 'routed': len(selected),
                          'seconds': result['timings']['adaptive:' + model]['elapsed_seconds'], 'errors': errors}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--models', nargs='*', default=[])
    parser.add_argument('--adaptive-model')
    parser.add_argument('--papers', nargs='*')
    args = parser.parse_args()
    papers = json.loads(CONFIG.read_text(encoding='utf-8'))['papers']
    if args.papers:
        papers = [paper for paper in papers if paper['id'] in args.papers]
    OUT.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        for paper in papers:
            saved = OUT / 'papers' / paper['id'] / 'result.json'
            if args.force or not saved.exists():
                prepare(paper)
    for model in args.models:
        with_llm(papers, model)
    if args.adaptive_model:
        adaptive(papers, args.adaptive_model)
    evaluate(papers)


if __name__ == '__main__':
    main()
