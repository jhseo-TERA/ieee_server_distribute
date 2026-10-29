"""Write live progress, then independent final checks for the local PDF batch.

This is a one-shot batch companion, not a scheduled/recurring task. It neither
starts inference nor modifies SQL. A terminal stage failure is reported as such.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark_reference_bulk import DEFAULT_OUT, read, save, now
from scripts.audit_reference_bulk import audit
from paper_server.parsers.references import key


def alive(pid):
    # Query process existence only; do not signal or change a worker.
    import ctypes
    from ctypes import wintypes
    library = ctypes.WinDLL('kernel32', use_last_error=True)
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    library.OpenProcess.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = library.OpenProcess(0x1000, False, int(pid))
    if handle:
        library.CloseHandle(handle)
        return True
    # A restricted status reader cannot conclude that an inaccessible process
    # has exited. The elevated batch companion can query its own workers.
    return None if ctypes.get_last_error() == 5 else False


def progress(out):
    manifest = read(out / 'manifest.json')
    settings = read(out / 'llm-settings.json')
    signature = settings['signature']
    prepared, results = [], []
    for paper in manifest['papers']:
        directory = out / 'papers' / paper['id']
        if (directory / 'prepared.json').exists():
            prepared.append(read(directory / 'prepared.json'))
        if (directory / 'result.json').exists():
            result = read(directory / 'result.json')
            if result.get('signature') == signature:
                results.append(result)
    stages = {}
    for stage in ('prepare', 'enrich'):
        path = out / (stage + '-status.json')
        state = read(path) if path.exists() else {'status': 'not_started'}
        if state['status'] == 'running':
            state['process_alive'] = alive(state['pid'])
        stages[stage] = state
    done = len(results) == len(manifest['papers']) and all(s['status'] == 'complete' for s in stages.values())
    failed = any(s['status'] == 'running' and s.get('process_alive') is False for s in stages.values())
    if (stages['enrich']['status'] == 'waiting_for_prepare' and stages['prepare']['status'] == 'complete'
            and len(results) < len(manifest['papers'])):
        failed = True
    status = 'complete' if done else 'needs_attention' if failed else 'running'
    total = len(manifest['papers'])
    result = {'at': now(), 'status': status, 'selected': total, 'prepared': len(prepared),
        'processed': len(results), 'prepare_statuses': dict(Counter(x['status'] for x in prepared)),
        'result_statuses': dict(Counter(x['status'] for x in results)),
        'references': sum(len(x['rows']) for x in results), 'routed_to_llm': sum(x['routed'] for x in results),
        'llm_completed': sum(x['llm_completed'] for x in results),
        'llm_success_seconds': round(sum(t['elapsed_seconds'] for x in results for t in x['llm_timings']), 3),
        'model_settings': signature, 'stages': stages}
    first = stages['prepare'].get('started_at')
    # prepare's terminal status retains elapsed seconds, but not necessarily start.
    if first:
        result['elapsed_seconds'] = round((datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds(), 1)
    save(out / 'progress.json', result)
    lines = [f"PDF 500개 1차 시험 — {status}", f"갱신: {result['at']}",
        f"선정: {total}; PDF 추출: {len(prepared)}/{total}; 필드 처리: {len(results)}/{total}",
        f"현재 처리된 참고문헌: {result['references']}",
        f"LLM 검토 대상: {result['routed_to_llm']}; LLM 검토 응답 완료: {result['llm_completed']}",
        f"PDF 추출 상태: {json.dumps(result['prepare_statuses'], ensure_ascii=False)}",
        f"필드 처리 상태: {json.dumps(result['result_statuses'], ensure_ascii=False)}",
        f"모델: {signature['model']}; 컨텍스트 8192; 출력 최대 2048; temperature 0.1; thinking 꺼짐",
        'Ollama 127.0.0.1:11434, 로컬 CPU 실행. 원문 일치 검증, 외부 API 호출/SQL 쓰기 없음.',
        'complete는 처리 절차 종료를 뜻하며, 모든 참고문헌의 의미 정확도를 보증하지 않습니다.',
        'v2는 대괄호·마침표 번호와 한 줄 인용을 지원합니다. 확인 불가능한 목록은 extraction_unresolved로 분리합니다(이전 no_references).',
        '완료 시 summary.json, quality-review.json, final-verification.json을 확인하세요.']
    # The status text is a convenience; the JSON above is atomically replaced.
    (out / 'RUN_STATUS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def holdout_score(out):
    path = out / 'holdout-audit-gold.json'
    if not path.exists():
        return None
    gold = read(path)
    manifest = {p['pdf']: p for p in read(out / 'manifest.json')['papers']}
    result = {'scope': gold['scope'], 'at': now(), 'pending_papers': [], 'scored_papers': 0,
        'count_exact_papers': 0, 'expected_reference_count': 0, 'extracted_reference_count': 0,
        'pipelines': {name: {'checked_fields': 0, 'correct_fields': 0, 'fields': {}, 'mismatches': []}
                      for name in ('grobid', 'before_llm', 'rows')},
        'note': 'Small IEEE spot-check. Missing reference counts all scored fields incorrect; not a 500-PDF accuracy estimate.'}
    for paper in gold['papers']:
        target = out / 'papers' / manifest[paper['pdf']]['id'] / 'result.json'
        if not target.exists():
            result['pending_papers'].append(paper['pdf'])
            continue
        value = read(target)
        result['scored_papers'] += 1
        result['expected_reference_count'] += paper['expected_count']
        result['extracted_reference_count'] += len(value['rows'])
        result['count_exact_papers'] += len(value['rows']) == paper['expected_count']
        for name, score in result['pipelines'].items():
            rows = {r['number']: r for r in value.get(name, [])}
            for expected in paper['gold']:
                for field in ('title', 'author', 'year', 'doi'):
                    if field not in expected:
                        continue
                    row = rows.get(expected['number'], {})
                    actual = row.get('title_display', row.get('title')) if name == 'rows' and field == 'title' else row.get(field)
                    correct = expected['number'] in rows and key(actual) == key(expected[field])
                    score['checked_fields'] += 1
                    score['correct_fields'] += int(correct)
                    per_field = score['fields'].setdefault(field, {'correct': 0, 'total': 0})
                    per_field['total'] += 1
                    per_field['correct'] += int(correct)
                    if not correct:
                        score['mismatches'].append({'pdf': paper['pdf'], 'number': expected['number'],
                            'field': field, 'expected': expected[field], 'actual': actual})
    save(out / 'holdout-score.json', result)
    return result


def final_checks(out):
    quality = audit(out, verify_pdfs=True)
    gold_path = out / 'manual-audit-gold.json'
    manual = {'note': 'Targeted diagnostic regression sample; not a random accuracy estimate.',
              'checked_fields': 0, 'correct_fields': 0, 'mismatches': []}
    if gold_path.exists():
        gold = read(gold_path)
        manifest = read(out / 'manifest.json')
        paper = next(p for p in manifest['papers'] if p['pdf'] == gold['pdf'])
        rows = {r['number']: r for r in read(out / 'papers' / paper['id'] / 'result.json')['rows']}
        for expected in gold['references']:
            for field in ('title', 'author', 'year', 'doi'):
                manual['checked_fields'] += 1
                row = rows.get(expected['number'], {})
                actual = row.get('title_display', row.get('title')) if field == 'title' else row.get(field)
                if expected['number'] in rows and key(actual) == key(expected[field]):
                    manual['correct_fields'] += 1
                else:
                    manual['mismatches'].append({'number': expected['number'], 'field': field,
                                                  'actual': actual, 'expected': expected[field]})
    progress_value = read(out / 'progress.json')
    final = {'finished_at': now(), 'status': 'complete', 'progress': progress_value,
        'structural_issue_count': len(quality['structural_issues']), 'manual_regression': manual,
        'validation_status': 'needs_review' if quality['structural_issues'] or manual['mismatches'] else 'passed_structural_checks',
        'source_checks': quality['counts'], 'preflight_checks': read(out / 'preflight-checks.json'),
        'holdout_spotcheck': holdout_score(out),
        'limitations': ['No gold-standard accuracy score for 500 PDFs.',
            'Non-supported reference formats are reported separately, not assumed citation-free.',
            'All outputs are experimental candidates; no live citation edges or SQL records were created.']}
    save(out / 'final-verification.json', final)
    print(json.dumps({'final_verification': str(out / 'final-verification.json'),
        'processed': progress_value['processed'], 'references': progress_value['references'],
        'structural_issue_count': final['structural_issue_count']}, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    out = args.out.resolve()
    while True:
        state = progress(out)
        holdout_score(out)
        print(json.dumps({k: state[k] for k in ('at', 'status', 'prepared', 'processed', 'references')}, ensure_ascii=True), flush=True)
        if state['status'] == 'complete':
            final_checks(out)
            break
        if state['status'] == 'needs_attention' or args.once:
            break
        time.sleep(30)


if __name__ == '__main__':
    main()
