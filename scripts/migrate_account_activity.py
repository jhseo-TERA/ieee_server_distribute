"""Create the additive login activity table before restarting the server."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import inspect, text
from web.app import engine


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    before = inspect(engine).has_table('account_login_sessions')
    if args.apply:
        ddl = (ROOT / 'scripts/migrations/016_account_activity.sql').read_text(encoding='utf-8')
        with engine.begin() as conn:
            conn.execute(text(ddl))
    print(json.dumps(dict(apply=args.apply, table_before=before,
                         table_after=inspect(engine).has_table('account_login_sessions'))))
