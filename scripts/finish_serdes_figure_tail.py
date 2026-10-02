"""Review the seven pre-existing held records outside the frozen 480 queue."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

from complete_serdes_figure_review import ROOT, OUT, read, save, source, source_rect_to_local, document_sheets
from PIL import Image, ImageDraw
from serdes_figures import FigureStore


def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['inspect','document','stage','publish'])
    parser.add_argument('--article');args=parser.parse_args()
    live=FigureStore(ROOT);out=OUT/'tail';out.mkdir(exist_ok=True)
    baseline=out/'baseline.json'
    if not baseline.exists():
        all_papers=read(ROOT/'outputs/serdes_figure_audit/inventory.json')['papers']
        papers=[{**p,'figure':live.lookup(p)} for p in all_papers if live.lookup(p)['status']=='review_required']
        assert len(papers)==7
        save(baseline,papers)
    papers=read(baseline)
    if args.command=='document':
        document_sheets(next(p for p in papers if p['article_number']==args.article));return
    if args.command=='inspect':
        for p in papers:
            f=p['figure'];a=p['article_number'];png,meta=source(p,f['page'])
            print(json.dumps({'id':a,'title':p['title'],'page':f['page'],'figure':f['figure_number'],
                              'caption':f['caption'],'page_count':meta['page_count'],'source':str(png),
                              'text':meta['text'],'alternatives':f.get('candidates',[])}))
        return
    decisions=read(out/'decisions.json');overrides=read(live.review_path)
    stage=FigureStore(ROOT,'staging-tail-review');stage.review_path=out/'overrides.json';staged=[]
    for p in papers:
        a=p['article_number'];d=decisions.get(a)
        if not d:continue
        _,meta=source(p,d['page']);assert meta['rotation']==0
        digest=hashlib.sha256(live.documents.resolve_path(p).read_bytes()).hexdigest()
        assert digest==p['figure']['pdf_sha256']
        e={k:d[k] for k in ('page','figure_number','caption','scope','evidence','source_pages_reviewed')}
        e.update(pdf_sha256=digest,bbox=source_rect_to_local(d['source_rect'],meta['media_box'],meta['bounds']),
                 reviewed_on='2026-09-15',review_batch='20260915-tail')
        overrides[a]=e;save(stage.review_path,overrides)
        old=stage.lookup(p);r=stage.prepare(p,retry=any(old.get(k)!=v for k,v in e.items()))
        assert r['status']=='available' and r['verified']
        im=Image.open(stage.paper_directory(a)/r['image']).convert('RGB');im.thumbnail((1400,1100))
        im.save(out/f'{a}-qa.png')
        staged.append({'article_number':a,'record':r})
        if args.command=='publish':
            approval=read(out/'approvals.json').get(a)
            from complete_serdes_figure_review import crop_digest
            assert approval==crop_digest(stage,a,r),'QA approval does not match this crop'
            dest=live.paper_directory(a);backup=out/f'{a}-before.json'
            if not backup.exists():save(backup,live.lookup(p))
            for key in ('image','diagnostics_file'):shutil.copy2(stage.paper_directory(a)/r[key],dest/r[key])
            save(dest/'index.json',r)
    save(out/'staged.json',staged)
    if args.command=='publish':save(live.review_path,overrides)
    print(json.dumps({'staged':len(staged),'published':args.command=='publish'}))


if __name__=='__main__':main()
