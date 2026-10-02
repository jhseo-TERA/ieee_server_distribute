"""Read-only representative-point / figure association audit."""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.append(str(ROOT/'.venv/Lib/site-packages'))
from sqlalchemy import text
import web.app as app_module
from serdes_figures import FigureStore


def main():
    out=ROOT/'outputs/serdes_figure_complete_20260915'
    store=FigureStore(ROOT)
    papers={p['article_number']:p for p in json.loads((ROOT/'outputs/serdes_figure_audit/inventory.json').read_text(encoding='utf-8'))['papers']}
    performance=app_module.build_serdes_performance({},strict=True)
    representative=set(performance['chart_sets']['representative'])
    with app_module.engine.connect() as conn:
        rows=conn.execute(text('SELECT m.*, i.canonical_paper_id, i.canonical_title, p.article_number '
                              'FROM serdes_measurements m JOIN serdes_implementations i ON i.id=m.implementation_id '
                              'JOIN papers p ON p.id=i.canonical_paper_id')).mappings().all()
        evidence=conn.execute(text('SELECT measurement_id,field_name,evidence_text FROM serdes_measurement_evidence')).mappings().all()
    by_id={r['id']:dict(r) for r in rows}
    by_evidence={}
    for e in evidence:by_evidence.setdefault(e['measurement_id'],[]).append(dict(e))
    checked=[]
    for point in performance['points']:
        a=str(point.get('article_number')); mid=point['performance'].get('measurement_id')
        if mid not in representative or a not in papers:continue
        measurement=by_id[mid];record=store.lookup(papers[a]);public=store.public(papers[a],measurement)
        checked.append({'article_number':a,'measurement_id':mid,'implementation_id':point['implementation_id'],
                        'title':point['title'],'component_scope':measurement.get('component_scope'),
                        'status':public['status'],'review_reasons':public.get('review_reasons',[]),
                        'figure_scope':record.get('scope'),'figure_number':record.get('figure_number'),
                        'figure_caption':record.get('caption'),'figure_evidence':record.get('evidence'),
                        'verified':record.get('verified',False),'measurement':measurement,
                        'performance':point['performance'],'evidence':by_evidence.get(mid,[])})
    result={'representative':len(representative),'linked_points':len(checked),'counts':dict(Counter(c['status'] for c in checked)),
            'scope_pairs':dict(Counter(str(c['component_scope'])+' -> '+str(c['figure_scope']) for c in checked)), 'points':checked}
    destination=out/('mapping-latest.json' if (out/'mapping-audit.json').exists() else 'mapping-audit.json')
    (destination).write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='points'}))


if __name__=='__main__':main()
