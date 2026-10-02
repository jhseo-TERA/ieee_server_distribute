"""Rescan remaining sources in isolation; promote only explicit visual reviews."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys
from PIL import Image,ImageDraw,ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.append(str(ROOT/'.venv/Lib/site-packages'))
import pypdfium2 as pdfium
from serdes_figures import FigureStore,render_crop
OUT=ROOT/'outputs/serdes_figure_remaining_20260915'

def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--scan',action='store_true')
    parser.add_argument('--summary',action='store_true')
    parser.add_argument('--publish-candidates',action='store_true',help='Publish new candidates only as unverified review_required, never as available')
    parser.add_argument('--packets',action='store_true')
    parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--status',choices=['review_required','not_found'])
    parser.add_argument('--article')
    parser.add_argument('--page',type=int)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args();OUT.mkdir(parents=True,exist_ok=True)
    live=FigureStore(ROOT);stage=FigureStore(ROOT,'staging-remaining-recovery')
    baseline=OUT/'baseline.json'
    if not baseline.exists():
        old=ROOT/'outputs/serdes_figure_recovery_20260915'
        decisions=json.loads((old/'decisions.json').read_text(encoding='utf-8'))
        rows=json.loads((old/'baseline.json').read_text(encoding='utf-8'))
        save(baseline,[{**p,'figure':live.lookup(p)} for p in rows if p['article_number'] not in decisions and live.lookup(p)['status'] in ('review_required','not_found')])
    rows=json.loads(baseline.read_text(encoding='utf-8'))
    if args.summary:
        decisions=json.loads((OUT/'decisions.json').read_text(encoding='utf-8'))
        pending=[];counts=Counter();transitions=Counter()
        for p in rows:
            a=p['article_number'];data=live.lookup(p);counts[data['status']]+=1
            transitions[p['figure']['status']+' -> '+data['status']]+=1
            if data['status']!='available':
                pending.append({'article_number':a,'title':p['title'],'status':data['status'],'review_reasons':data.get('review_reasons',[]),
                                'human_reviewed':a in decisions,'decision':decisions.get(a),
                                'source_page':data.get('page'),'pdf_sha256':data['pdf_sha256']})
        report={'target_count':len(rows),'human_reviewed':len(decisions),'restored':sum(d['decision']=='restore' for d in decisions.values()),
                'counts':dict(counts),'transitions':dict(transitions),'pending':pending}
        save(OUT/'summary.json',report);print(json.dumps({k:v for k,v in report.items() if k!='pending'}))
    if args.publish_candidates:
        promoted=[]
        for p in rows:
            if p['figure']['status']!='not_found':continue
            current=live.lookup(p);data=stage.lookup(p)
            if current['status']!='not_found' or not data.get('image') or data.get('verified'):continue
            assert data['fingerprint']==current['fingerprint']==p['figure']['fingerprint']
            assert hashlib.sha256(live.documents.resolve_path(p).read_bytes()).hexdigest()==data['pdf_sha256']==current['pdf_sha256']
            data={**data,'status':'review_required','verified':False,
                  'review_reasons':list(dict.fromkeys(data.get('review_reasons',[])+['source_visual_review_pending']))}
            source=stage.paper_directory(p['article_number']);dest=live.paper_directory(p['article_number'])
            for key in ('image','diagnostics_file'):shutil.copy2(source/data[key],dest/data[key])
            pointer=dest/'remaining-candidate.tmp';save(pointer,data);pointer.replace(dest/'index.json')
            promoted.append(p['article_number'])
        save(OUT/'candidate-promotion.json',{'articles':promoted,'status':'review_required','verified':False})
        print(json.dumps({'unverified_candidates_published':len(promoted)}),flush=True)
    if args.scan:
        results=[]
        for i,p in enumerate(rows,1):
            try:
                data=stage.prepare(p)
                results.append({'article_number':p['article_number'],'before':p['figure']['status'],'after':data})
            except Exception as exc:results.append({'article_number':p['article_number'],'error':str(exc)})
            if i%20==0:print(json.dumps({'done':i,'total':len(rows),'counts':dict(Counter(r.get('after',{}).get('status','error') for r in results))}),flush=True)
        save(OUT/'scan.json',results)
        print(json.dumps({'done':len(rows),'transitions':dict(Counter(r.get('before','error')+' -> '+r.get('after',{}).get('status','error') for r in results))}),flush=True)
    if args.packets:
        selected=[p for p in rows if (not args.status or p['figure']['status']==args.status) and (not args.article or p['article_number']==args.article)]
        font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',18)
        for i,p in enumerate(selected[args.start:args.start+args.count],args.start):
            data=stage.lookup(p);article=p['article_number']
            # No-candidate papers get every page, not an invented empty result.
            with pdfium.PdfDocument(str(live.documents.resolve_path(p))) as doc:
                pages=[data['page']] if data.get('image') else list(range(1,min(len(doc),24)+1))
                if args.page:pages=[args.page]
                for page_no in pages:
                    page=doc[page_no-1];bm=page.render(scale=1.8,draw_annots=False)
                    picture=bm.to_pil().convert('RGB');bm.close();picture.thumbnail((1120,1500))
                    tp=page.get_textpage();content=tp.get_text_range();tp.close()
                    canvas=Image.new('RGB',(1900,1600),'white');draw=ImageDraw.Draw(canvas)
                    canvas.paste(picture,(0,90));draw.text((10,5),f'{i:03} {article} source p{page_no} | {data["status"]}',font=font,fill='black')
                    draw.text((10,35),p['title'][:165],font=font,fill='black')
                    if data.get('image'):
                        crop=Image.open(stage.paper_directory(article)/data['image']).convert('RGB');crop.thumbnail((750,850));canvas.paste(crop,(1140,100))
                    import textwrap
                    draw.multiline_text((1140,970),'\n'.join(textwrap.wrap(data.get('caption','No candidate'),65)),font=font,fill='black')
                    stem=f'{article}-p{page_no}'
                    canvas.save(OUT/(stem+'.jpg'),quality=94)
                    save(OUT/(stem+'.json'),{'paper':p,'candidate':data,'page_text':content,'page_bounds':page.get_bbox(),'rotation':page.get_rotation(),'page_pixels':[picture.width,picture.height]})
                    page.close()
            print(article,flush=True)
    if args.apply:
        decisions=json.loads((OUT/'decisions.json').read_text(encoding='utf-8'))
        overrides=json.loads((ROOT/'config/serdes_figure_overrides.json').read_text(encoding='utf-8'))
        results=[]
        for p in rows:
            a=p['article_number'];d=decisions.get(a)
            if not d or d['decision']!='restore':continue
            entry=overrides[a]
            assert entry['evidence']==d['evidence'] and entry['review_batch']=='20260915-remaining'
            assert hashlib.sha256(live.documents.resolve_path(p).read_bytes()).hexdigest()==entry['pdf_sha256']
            previous=stage.lookup(p)
            changed=not previous.get('verified') or any(previous.get(k)!=entry.get(k) for k in ('bbox','page','scope','figure_number','implementation_ids'))
            data=stage.prepare(p,retry=changed)
            assert data['verified'] and data['status']=='available'
            if args.publish:
                src=stage.paper_directory(a);dest=live.paper_directory(a);dest.mkdir(parents=True,exist_ok=True)
                for f in src.iterdir():
                    if f.is_file() and f.name!='index.json':shutil.copy2(f,dest/f.name)
                pointer=dest/'remaining-index.tmp';shutil.copy2(src/'index.json',pointer);pointer.replace(dest/'index.json')
            image_path=stage.paper_directory(a)/data['image']
            shutil.copy2(image_path,OUT/(a+'-final.png'))
            results.append({'article_number':a,'record':data})
        save(OUT/('published.json' if args.publish else 'staged.json'),results)
        for start in range(0,len(results),6):
            canvas=Image.new('RGB',(1800,1800),'#eee');draw=ImageDraw.Draw(canvas)
            for cell,item in enumerate(results[start:start+6]):
                a=item['article_number'];data=item['record']
                picture=Image.open(OUT/(a+'-final.png')).convert('RGB');picture.thumbnail((880,540))
                x=cell%2*900;y=cell//2*600;canvas.paste(picture,(x+10,y+45))
                draw.text((x+10,y+10),f"{a} p{data['page']} Fig{data['figure_number']} {data['scope']}",fill='black')
            canvas.save(OUT/f'final-sheet-{start//6+1:02}.jpg',quality=96)
        print(json.dumps({'reviewed_restored':len(results),'published':args.publish}),flush=True)

if __name__=='__main__':main()
