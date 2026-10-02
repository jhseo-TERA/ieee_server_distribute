"""Opt-in Survey check against the fixed loopback server. Never approves data.

Default: inspect server-calculated snapshots and queue candidates without inference.
--live: enqueue two real model jobs; preserve job history and original records.
Authentication is locally signed for this smoke test, kept in memory and sent only
to 127.0.0.1. No credentials or cookies are printed or written to disk.
"""
import argparse
import json
from pathlib import Path
import secrets
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
from sqlalchemy import text
from web.app import app, engine, ADMIN_USERNAME, build_serdes_performance
from local_ai_repository import LocalAIRepository
from local_ai_survey import SurveyAIService, validate_filters


def counts():
    with engine.connect() as conn:
        return dict(conn.execute(text('SELECT COUNT(*) papers,SUM(is_favorite=1) favorites,'
                    'SUM(pdf_available=1) pdfs,(SELECT COUNT(*) FROM serdes_measurements) measurements FROM papers')).mappings().one())


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    repo = LocalAIRepository(engine)
    worker = SimpleNamespace(repo=repo, enqueue=lambda req, owner: {'request': req})
    survey = SurveyAIService(worker, build_serdes_performance)
    before = counts()
    analysis = {'kind': 'analyze', 'question': '집계와 대표 근거를 바탕으로 연구 경향을 세 문장, 비교 한계를 두 문장으로 요약해 주세요.'}
    snapshot = survey.submit(analysis, ADMIN_USERNAME, 'admin')['request']['survey_snapshot']
    emit({'preflight': True, 'counts': before, 'summary': snapshot['summary'], 'sample_count': snapshot['sample_count']})
    candidates = repo.get_papers([s['article_number'] for s in snapshot['sources'] if s['article_number']])
    keys = list(dict.fromkeys(p['article_number'] for p in candidates if p.get('pdf_available')))[:2]
    if len(keys) < 2:
        keys = list(dict.fromkeys(p['article_number'] for p in candidates))[:2]
    compare = {'kind': 'compare', 'article_numbers': keys, 'question': '두 논문의 저장된 성능을 세 문장으로 비교하고 직접 비교가 어려운 조건을 두 가지 명시하세요.'}
    comparison = survey.submit(compare, ADMIN_USERNAME, 'admin')['request']['survey_snapshot']
    gaps = survey.gaps()
    emit({'comparison_papers': keys, 'matching_points': [len(p['matching_points']) for p in comparison['comparison']],
          'gap_candidates': gaps['candidate_count'], 'gap_returned': gaps['returned_count'],
          'gap_favorites': sum(bool(p['is_favorite']) for p in gaps['items']),
          'gap_pdfs': sum(bool(p['pdf_available']) for p in gaps['items'])})
    if not args.live:
        assert counts() == before
        return 0
    client = requests.Session()
    token = secrets.token_urlsafe(32)
    cookie = app.session_interface.get_signing_serializer(app).dumps(
        {'authenticated': True, 'username': ADMIN_USERNAME, 'role': 'admin', 'csrf_token': token})
    client.cookies.set(app.config['SESSION_COOKIE_NAME'], cookie, domain='127.0.0.1', path='/')
    client.headers['X-CSRF-Token'] = token

    def request(method, path, **kwargs):
        response = client.request(method, 'http://127.0.0.1:5001' + path, timeout=60, allow_redirects=False, **kwargs)
        if response.status_code not in (200, 202):
            raise RuntimeError(f'Local endpoint {path}: HTTP {response.status_code}')
        return response.json()

    started = time.monotonic()
    jobs = [request('POST', '/api/ai/survey/jobs', json=payload)['job'] for payload in (analysis, compare)]
    pending = {j['id'] for j in jobs}
    emit({'jobs': [{'id': j['id'], 'kind': j['kind']} for j in jobs]})
    last_print, failed = 0, False
    while pending and time.monotonic() - started < 1500:
        for job_id in list(pending):
            job = request('GET', '/api/ai/jobs/' + job_id)['job']
            if job['status'] in {'complete', 'failed', 'cancelled'}:
                result = job.get('result') or {}
                emit({'job_id': job_id, 'kind': job['kind'], 'status': job['status'], 'error': job.get('error'),
                      'seconds': int(time.monotonic()-started), 'answer': result.get('answer'),
                      'citations': result.get('citations'), 'validation': result.get('validation'), 'usage': result.get('usage')})
                pending.remove(job_id)
                if job['status'] != 'complete':
                    failed = True
                    continue
                assert result['summary'] == job['request']['survey_snapshot']['summary']
                assert result['grounding'] == 'needs_review' and result['citations']
        if time.monotonic()-last_print > 25:
            emit({'elapsed': int(time.monotonic()-started), 'pending': len(pending)})
            last_print = time.monotonic()
        if pending:
            time.sleep(2)
    if pending:
        for job_id in pending:
            request('POST', '/api/ai/jobs/' + job_id + '/cancel', json={})
        return 1
    assert counts() == before, 'Original data counts changed'
    emit({'source_counts_unchanged': True, 'counts': counts()})
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
