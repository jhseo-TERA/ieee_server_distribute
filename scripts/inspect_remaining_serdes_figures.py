"""Read-only cache/source diagnostics for remaining architecture figures."""
from collections import Counter
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from serdes_figures import FigureStore

def main():
    store=FigureStore(ROOT)
    baseline=json.loads((ROOT/'outputs/serdes_figure_recovery_20260915/baseline.json').read_text(encoding='utf-8'))
    reasons=Counter(); examples=[]
    for paper in baseline:
        data=store.lookup(paper)
        if data['status']!='not_found':continue
        diagnostic=json.loads((store.paper_directory(paper['article_number'])/data['diagnostics_file']).read_text(encoding='utf-8'))
        captions=diagnostic['captions']
        reasons.update(set(c['reason'] for c in captions))
        examples.append({'article':paper['article_number'],'title':paper['title'],'captions':captions})
    out=ROOT/'outputs/serdes_figure_recovery_20260915/remaining-diagnostics.json'
    out.write_text(json.dumps({'reasons':dict(reasons),'papers':examples},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'reasons':dict(reasons),'papers':len(examples)}))
    for p in examples[:20]:print(json.dumps(p,ensure_ascii=False))

if __name__=='__main__':main()
