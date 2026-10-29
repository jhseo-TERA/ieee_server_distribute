"""Bounded PDFium text and geometric exponent evidence; no model or network."""
from __future__ import annotations

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import re
import unicodedata


def geometric_diacritics(text, box):
    """Attach a detached accent only to the letter it physically sits above."""
    accents={'\u00a8':'\u0308','\u00b4':'\u0301','\u02c7':'\u030c',**{c:c for c in '\u0300\u0301\u0302\u0303\u0308\u030a\u030c'}}
    inserts={};removed=set();edits=[]
    for index,char in enumerate(text):
        if char not in accents:continue
        mark=box(index);mx=(mark[0]+mark[2])/2;candidates=[]
        for position in range(max(0,index-100),min(len(text),index+100)):
            base=text[position]
            if not base.isalpha() or 'LATIN' not in unicodedata.name(base,''):continue
            bounds=box(position);h=bounds[3]-bounds[1];w=bounds[2]-bounds[0]
            if h<=0 or w<=0:continue
            if (bounds[0]-.12*w<=mx<=bounds[2]+.12*w and mark[1]>=bounds[1]+.65*h
                    and mark[1]<=bounds[3]+.8*h and mark[3]>=bounds[3]-.1*h):
                # TeX fonts often use dotless i under a separately positioned
                # accent; the printed acute-i is the ordinary Latin í.
                combined=unicodedata.normalize('NFC',('i' if base=='ı' else base)+accents[char])
                if len(combined)==1 and combined!=base:
                    candidates.append((abs(mx-(bounds[0]+bounds[2])/2),position,combined,bounds))
        if not candidates:continue
        candidates.sort()
        if len(candidates)>1 and candidates[1][0]-candidates[0][0]<.15:continue
        _,position,combined,bounds=candidates[0]
        if position in inserts:continue
        inserts[position]=combined;removed.add(index)
        left=position-1
        while left>=0 and (text[left].isspace() or left==index):left-=1
        if left>=0 and text[left].isalpha() and position-left<8:
            previous=box(left);h=min(bounds[3]-bounds[1],previous[3]-previous[1])
            if h>0 and -.1*h<=bounds[0]-previous[2]<=.25*h and abs(bounds[1]-previous[1])<.3*h:
                removed.update(i for i in range(left+1,position) if text[i] in ' \t')
        edits.append({'accent_index':index,'base_index':position,'literal':char,'base':text[position],
                      'combined':combined,'accent_box':mark,'base_box':bounds,
                      'removed_space_indices':sorted(i for i in removed if i!=index and text[i] in ' \t'),
                      'rule':'detached_accent_geometrically_above_latin_letter'})
    return ''.join(inserts.get(i,c) for i,c in enumerate(text) if i not in removed),edits


def geometric_exponents(text, box):
    """Only insert ^ when the printed exponent is smaller AND raised.

    Plain '211-1' stays literal. Bibliographic labels and normal baseline text
    cannot become powers merely because a PRBS expression is expected.
    """
    edits = []
    for match in re.finditer(r'\b2(\d{1,4})(?=[−–-]1\b)', text):
        base = box(match.start())
        exponent = [box(i) for i in range(match.start(1), match.end(1))]
        h = base[3] - base[1]
        if h > 0 and all(0 < b[3]-b[1] < .9*h and b[1] > base[1]+.2*h for b in exponent):
            edits.append({'index': match.start(1), 'base_box': base, 'exponent_boxes': exponent,
                          'literal': match[0], 'rule': 'smaller_and_raised_numeric_exponent'})
    for edit in reversed(edits):
        text = text[:edit['index']] + '^' + text[edit['index']:]
    return text, edits


def geometric_text(literal, box):
    """Apply both geometry edit sets at their original, authenticated offsets."""
    _,exponents=geometric_exponents(literal,box)
    _,accents=geometric_diacritics(literal,box)
    replacements={e['base_index']:e['combined'] for e in accents}
    removed={e['accent_index'] for e in accents}|{i for e in accents for i in e.get('removed_space_indices',[])}
    insertions={e['index']:'^' for e in exponents}
    text=''.join(insertions.get(i,'')+replacements.get(i,c) for i,c in enumerate(literal) if i not in removed)
    return text,exponents,accents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8'))
    root = Path(__file__).resolve().parents[1]
    path = Path(request['pdf']).resolve()
    if path.suffix.lower() != '.pdf' or not any(path.is_relative_to((root/p).resolve()) for p in
                                              ('ieee-pdf','optica-pdf','nature-pdf','DesignCon')):
        raise ValueError('PDF outside authorized library')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != request['source_sha256'] or path.stat().st_size > 64*1024*1024:
        raise ValueError('Source hash or size rejected')
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    pages = []
    with pdfium.PdfDocument(str(path)) as document:
        if not 1 <= len(document) <= 300:
            raise ValueError('Page count outside bounds')
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            try:
                literal = textpage.get_text_range()
                def box(position):
                    char_index = raw.FPDFText_GetCharIndexFromTextIndex(textpage, position)
                    if char_index < 0:
                        return (0,0,0,0)
                    return textpage.get_charbox(char_index)
                text,exponents,accents=geometric_text(literal,box)
                pages.append({'page': index+1, 'text': text, 'literal_text': literal,
                              'geometric_exponents': exponents,'geometric_diacritics':accents})
            finally:
                textpage.close()
                page.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'source_sha256': digest, 'extractor': 'pypdfium2',
        'extractor_version': version('pypdfium2'), 'pages': pages}, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__':
    main()
