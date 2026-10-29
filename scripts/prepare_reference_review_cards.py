"""Source-only visual navigation cards; never reads extraction predictions."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.prepare_reference_validation import read,save,sample_numbers


def build(out):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    from PIL import Image,ImageDraw
    cards=[]
    for paper in read(out/'manifest.json')['papers']:
        directory=out/'sources'/paper['audit_id']
        inv=read(directory/'inventory.json')
        selected=sample_numbers(inv['detected_count'])
        snippets=[]; evidence=[]
        with pdfium.PdfDocument(str(ROOT/paper['pdf'])) as doc:
            for entry in inv['entries']:
                if entry['number'] not in selected:continue
                pn=entry['page'];page=doc[pn-1];tp=page.get_textpage()
                labeltext=lambda n:f'[{n}]' if inv['style']=='bracket' else f'{n}.' if inv['style']=='dot' else str(n)+' '
                def occurrences(text,index=0):
                    searcher=tp.search(text,index=index);found=[]
                    while match:=searcher.get_next():found.append(match)
                    searcher.close();return found
                matches=occurrences(labeltext(entry['number']))
                if not matches:tp.close();page.close();continue
                start=matches[-1][0]
                following=occurrences(labeltext(entry['number']+1),index=start+1)
                stop=min(following[0][0] if following else tp.count_chars(),start+1800)
                boxes=[]
                for pos in range(max(0,start),max(start,stop)):
                    b=tp.get_charbox(pos)
                    if b[2]>b[0] and b[3]>b[1]:boxes.append(b)
                if not boxes:tp.close();page.close();continue
                origin_x,origin_y,right_edge,top_edge=page.get_bbox()
                boxes=[(b[0]-origin_x,b[1]-origin_y,b[2]-origin_x,b[3]-origin_y) for b in boxes]
                anchor=boxes[0];height=page.get_height();width=page.get_width()
                # Include citation text through the next label, with a bounded
                # tail on the last reference so author biographies do not fill
                # a review card. Full original pages remain beside each card.
                same=[b for b in boxes if (b[0]<width*.49)==(anchor[0]<width*.49) and b[3]<=anchor[3]+5]
                ytop=min(height,height-anchor[3]+0) # image coordinates below
                bottom=min((height-b[1] for b in same),default=ytop+80)
                nearby=[b for b in boxes if abs(b[3]-anchor[3])<18]
                right=max((b[2] for b in nearby),default=width-20)
                left=max(0,anchor[0]-9)
                if right<width*.56 and anchor[0]<width*.49:right=width*.5-5
                else:right=width-20
                y0=max(0,ytop-5);y1=min(height,max(ytop+25,min(bottom+8,ytop+180)))
                image=page.render(scale=2).to_pil().crop((int(left*2),int(y0*2),int(right*2),int(y1*2)))
                snippets.append((entry['number'],pn,image))
                imagepath=directory/f'page-{pn:03}.png'
                if not imagepath.exists():page.render(scale=1.7).to_pil().save(imagepath)
                evidence.append(imagepath.name)
                tp.close();page.close()
        cellwidth=800
        heights=[]
        for _,_,image in snippets:
            ratio=min(1,cellwidth/image.width)
            heights.append(int(image.height*ratio)+24)
        pairs=[max(heights[i:i+2]) for i in range(0,len(heights),2)]
        card=Image.new('RGB',(cellwidth*2+30,max(70,sum(pairs)+40)),'white')
        draw=ImageDraw.Draw(card)
        draw.text((8,5),f"{paper['audit_id']} | {paper['pdf']} | navigation count {inv['detected_count']} (verify from source)",fill='black')
        y=30
        for i,(number,pn,image) in enumerate(snippets):
            x=10+(i%2)*(cellwidth+10)
            draw.text((x,y),f'Ref {number}, page {pn}',fill='blue')
            if image.width>cellwidth:image=image.resize((cellwidth,round(image.height*cellwidth/image.width)))
            card.paste(image,(x,y+20))
            if i%2 or i==len(snippets)-1:y+=pairs[i//2]
        target=out/'review-cards'/f"{paper['audit_id']}.png"
        target.parent.mkdir(exist_ok=True);card.save(target)
        save(out/'review-cards'/f"{paper['audit_id']}.json",{'paper':paper['audit_id'],'source_pages':sorted(set(evidence)),
            'note':'Source crops are navigation; verify incomplete crops against full original page.'})
        cards.append((paper['audit_id'],card))
    for i in range(0,len(cards),5):
        batch=cards[i:i+5]
        sheet=Image.new('RGB',(1630,sum(im.height+8 for _,im in batch)),'#dddddd')
        y=0
        for name,im in batch:sheet.paste(im,(0,y));y+=im.height+8
        sheet.save(out/'review-cards'/f'batch-{i+1:03}-{i+len(batch):03}.png')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    build(parser.parse_args().out.resolve())
