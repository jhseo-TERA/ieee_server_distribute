"""Prepare original block-diagram thumbnails without editing PDFs or SQL rows."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import bindparam, text
from web.app import engine, build_serdes_performance, ROOT
from serdes_figures import FigureStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--article', action='append')
    parser.add_argument('--retry', action='store_true')
    args = parser.parse_args()
    ids = sorted({p['paper_id'] for p in build_serdes_performance({'evidence_tier':'all'})['points'] if p.get('paper_id')})
    if not ids:
        raise RuntimeError('No Survey papers found; check the database.')
    with engine.connect() as conn:
        papers = [dict(r) for r in conn.execute(text(
            'SELECT id,article_number,title,source_system,pdf_local_path,pdf_available,year FROM papers '
            'WHERE id IN :ids ORDER BY CAST(year AS UNSIGNED) DESC,id DESC').bindparams(
                bindparam('ids', expanding=True)), {'ids': ids}).mappings()]
    if args.article:
        papers = [p for p in papers if p['article_number'] in args.article]
    if args.limit:
        papers = papers[:args.limit]
    store, counts, started = FigureStore(ROOT), Counter(), time.monotonic()
    for index, paper in enumerate(papers, 1):
        try:
            result = store.prepare(paper, retry=args.retry)
            counts[result['status']] += 1
            detail = {k: result[k] for k in ('page','figure_number','caption','image') if k in result}
        except Exception as exc:
            counts['error'] += 1
            detail = {'error': type(exc).__name__, 'message': str(exc)[:180]}
        if len(papers) <= 30 or index % 25 == 0 or 'error' in detail:
            print(json.dumps({'done': index, 'total': len(papers), 'article': paper['article_number'],
                              'counts': counts, **detail}, ensure_ascii=False), flush=True)
    print(json.dumps({'complete': True, 'counts': counts, 'seconds': round(time.monotonic()-started,1)}), flush=True)


if __name__ == '__main__':
    main()
