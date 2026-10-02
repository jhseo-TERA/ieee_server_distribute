"""Resumable, source-bound review of the frozen 480-paper queue.

Packets are review aids, never automatic approvals. Only decisions explicitly
recorded after source inspection can be staged or published.
"""
import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / '.venv/Lib/site-packages'))
import pypdfium2 as pdfium
from serdes_figures import FigureStore, render_crop

OUT = ROOT / 'outputs/serdes_figure_complete_20260915'
BATCH = '20260915-complete'
FONT = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 18)
SMALL = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 15)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(12):
        try:
            temp.replace(path)
            break
        except PermissionError:
            if attempt==11: raise
            time.sleep(0.05*(attempt+1))


def effective_decisions():
    decisions=read(OUT/'decisions.json')
    for name in ('qa-adjustments.json','source-repairs.json','repair-qa.json'):
        path=OUT/name
        if path.exists():
            for a,adjustment in read(path).items():
                decisions[a]={**decisions.get(a,{}),**adjustment}
    checks=OUT/'final-checks.json'
    if checks.exists():
        for a in read(checks):
            decisions[a]['final_crop_verified']=True
    return decisions


def inventory():
    OUT.mkdir(parents=True, exist_ok=True)
    snapshot = OUT / 'baseline.json'
    if not snapshot.exists():
        old = ROOT / 'outputs/serdes_figure_remaining_20260915'
        pending = {p['article_number'] for p in read(old/'summary.json')['pending']}
        store = FigureStore(ROOT)
        papers = [{**p, 'figure': store.lookup(p)} for p in read(old/'baseline.json') if p['article_number'] in pending]
        papers.sort(key=lambda p: (p['figure']['status'] != 'review_required', int(p['article_number'])))
        assert len(papers) == 480
        for i, p in enumerate(papers):
            p['review_index'] = i
        save(snapshot, papers)
    return read(snapshot)


def source(paper, page_no):
    """Poppler rendering plus native PDF text/coordinates; original stays untouched."""
    folder = OUT / 'sources' / paper['article_number']
    folder.mkdir(parents=True, exist_ok=True)
    meta = folder / f'p{page_no}.json'
    picture = folder / f'p{page_no}.png'
    if not meta.exists() or not picture.exists():
        path = FigureStore(ROOT).documents.resolve_path(paper)
        with pdfium.PdfDocument(str(path)) as doc:
            page = doc[page_no-1]
            tp = page.get_textpage()
            record = {'page': page_no, 'page_count': len(doc), 'bounds': page.get_bbox(), 'media_box':page.get_mediabox(),
                      'rotation': page.get_rotation(), 'text': tp.get_text_range()}
            tp.close(); page.close()
        subprocess.run(['pdftoppm', '-f', str(page_no), '-l', str(page_no), '-singlefile',
                        '-scale-to', '1800', '-png', str(path), str(picture.with_suffix(''))],
                       check=True, capture_output=True)
        save(meta, record)
    record=read(meta)
    if 'media_box' not in record:
        with pdfium.PdfDocument(str(FigureStore(ROOT).documents.resolve_path(paper))) as doc:
            page=doc[page_no-1]; record['media_box']=page.get_mediabox(); page.close()
        save(meta,record)
    return picture,record


def source_rect_to_local(rect, media_box, visible_box):
    """Poppler default MediaBox raster -> PDFium visible-page local coordinates."""
    ml,mb,mr,mt=media_box; x0,y0,x1,y1=rect
    vl,vb,_,_=visible_box
    return [ml+(mr-ml)*x0-vl,mt-(mt-mb)*y1-vb,
            ml+(mr-ml)*x1-vl,mt-(mt-mb)*y0-vb]


