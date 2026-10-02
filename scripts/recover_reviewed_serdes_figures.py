"""Build source/crop review packets and apply explicitly recorded visual decisions."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import shutil
import textwrap
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.append(str(ROOT/'.venv/Lib/site-packages'))
import pypdfium2 as pdfium
from serdes_figures import FigureStore

OUT=ROOT/'outputs/serdes_figure_recovery_20260915'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--publish',action='store_true')
    args=parser.parse_args();OUT.mkdir(parents=True,exist_ok=True)
    store=FigureStore(ROOT)
    inventory=json.loads((ROOT/'outputs/serdes_figure_audit/inventory.json').read_text(encoding='utf-8'))['papers']
    snapshot=OUT/'baseline.json'
    if not snapshot.exists():
        rows=[{**p,'figure':store.lookup(p)} for p in inventory]
        snapshot.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=json.loads(snapshot.read_text(encoding='utf-8'))
    pending=[p for p in rows if p['figure']['status']=='review_required']
    if args.apply:
        decisions=json.loads((OUT/'decisions.json').read_text(encoding='utf-8'))
        config=ROOT/'config/serdes_figure_overrides.json'
        overrides=json.loads(config.read_text(encoding='utf-8'))
        restored=[]
        stage=FigureStore(ROOT,'staging-reviewed-recovery')
        for paper in pending:
            article=paper['article_number'];decision=decisions.get(article)
            if not decision or decision.get('decision')!='restore':continue
            original=paper['figure'];current=store.lookup(paper)
            entry=overrides[article]
            keys=('bbox','scope','page','figure_number','measurement_ids','implementation_ids')
            if current.get('verified') and current.get('review_batch')=='20260915' and all(current.get(k)==entry.get(k) for k in keys):continue
            assert current['pdf_sha256']==original['pdf_sha256'],article
            assert entry['review_batch']=='20260915' and entry['evidence']==decision['evidence'],article
            digest=hashlib.sha256(store.documents.resolve_path(paper).read_bytes()).hexdigest()
            assert digest==entry['pdf_sha256'],article
            staged=stage.lookup(paper)
            changed=any(staged.get(key)!=entry.get(key) for key in ('bbox','scope','page','figure_number','measurement_ids','implementation_ids'))
            record=stage.prepare(paper,retry=changed)
            assert record['status']=='available' and record['verified'],article
            assert record['bbox']==entry['bbox'] and record['scope']==entry['scope'],article
            if args.publish:
                source=stage.paper_directory(article);dest=store.paper_directory(article)
                for file in source.iterdir():
                    if file.name!='index.json' and file.is_file():shutil.copy2(file,dest/file.name)
                temporary=dest/'recovery-index.tmp';shutil.copy2(source/'index.json',temporary)
                temporary.replace(dest/'index.json')
            restored.append(article)
            print(json.dumps({'restored':article,'page':record['page'],'figure':record['figure_number']}),flush=True)
        counts=Counter(store.lookup(p)['status'] for p in rows)
        updated=restored
        target=store if args.publish else stage
        restored=[p['article_number'] for p in pending if target.lookup(p).get('review_batch')=='20260915' and target.lookup(p).get('verified')]
        (OUT/('result.json' if args.publish else 'staged.json')).write_text(json.dumps({'restored':restored,'updated_this_run':updated,'published':args.publish,'counts':dict(counts)},indent=2),encoding='utf-8')
        for start in range(0,len(restored),6):
            canvas=Image.new('RGB',(1800,1800),'#eee');draw=ImageDraw.Draw(canvas)
            for cell,article in enumerate(restored[start:start+6]):
                paper=next(p for p in pending if p['article_number']==article);data=stage.lookup(paper)
                picture=Image.open(stage.paper_directory(article)/data['image']).convert('RGB');picture.thumbnail((880,540))
                x=(cell%2)*900;y=(cell//2)*600;canvas.paste(picture,(x+10,y+45))
                draw.text((x+10,y+10),f"{article} p{data['page']} Fig{data['figure_number']} {data['scope']}",fill='black')
            canvas.save(OUT/f'restored-{start//6+1:02}.jpg',quality=95)
        print(json.dumps({'counts':dict(counts),'restored':len(restored)}));return
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',19)
    for index,paper in enumerate(pending[args.start:args.start+args.count],args.start):
        data=paper['figure'];article=paper['article_number']
        path=store.documents.resolve_path(paper)
        with pdfium.PdfDocument(str(path)) as doc:
            page=doc[data['page']-1];bounds=page.get_bbox();rotation=page.get_rotation();bitmap=page.render(scale=1.8,draw_annots=False)
            picture=bitmap.to_pil().convert('RGB');bitmap.close()
            tp=page.get_textpage();content=tp.get_text_range();tp.close();page.close()
            first=doc[0];tp=first.get_textpage();intro=tp.get_text_range();tp.close();first.close()
        canvas=Image.new('RGB',(1900,1600),'white');draw=ImageDraw.Draw(canvas)
        picture.thumbnail((1120,1500));canvas.paste(picture,(0,90))
        crop=Image.open(store.paper_directory(article)/data['image']).convert('RGB');crop.thumbnail((750,850))
        canvas.paste(crop,(1140,100))
        caption=f"{index:03} | {article} | p{data['page']} Fig{data['figure_number']} | {','.join(data.get('review_reasons',[]))}"
        draw.text((10,4),caption,font=font,fill='black')
        draw.multiline_text((10,30),'\n'.join(textwrap.wrap(paper['title'],140)),font=font,fill='black')
        lines=textwrap.wrap(data['caption'],68)+['']+textwrap.wrap(data.get('context',''),68)
        draw.multiline_text((1140,990),'\n'.join(lines),font=font,fill='black')
        canvas.save(OUT/f'{index:03}-{article}.jpg',quality=93)
        (OUT/f'{index:03}-{article}.json').write_text(json.dumps({'paper':paper,'page_text':content,'intro':intro,
            'packet_transform':{'page_bounds':bounds,'rotation':rotation,'page_pixels':[picture.width,picture.height],
                                'offset':[0,90],'packet_size':[1900,1600]}},ensure_ascii=False,indent=2),encoding='utf-8')
        print(caption,flush=True)


if __name__=='__main__':main()
