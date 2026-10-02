"""Opt-in local inference smoke. Stages proposals; never approves measurements."""
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web.app import app, ADMIN_USERNAME, engine
from sqlalchemy import text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', choices=['ask', 'review', 'extract'], required=True)
    parser.add_argument('--vision', action='store_true')
    parser.add_argument('--paper', default='10003645')
    parser.add_argument('--auto', action='store_true', help='Exercise question-to-search planning (ask only).')
    args = parser.parse_args()
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(authenticated=True, username=ADMIN_USERNAME, role='admin', csrf_token='local-smoke')
    for path in ['/ai', '/api/ai/status', '/api/ai/search?q=receiver', '/api/ai/papers/' + args.paper]:
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f'{path}: HTTP {response.status_code}: {response.get_json()}')
    payload = dict(kind=args.kind, article_numbers=[args.paper], num_ctx=16384,
                   max_tokens=2048, thinking='off' if args.kind == 'extract' else 'low',
                   question='핵심 성능과 전력 범위를 근거와 함께 세 문장으로 요약하고 확인이 필요한 항목 두 개를 제시하세요.')
    if args.auto:
        if args.kind != 'ask':
            parser.error('--auto requires --kind ask')
        payload.update(article_numbers=[], question='time-windowed LSB 디코더를 사용한 PAM4 수신기 논문을 찾아 에너지 효율을 근거와 함께 요약해줘.')
    if args.kind == 'extract':
        payload.update(pages=[1], vision=args.vision,
                       question='첫 페이지의 초록에서 공정 nm, 데이터 전송률, 에너지 효율 중 명확한 수치 최대 3개만 추출하세요. 원문 문장을 그대로 인용하세요.')
    with engine.connect() as conn:
        before = {name: conn.execute(text('SELECT COUNT(*) FROM ' + name)).scalar_one()
                  for name in ['papers', 'serdes_measurements']}
    response = client.post('/api/ai/jobs', json=payload, headers={'X-CSRF-Token': 'local-smoke'})
    if response.status_code != 202:
        raise RuntimeError(str(response.get_json()))
    job_id = response.get_json()['job']['id']
    print(json.dumps({'job_id': job_id, 'kind': args.kind, 'vision': args.vision}), flush=True)
    started, last_print = time.monotonic(), 0
    while time.monotonic() - started < 1000:
        job = client.get('/api/ai/jobs/' + job_id).get_json()['job']
        elapsed = int(time.monotonic() - started)
        if elapsed - last_print >= 15:
            print(json.dumps({'seconds': elapsed, 'status': job['status'], 'progress': job['progress']}, ensure_ascii=False), flush=True)
            last_print = elapsed
        if job['status'] in {'complete', 'failed', 'cancelled'}:
            result = job.get('result') or {}
            summary = {k: result.get(k) for k in ['answer', 'grounding', 'citations', 'findings', 'validation_counts', 'limitations', 'usage']}
            summary.update(job_id=job_id, status=job['status'], error=job.get('error'), seconds=elapsed,
                           proposals=[{k: p.get(k) for k in ['id', 'field', 'value', 'unit', 'validation_status', 'validation_reasons']} for p in result.get('proposals', [])])
            with engine.connect() as conn:
                after = {name: conn.execute(text('SELECT COUNT(*) FROM ' + name)).scalar_one() for name in before}
            summary['original_counts_unchanged'] = before == after
            print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)
            if job['status'] != 'complete' or before != after:
                return 1
            return 0
        time.sleep(1)
    client.post('/api/ai/jobs/' + job_id + '/cancel', headers={'X-CSRF-Token': 'local-smoke'})
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
