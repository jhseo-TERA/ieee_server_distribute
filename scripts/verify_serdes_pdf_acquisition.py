"""Validate precisely the missing PDFs in the frozen survey audit inventory."""
import hashlib
import argparse
import html
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
import pypdfium2 as pdfium

ROOT=Path(__file__).resolve().parents[1]

def words(value):
    return set(re.findall(r'[a-z0-9]+', html.unescape(value).lower()))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--check-db',action='store_true')
    args=parser.parse_args()
    out=ROOT/'outputs/serdes_figure_audit'
    inventory=json.loads((out/'inventory.json').read_text(encoding='utf-8'))
    review_path=out/'identity_reviews.json'
    reviews=json.loads(review_path.read_text(encoding='utf-8')) if review_path.exists() else {}
    results=[]
    for paper in inventory['papers']:
        if paper['figure']['status']!='missing_pdf': continue
        path=ROOT/'ieee-pdf'/f"{paper['article_number']}.pdf"
        item={k:paper[k] for k in ('article_number','title','doi')}
        item.update(path=str(path),status='missing')
        if path.is_file():
            try:
                with pdfium.PdfDocument(str(path)) as doc:
                    if not len(doc):raise ValueError('empty PDF')
                    for i in range(len(doc)):
                        page=doc[i]
                        if i==0:
                            tp=page.get_textpage(); first=tp.get_text_range(); tp.close()
                        page.get_size();page.close()
                    expected=words(paper['title']); actual=words(first)
                    overlap=len(expected & actual)/max(1,len(expected))
                    doi=paper.get('doi') or ''
                    doi_match=bool(doi) and doi.lower() in first.lower()
                    item.update(status='validated' if overlap>=.9 or doi_match else 'identity_review',
                        pages=len(doc),title_word_overlap=round(overlap,3),doi_on_first_page=doi_match,
                        first_page_text=first[:10000],size=path.stat().st_size,sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                    review=reviews.get(str(paper['article_number']))
                    if review and review['sha256']==item['sha256'] and review['decision']=='match':
                        item.update(status='validated',identity_method='visual_review',identity_evidence=review['evidence'])
                    else:
                        item['identity_method']='title_or_doi_text'
            except Exception as exc:item.update(status='invalid',error=str(exc))
        results.append(item)
    payload={'checked_at':datetime.now(timezone.utc).isoformat(),'target':len(results),
        'validated':sum(p['status']=='validated' for p in results),
        'missing':sum(p['status']=='missing' for p in results),
        'review':sum(p['status']=='identity_review' for p in results),
        'invalid':sum(p['status']=='invalid' for p in results),'papers':results}
    if args.check_db:
        sys.path.insert(0,str(ROOT))
        from sqlalchemy import bindparam, text
        from web.app import engine
        from serdes_figures import FigureStore
        from collections import Counter
        ids=[p['article_number'] for p in results]
        with engine.connect() as conn:
            rows=[dict(r) for r in conn.execute(text(
                'SELECT id,article_number,title,source_system,pdf_available,pdf_local_path FROM papers WHERE article_number IN :ids'
            ).bindparams(bindparam('ids',expanding=True)),{'ids':ids}).mappings()]
        store=FigureStore(ROOT)
        payload['database']={'target':len(ids),'rows':len(rows),
            'available':sum(bool(r['pdf_available']) for r in rows),
            'local_paths_exist':sum(bool(r['pdf_local_path']) and (ROOT/r['pdf_local_path']).is_file() for r in rows),
            'figure_states':dict(Counter(store.lookup(r)['status'] for r in rows))}
    (out/'acquisition.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in payload.items() if k!='papers'}))
    for p in results:
        if p['status'] not in ('validated','missing'):print(p['article_number'],p['status'],p.get('title_word_overlap'))

if __name__=='__main__':main()
