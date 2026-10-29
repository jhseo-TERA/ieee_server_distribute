"""Add private daily reading tables; existing paper/favorite data is untouched."""
import argparse
import json
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import URL, create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def migrate(engine, apply=False):
    names = ('daily_review_batches', 'daily_review_picks', 'personal_paper_reviews')
    before = {name: inspect(engine).has_table(name) for name in names}
    if apply:
        ddl = (ROOT / 'scripts/migrations/015_daily_review.sql').read_text(encoding='utf-8')
        with engine.begin() as conn:
            for statement in ddl.split(';'):
                if statement.strip():
                    conn.execute(text(statement))
    return dict(apply=apply, tables_before=before,
                tables_after={name: inspect(engine).has_table(name) for name in names})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    values = dotenv_values(ROOT / '.env')
    engine = create_engine(URL.create('mysql+pymysql', username=values['DB_USER'],
        password=values['DB_PASSWORD'], host=values.get('DB_HOST', '127.0.0.1'),
        port=int(values.get('DB_PORT', 3306)), database=values['DB_NAME']), pool_pre_ping=True)
    try:
        print(json.dumps(migrate(engine, args.apply)))
    finally:
        engine.dispose()
