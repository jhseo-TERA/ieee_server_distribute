"""Offline, staged extraction and review inventory; original PDFs/DB untouched."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import shutil

ROOT = Path(__file__).resolve().parents[1]
# The bundled Pillow is ABI-compatible with this interpreter; PDFium is pure Python.
from PIL import Image, ImageDraw
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / '.venv/Lib/site-packages'))
from serdes_figures import FigureStore, VERSION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--article', action='append')
    parser.add_argument('--retry', action='store_true')
    parser.add_argument('--contact-status',choices=['all','available','review_required'],default='all')
    parser.add_argument('--only-status',choices=['not_found','available','review_required'])
    parser.add_argument('--promote', action='store_true', help='Publish the selected, already-staged records atomically per paper')
    args = parser.parse_args()
    out = ROOT / 'outputs/serdes_figure_upgrade'
    out.mkdir(parents=True, exist_ok=True)
    inventory = json.loads((ROOT/'outputs/serdes_figure_audit/inventory.json').read_text(encoding='utf-8'))
    papers = inventory['papers']
    if args.article:
        papers = [p for p in papers if p['article_number'] in args.article]
        if {p['article_number'] for p in papers} != set(args.article):
            raise ValueError('Some requested papers are absent from the audit inventory')
    stage = FigureStore(ROOT, 'staging-architecture-v5')
    live = FigureStore(ROOT)
    if args.only_status:
        papers = [paper for paper in papers if stage.lookup(paper)['status']==args.only_status]
    records, counts = [], Counter()
    for index, paper in enumerate(papers, 1):
        try:
            if args.promote:
                record = stage.lookup(paper)
                if record.get('version') != VERSION:
                    raise ValueError('Missing/current-source staging record required before promotion')
                source = stage.paper_directory(paper['article_number'])
                dest = live.paper_directory(paper['article_number']); dest.mkdir(parents=True, exist_ok=True)
                # Retain all historical live records. The pointer is the last write.
                for file in source.iterdir():
                    if file.name != 'index.json' and file.is_file(): shutil.copy2(file, dest/file.name)
                pointer = dest/'upgrade-index.tmp'
                shutil.copy2(source/'index.json', pointer)
                pointer.replace(dest/'index.json')
            else:
                record = stage.prepare(paper, retry=args.retry)
            records.append({'article_number':paper['article_number'], 'title':paper['title'],
                            'before':paper['figure']['status'], 'after':record})
            counts[record['status']] += 1
        except Exception as exc:
            counts['error'] += 1
            records.append({'article_number':paper['article_number'],'error':str(exc)})
        if len(papers)<=30 or index%25==0:
            print(json.dumps({'done':index,'total':len(papers),'counts':dict(counts)}),flush=True)
    report = {'version':VERSION,'promoted':args.promote,'counts':dict(counts),'papers':records}
    name = 'promotion' if args.promote else 'evaluation'
    if args.article: name += '-targeted'
    if args.only_status: name += '-'+args.only_status
    (out/f'{name}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not args.promote:
        images = [r for r in records if r.get('after',{}).get('image') and
                  (args.contact_status=='all' or r['after']['status']==args.contact_status)]
        for start in range(0,len(images),16):
            canvas=Image.new('RGB',(1600,1600),'#eee');draw=ImageDraw.Draw(canvas)
            for cell, item in enumerate(images[start:start+16]):
                row, col=divmod(cell,4);x,y=col*400,row*400; data=item['after']
                image=Image.open(stage.paper_directory(item['article_number'])/data['image']).convert('RGB')
                image.thumbnail((380,338));canvas.paste(image,(x+10,y+53))
                draw.text((x+8,y+5),f"{item['article_number']} p{data['page']} Fig{data['figure_number']} {data['status']}",fill='black')
                draw.text((x+8,y+22),','.join(data.get('review_reasons',[]))[:58],fill='black')
                draw.text((x+8,y+37),item['title'].encode('ascii','replace').decode()[:57],fill='black')
            canvas.save(out/f'{name}-{args.contact_status}-{start//16+1:02}.jpg')
    print(json.dumps({'complete':True,'counts':dict(counts)}),flush=True)
    if counts['error']: raise SystemExit(1)


if __name__ == '__main__': main()
