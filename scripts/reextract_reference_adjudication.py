"""Independent native PDF text for every candidate; one persistent PDFium process."""
from pathlib import Path
import sys,json,hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.reference_evidence_worker import geometric_text


def main():
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    out=ROOT/'outputs/reference_findings_adjudication_20261020'
    directory=out/'native-full';directory.mkdir(exist_ok=True)
    worker_hash=hashlib.sha256((ROOT/'scripts/reference_evidence_worker.py').read_bytes()).hexdigest()
    for index,path in enumerate(sorted((out/'evidence').glob('*.json')),1):
        target=directory/path.name
        if target.exists() and json.loads(target.read_text(encoding='utf-8')).get('worker_sha256')==worker_hash:continue
        evidence=json.loads(path.read_text(encoding='utf-8'));paper=evidence['paper']
        pdf=ROOT/paper['pdf']
        if hashlib.sha256(pdf.read_bytes()).hexdigest()!=paper['sha256']:raise ValueError('PDF changed')
        pages=[]
        with pdfium.PdfDocument(str(pdf)) as document:
            for pn in range(1,len(document)+1):
                page=document[pn-1];tp=page.get_textpage()
                literal=tp.get_text_range()
                def box(position):
                    char_index=raw.FPDFText_GetCharIndexFromTextIndex(tp,position)
                    return tp.get_charbox(char_index) if char_index>=0 else (0,0,0,0)
                text,exponents,accents=geometric_text(literal,box)
                pages.append({'page':pn,'text':text,'literal_text':literal,
                              'geometric_diacritics':accents,'geometric_exponents':exponents})
                tp.close();page.close()
        target.write_text(json.dumps({'source_sha256':paper['sha256'],'extractor':'pypdfium2',
            'worker_sha256':worker_hash,
            'pages':pages},ensure_ascii=False),encoding='utf-8')
        if index%50==0:print(json.dumps({'pdfs':index,'total':839}),flush=True)
    print('Native source re-extraction complete.',flush=True)


if __name__=='__main__':main()
