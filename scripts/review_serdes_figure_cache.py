"""Build read-only visual QA sheets and heuristic flags (not verdicts)."""
import json
import re
import sys
from collections import Counter
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from serdes_figures import FigureStore

def main():
    out = ROOT / 'outputs/serdes_figure_audit'
    report = json.loads((out / 'inventory.json').read_text(encoding='utf-8'))
    rows = [p for p in report['papers'] if p['figure']['status'] == 'available']
    store = FigureStore(ROOT)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 15)
    flagged = []
    for p in rows:
        f = p['figure']; cap = f['caption'].lower(); flags = []
        if re.search(r'\([a-e]\)', cap): flags.append('multi_panel')
        if re.search(r'\[\d', cap): flags.append('cited_figure')
        if re.search(r'experiment|measurement|test.?bench|testbed', cap): flags.append('measurement_context')
        if re.search(r'general|various|different architectures|conventional|concept|comparison', cap): flags.append('background_or_comparison')
        if re.search(r'bbpd|phase detector|clocking|gearbox|adc block|front.end and adc|single.ended da', cap): flags.append('subblock_candidate')
        candidates = f.get('candidates', [])
        if len(candidates)>1 and candidates[0]['score']==candidates[1]['score']: flags.append('top_score_tie')
        p['qa_flags'] = flags
        if flags: flagged.append(p)
    (out/'quality_flags.json').write_text(json.dumps({'counts':dict(Counter(x for p in rows for x in p['qa_flags'])), 'flagged_count':len(flagged), 'papers':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    for start in range(0,len(rows),24):
        sheet = Image.new('RGB',(1600,1800),'#e0e0e0'); draw=ImageDraw.Draw(sheet)
        for j,p in enumerate(rows[start:start+24]):
            x=(j%4)*400; y=(j//4)*300; f=p['figure']
            draw.rectangle((x+2,y+2,x+397,y+297),fill='white')
            draw.text((x+6,y+5),f"{start+j+1} | {p['article_number']} | Fig {f['figure_number']}",font=font,fill='black')
            im=Image.open(store.paper_directory(p['article_number'])/f['image']).convert('RGB')
            im.thumbnail((386,230)); sheet.paste(im,(x+7+(386-im.width)//2,y+30))
            draw.text((x+6,y+265),p['title'][:47],font=font,fill='black')
            draw.text((x+6,y+282),','.join(p['qa_flags'])[:48],font=font,fill='red')
        sheet.save(out/f'sheet_{start//24+1:02}.jpg')
    print(json.dumps({'counts':dict(Counter(x for p in rows for x in p['qa_flags'])), 'flagged':len(flagged),'sheets':(len(rows)+23)//24}))

if __name__=='__main__': main()
