"""Read-only survey figure inventory; writes a reproducible diagnostic report."""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import bindparam, text
from web.app import engine, build_serdes_performance, ROOT
from serdes_figures import FigureStore

def main():
    data = build_serdes_performance({'evidence_tier': 'structured'}, strict=True)
    rep = set(data['chart_sets']['representative'])
    ids = sorted({p['paper_id'] for p in data['points'] if p['performance']['measurement_id'] in rep})
    with engine.connect() as conn:
        papers = [dict(r) for r in conn.execute(text(
            'SELECT id,article_number,title,doi,url,source_system,pdf_local_path,pdf_available,year FROM papers WHERE id IN :ids'
        ).bindparams(bindparam('ids', expanding=True)), {'ids': ids}).mappings()]
        named = [dict(r) for r in conn.execute(text(
            "SELECT id,article_number,title,doi,url,source_system,pdf_local_path,pdf_available,year FROM papers WHERE title LIKE '%Low-power CMOS receivers for short reach optical communication%'"
        )).mappings()]
    store = FigureStore(ROOT)
    rows = [{**p, 'figure': store.lookup(p)} for p in papers]
    output = Path(ROOT) / 'outputs' / 'serdes_figure_audit'
    output.mkdir(parents=True, exist_ok=True)
    report = {'counts': dict(Counter(p['figure']['status'] for p in rows)), 'papers': rows,
              'named': [{**p, 'figure': store.lookup(p)} for p in named]}
    (output / 'inventory.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'counts': report['counts'], 'missing': [p for p in rows if p['figure']['status']=='missing_pdf'], 'named': report['named']}, ensure_ascii=False, default=str))

if __name__ == '__main__':
    main()
