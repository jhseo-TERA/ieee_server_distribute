"""Initialize the configured vault and backfill admin's saved reviews."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from paper_server.obsidian_reviews import ObsidianReviewSync, REVIEW_WRITER
from web.app import app, engine


if __name__ == '__main__':
    vault = app.config.get('OBSIDIAN_VAULT_PATH')
    if not vault:
        raise SystemExit('Set OBSIDIAN_VAULT_PATH to the existing Obsidian vault first.')
    with engine.connect() as conn:
        account = conn.execute(text('SELECT id FROM accounts WHERE username=:name AND is_active=1'),
                               {'name': REVIEW_WRITER}).scalar_one()
    print(json.dumps(ObsidianReviewSync(engine, vault).sync_all(f'account:{account}'), ensure_ascii=False))