def packet(paper):
    f = paper['figure']; a = paper['article_number']
    target = OUT / 'packets' / f'{paper["review_index"]:03}-{a}.jpg'
    if target.exists():
        return target
    png, meta = source(paper, f['page'])
    im = Image.open(png).convert('RGB')
    tile = Image.new('RGB', (1000, 700), 'white'); draw = ImageDraw.Draw(tile)
    draw.text((8, 4), f'{paper["review_index"]:03} | {a} | p{f["page"]} Fig {f["figure_number"]} | {f["scope"]}', font=FONT, fill='black')
    draw.multiline_text((8, 30), '\n'.join(textwrap.wrap(paper['title'], 112)[:2]), font=SMALL, fill='black')
    im.thumbnail((325, 520)); tile.paste(im, (5, 85))
    crop = Image.open(FigureStore(ROOT).paper_directory(a)/f['image']).convert('RGB')
    crop.thumbnail((650, 475)); tile.paste(crop, (340, 85))
    draw.multiline_text((340, 570), '\n'.join(textwrap.wrap(f['caption'], 81)[:4]), font=SMALL, fill='black')
    draw.text((8, 674), ', '.join(f.get('review_reasons', [])), font=SMALL, fill='#994400')
    target.parent.mkdir(parents=True, exist_ok=True); tile.save(target, quality=95)
    return target


