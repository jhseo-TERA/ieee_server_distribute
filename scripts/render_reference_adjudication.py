"""Read-only PDFium citation crops for source adjudication, no inference."""
from pathlib import Path
import argparse,json,re,sys
from difflib import SequenceMatcher

ROOT=Path(__file__).resolve().parents[1]


def locate(document,preferred,number,row):
    key=lambda s: ''.join(c.casefold() for c in s if c.isalnum())
    expected=key(row['raw'])[:160];best=None
    for pn in [preferred]+[n for n in range(1,len(document)+1) if n!=preferred]:
        page=document[pn-1];tp=page.get_textpage()
        for token in (f'[{number}]',f'{number}. ',row['raw'][:22]):
            finder=tp.search(token);found=[]
            while match:=finder.get_next():found.append(match)
            finder.close()
            for start,length in found:
                literal=tp.get_text_range(start+length, min(tp.count_chars()-start-length,350))
                candidate=key(literal)[:len(expected)]
                score=SequenceMatcher(None,expected,candidate,autojunk=False).ratio()
                if best is None or score>best[0]:best=(score,pn,start)
            if found:break
        tp.close();page.close()
        if best and best[0]>=.85:break
    return best


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--keys',type=Path,required=True)
    args=parser.parse_args();out=args.out.resolve()
    import pypdfium2 as pdfium
    from PIL import Image,ImageDraw
    keys=json.loads(args.keys.read_text(encoding='utf-8'))
    adjudications=out/'adjudications.json'
    adjudicated={r['key']:r for r in json.loads(adjudications.read_text(encoding='utf-8'))} if adjudications.exists() else {}
    images=[];records=[]
    for key in keys:
        pid,number=key.split(':');number=int(number)
        data=json.loads((out/'evidence'/(pid+'.json')).read_text(encoding='utf-8'))
        row=next(r for r in data['rows'] if r['number']==number)
        pn=row.get('source_view',{}).get('page',row['page'])
        located=next((d.get('source_page') for d in adjudicated.get(key,{}).get('decisions',[]) if d.get('source_page')),None)
        if located:pn=located
        with pdfium.PdfDocument(str(ROOT/data['paper']['pdf'])) as doc:
            location=locate(doc,pn,number,row)
            if location:score,pn,start=location
            page=doc[pn-1];tp=page.get_textpage()
            image=page.render(scale=2).to_pil().convert('RGB')
            crop=None
            if location and score>=.45:
                finder=tp.search(f'[{number+1}]',index=start+1)
                following=finder.get_next();finder.close()
                stop=min(following[0] if following else tp.count_chars(),start+1800)
                boxes=[tp.get_charbox(i) for i in range(start,stop)]
                boxes=[b for b in boxes if b[2]>b[0] and b[3]>b[1]]
                if boxes:
                    ox,oy,_,_=page.get_bbox();width=page.get_width();height=page.get_height()
                    boxes=[(b[0]-ox,b[1]-oy,b[2]-ox,b[3]-oy) for b in boxes]
                    a=boxes[0]
                    same=[b for b in boxes if (b[0]<width*.49)==(a[0]<width*.49) and b[3]<=a[3]+8]
                    y0=max(0,height-a[3]-6);y1=min(height,max(height-b[1] for b in same)+6,y0+180)
                    # A one-column proceedings page must not be mistaken for
                    # two columns merely because its label is in the left half.
                    x0=max(0,a[0]-10);x1=width-12
                    crop=(int(x0*2),int(y0*2),int(x1*2),int(y1*2))
                    image=image.crop(crop)
            if image.width>900:image=image.resize((900,round(image.height*900/image.width)))
            if not crop:
                image.thumbnail((900,1000))
            records.append({'key':key,'pdf':data['paper']['pdf'],'page':pn,'crop':crop,'pdf_sha256':data['paper']['sha256'],
                            'location_score':location[0] if location else None})
            tp.close();page.close()
        target=out/'visual'/('ref-'+key.replace(':','-')+'.png')
        target.parent.mkdir(parents=True,exist_ok=True);image.save(target)
        images.append((key,image))
    for offset in range(0,len(images),6):
        items=images[offset:offset+6];heights=[max(im.height for _,im in items[i:i+2])+30 for i in range(0,len(items),2)]
        sheet=Image.new('RGB',(1840,sum(heights)+12),'#eeeeee');draw=ImageDraw.Draw(sheet);y=6
        for index,(key,im) in enumerate(items):
            x=8+920*(index%2);draw.text((x,y),key,fill='black');sheet.paste(im,(x,y+24))
            if index%2 or index==len(items)-1:y+=heights[index//2]
        sheet.save(out/'visual'/(args.keys.stem+f'-sheet-{offset//6+1:02}.png'))
    (out/'visual'/(args.keys.stem+'-index.json')).write_text(json.dumps(records,indent=2),encoding='utf-8')
    print(json.dumps({'rendered':len(records)}))


if __name__=='__main__':main()
