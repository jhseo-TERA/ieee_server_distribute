"""Apply only the additive recommendation feedback schema for this deployment."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from web.app import engine


def snapshot(conn):
    counts = dict(conn.execute(text('SELECT COUNT(*) papers,SUM(is_favorite=1) favorites FROM papers')).mappings().one())
    favorites = list(conn.execute(text('SELECT article_number FROM papers WHERE is_favorite=1 ORDER BY article_number')).scalars())
    return {**{k:int(v or 0) for k,v in counts.items()},
            'favorites_sha256':hashlib.sha256(json.dumps(favorites).encode()).hexdigest()}


def main():
    with engine.connect() as conn:
        active = conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('running','queued')")).scalar()
        if active:
            raise RuntimeError('Active AI jobs must finish before restart.')
        before = snapshot(conn)
    migration = ROOT/'scripts/migrations/011_recommendation_feedback.sql'
    with engine.begin() as conn:
        conn.execute(text(migration.read_text(encoding='utf-8')))
        count = conn.execute(text('SELECT COUNT(*) FROM recommendation_feedback')).scalar()
        after = snapshot(conn)
    assert before == after, 'Source state changed during migration; inspect before restarting.'
    result = {'migration':migration.name, 'source_before':before, 'source_after':after,
              'source_unchanged':True, 'feedback_rows':count}
    output = ROOT/'outputs/recommendation_quality/deployment_migration.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