def sheets(papers, start, count):
    selected = papers[start:start+count]
    for offset in range(0, len(selected), 6):
        group = selected[offset:offset+6]
        canvas = Image.new('RGB', (2000, 2100), '#aaa')
        for cell, p in enumerate(group):
            if p['figure'].get('image'):
                canvas.paste(Image.open(packet(p)), ((cell%2)*1000, (cell//2)*700))
        name = f'sheet-{group[0]["review_index"]:03}.jpg'
        canvas.save(OUT/name, quality=95)
        print(name, flush=True)


def repair_queue(papers):
    path = OUT/'repair-queue.json'
    if not path.exists():
        ds = read(OUT/'decisions.json')
        save(path, [p['article_number'] for p in papers if ds.get(p['article_number'],{}).get('decision')=='needs_source'])
    by_id = {p['article_number']:p for p in papers}
    return [by_id[a] for a in read(path)]


def catalog(papers):
    ds = read(OUT/'decisions.json')
    for p in papers:
        path = FigureStore(ROOT).documents.resolve_path(p)
        print('\nARTICLE', p['article_number'], p['title'])
        print('REVIEW', json.dumps(ds.get(p['article_number'],{})))
        with pdfium.PdfDocument(str(path)) as doc:
            for i in range(len(doc)):
                page=doc[i]; tp=page.get_textpage(); content=tp.get_text_range()
                lines=content.splitlines()
                for j,line in enumerate(lines):
                    if re.match(r'^\s*(?:Fig\.?|Figure)\s*\d+(?:\.\d+)*[.:]\s', line, re.I):
                        print(f'P{i+1}:', ' '.join(lines[j:j+2])[:200])
                tp.close(); page.close()


def repair_sheets(papers, start, count):
    ds=effective_decisions()
    selected=repair_queue(papers)[start:start+count]
    source_sheets(selected,ds,start,'repair')


def source_sheets(selected,ds,start,prefix):
    for offset in range(0,len(selected),4):
        canvas=Image.new('RGB',(2800,3800),'#ddd'); draw=ImageDraw.Draw(canvas)
        for cell,p in enumerate(selected[offset:offset+4]):
            a=p['article_number']; d=ds.get(a,{}); f=p['figure']
            candidate=next((c for c in f.get('candidates',[]) if c['figure_number']==d.get('candidate_figure')),f)
            page_no=d.get('repair_page',d.get('page',candidate.get('page',1)))
            png,meta=source(p,page_no)
            im=Image.open(png).convert('RGB'); im.thumbnail((1380,1800))
            x=(cell%2)*1400; y=(cell//2)*1900
            draw.text((x+8,y+4),f'{start+offset+cell} | {a} | p{page_no} | {im.width}x{im.height}',font=FONT,fill='black')
            draw.multiline_text((x+8,y+28),'\n'.join(textwrap.wrap(p['title'],140)[:2]),font=SMALL,fill='black')
            canvas.paste(im,(x+8,y+85))
        name=OUT/f'{prefix}-{start+offset:03}.jpg'; canvas.save(name,quality=96); print(name)


def document_sheets(paper):
    """Whole-document evidence for a manually confirmed negative disposition."""
    a=paper['article_number']
    _,meta=source(paper,1)
    for start in range(1,meta['page_count']+1,4):
        canvas=Image.new('RGB',(2800,3800),'#ddd'); draw=ImageDraw.Draw(canvas)
        for cell,page_no in enumerate(range(start,min(start+4,meta['page_count']+1))):
            png,_=source(paper,page_no)
            im=Image.open(png).convert('RGB'); im.thumbnail((1380,1800))
            x=(cell%2)*1400; y=(cell//2)*1900
            draw.text((x+8,y+4),f'{a} | p{page_no}/{meta["page_count"]} | {im.width}x{im.height}',font=FONT,fill='black')
            draw.multiline_text((x+8,y+28),'\n'.join(textwrap.wrap(paper['title'],140)[:2]),font=SMALL,fill='black')
            canvas.paste(im,(x+8,y+85))
        path=OUT/f'document-{a}-{start:02}.jpg'; canvas.save(path,quality=96); print(path)


def apply(papers, publish=False):
    decisions = effective_decisions()
    overrides_path = ROOT/'config/serdes_figure_overrides.json'
    overrides = read(overrides_path)
    live = FigureStore(ROOT); stage = FigureStore(ROOT, 'staging-complete-review')
    stage.review_path = OUT/'staged-overrides.json'
    # Draft reviews must not affect production extraction before final crop QA.
    drafts = {a:e for a,e in overrides.items() if e.get('review_batch') == BATCH}
    if drafts:
        unpublished = {a for a in drafts if live.lookup(next(p for p in papers if p['article_number']==a)).get('review_batch') != BATCH}
        if unpublished:
            save(overrides_path, {a:e for a,e in overrides.items() if a not in unpublished})
    restored = []; published = []
    for p in papers:
        a = p['article_number']; d = decisions.get(a)
        if not d or d['decision'] != 'restore':
            continue
        assert d.get('evidence') and d.get('source_pages_reviewed')
        original = p['figure']
        assert hashlib.sha256(live.documents.resolve_path(p).read_bytes()).hexdigest() == original['pdf_sha256']
        f = {**original, **d}
        if d.get('candidate_figure'):
            candidate = next(c for c in original['candidates'] if c['figure_number'] == d['candidate_figure'])
            f = {**f, **candidate, **d}
        if 'padding' in d:
            l, b, r, t = f['bbox']; pl, pb, pr, pt = d['padding']
            f['bbox'] = [l-pl, b-pb, r+pr, t+pt]
        if 'crop_fraction' in d:
            l, b, r, t = f['bbox']; x0, y0, x1, y1 = d['crop_fraction']
            f['bbox'] = [l+(r-l)*x0, t-(t-b)*y1, l+(r-l)*x1, t-(t-b)*y0]
        if 'pixel_rect' in d:
            png, meta = source(p, d.get('page', original.get('page')))
            with Image.open(png) as im:
                pw, ph = im.size
            assert meta['rotation'] == 0, 'Use explicit PDF bbox for rotated sources'
            l,t,r,b=d['pixel_rect']
            f['bbox']=[round(v,3) for v in source_rect_to_local([l/pw,t/ph,r/pw,b/ph],meta['media_box'],meta['bounds'])]
        if 'extra_padding' in d:
            l,b,r,t=f['bbox']; pl,pb,pr,pt=d['extra_padding']
            f['bbox']=[l-pl,b-pb,r+pr,t+pt]
        if 'final_crop_fraction' in d:
            l,b,r,t=f['bbox']; x0,y0,x1,y1=d['final_crop_fraction']
            f['bbox']=[l+(r-l)*x0,t-(t-b)*y1,l+(r-l)*x1,t-(t-b)*y0]
        if 'source_rect' in d:
            _,meta=source(p,f['page'])
            assert meta['rotation']==0
            f['bbox']=source_rect_to_local(d['source_rect'],meta['media_box'],meta['bounds'])
        entry = {k: f[k] for k in ('pdf_sha256', 'page', 'figure_number', 'caption', 'bbox', 'scope', 'evidence')}
        entry.update(reviewed_on='2026-09-15', review_batch=BATCH, source_pages_reviewed=d['source_pages_reviewed'])
        if 'source_regions' in d:
            _,meta=source(p,f['page'])
            entry['include_bboxes']=[source_rect_to_local(rect,meta['media_box'],meta['bounds']) for rect in d['source_regions']]
        for key in ('measurement_ids', 'implementation_ids'):
            if key in d: entry[key] = d[key]
        prior = overrides.get(a)
        assert prior is None or prior.get('review_batch') == BATCH, a
        overrides[a] = entry
        save(stage.review_path, overrides)
        cached = stage.lookup(p)
        changed = not cached.get('verified') or any(cached.get(k) != v for k, v in entry.items())
        record = stage.prepare(p, retry=changed)
        assert record['status'] == 'available' and record['verified']
        if publish and d.get('final_crop_verified') is True:
            seals_path=OUT/'crop-approval-digests.json'
            assert seals_path.exists(), 'Seal explicitly reviewed crops before publishing'
            assert read(seals_path).get(a)==crop_digest(stage,a,record), f'{a}: crop changed after final QA'
            src = stage.paper_directory(a); dest = live.paper_directory(a); dest.mkdir(parents=True, exist_ok=True)
            backup = OUT/'before-publish'/f'{a}.json'
            if not backup.exists(): save(backup, live.lookup(p))
            for key in ('image', 'diagnostics_file'):
                shutil.copy2(src/record[key], dest/record[key])
            save(dest/'index.json', record)
            published.append({'article_number': a, 'record': record})
        restored.append({'article_number': a, 'record': record})
        if len(restored)%20 == 0: print(f'staged {len(restored)}', flush=True)
    save(OUT/'staged.json', restored)
    if publish:
        current_overrides=read(overrides_path)
        for item in published:
            a=item['article_number']; current_overrides[a]=overrides[a]
        save(overrides_path,current_overrides)
        save(OUT/'published.json', published)
    qa=[item for item in restored if not decisions[item['article_number']].get('final_crop_verified')]
    save(OUT/'qa-queue.json',[item['article_number'] for item in qa])
    for offset in range(0, len(qa), 6):
        canvas = Image.new('RGB', (2000, 1800), '#eee'); draw = ImageDraw.Draw(canvas)
        for cell, item in enumerate(qa[offset:offset+6]):
            a=item['article_number']; f=item['record']
            im=Image.open(stage.paper_directory(a)/f['image']).convert('RGB'); im.thumbnail((980, 545))
            x=(cell%2)*1000; y=(cell//2)*600
            canvas.paste(im, (x+8,y+40))
            draw.text((x+8,y+8), f'{a} p{f["page"]} Fig{f["figure_number"]} {f["scope"]}', font=FONT, fill='black')
        canvas.save(OUT/f'qa-{offset:03}.jpg', quality=96)
    print(json.dumps({'staged': len(restored), 'published': len(published)}), flush=True)


def crop_digest(store, article, record):
    geometry={k:record.get(k) for k in ('pdf_sha256','page','figure_number','caption','bbox','scope','include_bboxes')}
    geometry['image_sha256']=hashlib.sha256((store.paper_directory(article)/record['image']).read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(geometry,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def seal_crops():
    """One-time migration of this batch's explicit visual approvals to content seals."""
    path=OUT/'crop-approval-digests.json'
    assert not path.exists(), 'Never silently renew an approval after changing a crop'
    stage=FigureStore(ROOT,'staging-complete-review')
    staged=read(OUT/'staged.json')
    approved={a for a,d in effective_decisions().items() if d.get('final_crop_verified') and d.get('decision')=='restore'}
    assert approved=={item['article_number'] for item in staged}
    save(path,{item['article_number']:crop_digest(stage,item['article_number'],item['record']) for item in staged})
    print(json.dumps({'content_bound_crop_approvals':len(staged)}))


def apply_absences(papers):
    live=FigureStore(ROOT); stage=FigureStore(ROOT,'staging-complete-review')
    stage.review_path=OUT/'absence-overrides.json'
    reviews=read(OUT/'absence-reviews.json'); by_id={p['article_number']:p for p in papers}
    overrides=read(live.review_path); published=[]
    for a,review in reviews.items():
        p=by_id[a]; digest=hashlib.sha256(live.documents.resolve_path(p).read_bytes()).hexdigest()
        assert digest==p['figure']['pdf_sha256']
        entry={**review,'pdf_sha256':digest,'disposition':'no_top_diagram',
               'source_pages_reviewed':list(range(1,review['page_count']+1)),
               'reviewed_on':'2026-09-15','review_batch':BATCH}
        save(stage.review_path,{a:entry})
        record=stage.prepare(p,retry=True)
        assert record['status']=='no_top_diagram' and record['verified'] and not record.get('image')
        dest=live.paper_directory(a); backup=OUT/'before-publish'/f'{a}.json'
        if not backup.exists(): save(backup,live.lookup(p))
        shutil.copy2(stage.paper_directory(a)/record['diagnostics_file'],dest/record['diagnostics_file'])
        save(dest/'index.json',record); overrides[a]=entry
        published.append({'article_number':a,'record':record})
    save(live.review_path,overrides);save(OUT/'published-absences.json',published)
    print(json.dumps({'whole_document_absences_published':len(published)}))


def publish_context_notes():
    live=FigureStore(ROOT); overrides=read(live.review_path)
    target=ROOT/'config/serdes_figure_context_notes.json'
    entries=read(target) if target.exists() else {}
    for a,note in read(OUT/'context-notes.json').items():
        review=overrides[a]
        entries[a]={k:review[k] for k in ('pdf_sha256','page','figure_number','scope','bbox')}
        entries[a].update(note=note,reviewed_on='2026-09-15')
    save(target,entries);print(json.dumps({'qualified_figure_associations':len(entries)}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['sheets', 'info', 'source', 'document', 'apply', 'seal-crops', 'apply-absences', 'context-notes', 'summary', 'catalog', 'repair-sheets', 'qa-source'])
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--count', type=int, default=6)
    parser.add_argument('--article'); parser.add_argument('--page', type=int)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--queue', choices=['repair','missing'], default='repair')
    args = parser.parse_args(); papers = inventory()
    selected_queue = repair_queue(papers) if args.queue == 'repair' else [p for p in papers if p['figure']['status']=='not_found']
    if args.command == 'sheets': sheets(papers, args.start, args.count)
    elif args.command == 'catalog': catalog(selected_queue[args.start:args.start+args.count])
    elif args.command == 'repair-sheets': source_sheets(selected_queue[args.start:args.start+args.count],effective_decisions(),args.start,args.queue)
    elif args.command == 'qa-source':
        ds=effective_decisions()
        selected=[p for p in papers if ds.get(p['article_number'],{}).get('decision')=='restore' and not ds[p['article_number']].get('final_crop_verified')]
        source_sheets(selected[args.start:args.start+args.count],ds,args.start,'qa-source')
    elif args.command == 'info':
        for p in papers[args.start:args.start+args.count]:
            f = p['figure']
            print(json.dumps({'i': p['review_index'], 'id': p['article_number'], 'title': p['title'],
                              **{k: f.get(k) for k in ('page','figure_number','caption','context','scope','bbox')},
                              'alternatives': [{k: c.get(k) for k in ('page','figure_number','caption','scope')} for c in f.get('candidates', [])[1:4]]}, ensure_ascii=False))
    elif args.command == 'source':
        p = next(p for p in papers if p['article_number'] == args.article)
        png, meta = source(p, args.page or p['figure'].get('page', 1)); print(png); print(meta['text'])
    elif args.command == 'document': document_sheets(next(p for p in papers if p['article_number']==args.article))
    elif args.command == 'apply': apply(papers, args.publish)
    elif args.command == 'seal-crops': seal_crops()
    elif args.command == 'apply-absences': apply_absences(papers)
    elif args.command == 'context-notes': publish_context_notes()
    else:
        ds = effective_decisions() if (OUT/'decisions.json').exists() else {}
        if (OUT/'published-absences.json').exists():
            for item in read(OUT/'published-absences.json'):
                ds[item['article_number']]={'decision':'no_top_diagram'}
        result = {'target': len(papers), 'decisions': dict(Counter(d['decision'] for d in ds.values())),
                  'unreviewed': [p['article_number'] for p in papers if p['article_number'] not in ds],
                  'live_counts': dict(Counter(FigureStore(ROOT).lookup(p)['status'] for p in papers))}
        save(OUT/'summary.json', result); print(json.dumps({k:v for k,v in result.items() if k!='unreviewed'}))


if __name__ == '__main__': main()
