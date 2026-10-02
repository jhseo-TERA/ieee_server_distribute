"""Measure recommendation reads and roll back all favorite API test writes.

No model jobs, remote requests or permanent database changes are made.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import text
from local_ai_recommendations import RecommendationService
from local_ai_repository import LocalAIRepository
import web.app as app_module


def emit(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def elapsed(call):
    start = time.perf_counter()
    result = call()
    return round((time.perf_counter() - start) * 1000, 2), result


def main():
    engine = app_module.engine
    with engine.connect() as conn:
        selected = [dict(r) for r in conn.execute(text(
            'SELECT article_number,is_favorite,citation_count,citation_source,citation_updated_at '
            'FROM papers WHERE is_favorite=0 ORDER BY id DESC LIMIT 240')).mappings()]
        jobs_before = conn.execute(text('SELECT COUNT(*) FROM ai_jobs')).scalar()
    assert len(selected) == 240
    articles = [r['article_number'] for r in selected]
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session.update(authenticated=True, username=app_module.ADMIN_USERNAME, role='admin', csrf_token='smoke-local')
    with engine.connect() as conn:
        transaction = conn.begin()
        @contextmanager
        def begin():
            yield conn  # Route commits are deliberately confined to this rollback-only transaction.
        try:
            with patch.object(app_module, 'engine', SimpleNamespace(begin=begin)):
                ms, response = elapsed(lambda: client.post('/api/favorites/bulk',
                    json={'article_numbers': articles}, headers={'X-CSRF-Token': 'smoke-local'}))
                assert response.status_code == 200 and response.json['changed_count'] == 240
                emit({'bulk_240_api_ms': ms, 'changed_count': response.json['changed_count'], 'rollback_only': True})
                samples = []
                for art in articles[:5]:
                    ms, response = elapsed(lambda: client.post('/api/favorite',
                        json={'article_number': art, 'favorite': False}, headers={'X-CSRF-Token': 'smoke-local'}))
                    assert response.status_code == 200 and response.json['favorite_delta'] == -1
                    samples.append(ms)
                emit({'single_api_ms_median': statistics.median(samples), 'samples_ms': samples, 'rollback_only': True})
        finally:
            transaction.rollback()
    with engine.connect() as conn:
        actual = [dict(r) for r in conn.execute(text(
            'SELECT article_number,is_favorite,citation_count,citation_source,citation_updated_at '
            'FROM papers WHERE is_favorite=0 ORDER BY id DESC LIMIT 240')).mappings()]
    assert actual == selected, 'Favorite/citation state did not survive rollback'

    service = RecommendationService(engine, None, app_module.build_sql_recommendations)
    try:
        started = time.perf_counter()
        ms, view = elapsed(service.view)
        emit({'cold_read_ms': ms, 'items': len(view['items']), 'refresh': view['refresh']})
        with service.sql_cache.condition:
            assert service.sql_cache.condition.wait_for(lambda: bool(service.sql_cache.entries or service.sql_cache.errors), timeout=45)
        assert service.sql_cache.entries, 'Background candidate SQL failed'
        emit({'background_ready_ms_including_debounce': round((time.perf_counter()-started)*1000, 2)})
        samples = []
        for _ in range(5):
            ms, view = elapsed(service.view)
            samples.append(ms)
        emit({'cached_read_ms_median': statistics.median(samples), 'samples_ms': samples, 'items': len(view['items'])})
        snap = service.snapshot()
        snap['signature'] = 'smoke-changed-signature'
        service.snapshot = lambda: snap
        ms, stale = elapsed(service.view)
        emit({'changed_signature_read_ms': ms, 'items': len(stale['items']), 'refresh': stale['refresh']})
    finally:
        service.close()

    worker = SimpleNamespace(repo=LocalAIRepository(engine))
    service = RecommendationService(engine, worker, app_module.build_sql_recommendations)
    service.schedule_refresh = lambda snap: False  # Read stored AI results without enqueuing inference.
    try:
        ms, view = elapsed(service.view)
        emit({'stored_ai_read_ms': ms, 'items': len(view['items']), 'engine': view['engine'], 'stale': view['ai']['stale']})
    finally:
        service.close()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM ai_jobs')).scalar() == jobs_before
    emit({'favorite_and_citation_rows_preserved': True, 'ai_jobs_unchanged': True})


if __name__ == '__main__':
    main()
