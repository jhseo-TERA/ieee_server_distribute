"""Verify MySQL feedback in a connection-local temporary table only."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
import web.app as app
from recommendation_feedback import FeedbackStore


class TemporaryEngine:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connect(self):
        yield self.conn

    @contextmanager
    def begin(self):
        with self.conn.begin_nested():
            yield self.conn


def main():
    with app.engine.connect() as conn:
        ddl = (ROOT / 'scripts/migrations/011_recommendation_feedback.sql').read_text(encoding='utf-8')
        ddl = ddl.replace('CREATE TABLE IF NOT EXISTS', 'CREATE TEMPORARY TABLE')
        conn.execute(text(ddl))
        try:
            adapter = TemporaryEngine(conn)
            store = FeedbackStore(adapter)
            with patch.object(app, 'engine', adapter):
                baseline = app.build_sql_recommendations(20)
                article = str(baseline['items'][0]['article_number'])
                before = conn.execute(text('SELECT is_favorite FROM papers WHERE article_number=:a'), {'a': article}).scalar()
                store.save(article, 'dismissed')
                store.save(article, 'dismissed')
                assert len(store.read()['actions']) == 1
                excluded = app.build_sql_recommendations(20)
                assert article not in {str(p['article_number']) for p in excluded['items']}
                store.save(article, 'reviewed')
                assert store.read()['actions'][article]['action'] == 'reviewed'
                store.save(article, 'restore')
                restored = app.build_sql_recommendations(20)
                assert article in {str(p['article_number']) for p in restored['items']}
                after = conn.execute(text('SELECT is_favorite FROM papers WHERE article_number=:a'), {'a': article}).scalar()
                assert before == after
                print(json.dumps({'temporary_table_only': True, 'exclude_and_restore': True,
                                  'favorites_unchanged': True, 'candidate_count': len(restored['items'])}))
        finally:
            conn.execute(text('DROP TEMPORARY TABLE recommendation_feedback'))
    app.engine.dispose()


if __name__ == '__main__':
    main()
