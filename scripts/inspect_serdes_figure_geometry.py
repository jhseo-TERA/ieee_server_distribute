"""Audit cached PDF geometry without changing extraction results."""
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
import pypdfium2 as pdfium

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from serdes_figures import text_lines, valid_rect, CAPTION, caption_score

def main():
    out=ROOT/'outputs/serdes_figure_audit'
    report=json.loads((out/'inventory.json').read_text(encoding='utf-8'))
    results=[]; hashes=defaultdict(list)
    for p in report['papers']:
        f=p['figure']
        if f['status']!='available':continue
        path=ROOT/p['pdf_local_path']; hashes[f['pdf_sha256']].append(p['article_number'])
        with pdfium.PdfDocument(str(path)) as doc:
            page=doc[f['page']-1]; width,height=page.get_size(); crop=list(page.get_cropbox()); media=list(page.get_mediabox())
            tp=page.get_textpage(); texts=[]; graphics=[]
            for obj in page.get_objects(max_depth=1,textpage=tp):
                box=obj.get_bounds()
                if not valid_rect(box,width,height):continue
                if obj.type==1:
                    try:s=obj.extract().strip()
                    except UnicodeError:continue
                    if s:texts.append({'text':s,'box':box})
                elif obj.type in (3,5):graphics.append(list(box))
            captions=[]
            for line in text_lines(texts):
                match=CAPTION.match(line['text'].strip())
                if match:captions.append({'text':line['text'],'bbox':line['box'],'score_first_line':caption_score(line['text'],match[1],p['title'])})
            first=doc[0]; titletext=first.get_textpage()
            first_text=titletext.get_text_range()[:8000]; titletext.close();first.close()
            results.append({'article_number':p['article_number'],'title':p['title'],'page':f['page'],'page_size':[width,height],
                'cropbox':crop,'mediabox':media,'nonzero_origin':any(abs(x)>.01 for x in crop[:2]+media[:2]),
                'cached_bbox':f['bbox'],'captions_on_page':captions,'graphic_boxes':graphics,
                'pdf_hash_matches':hashlib.sha256(path.read_bytes()).hexdigest()==f['pdf_sha256'],
                'first_page_excerpt':first_text})
            tp.close();page.close()
    payload={'count':len(results),'nonzero_origin':sum(p['nonzero_origin'] for p in results),
        'hash_mismatch':sum(not p['pdf_hash_matches'] for p in results),'duplicate_pdfs':[ids for ids in hashes.values() if len(ids)>1], 'papers':results}
    (out/'geometry.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in payload.items() if k!='papers'}),flush=True)

if __name__=='__main__':main()
