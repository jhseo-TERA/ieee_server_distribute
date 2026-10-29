"""Evidence ledger for the frozen 1,608 review candidates; no SQL/API writes.

Preserve baseline code before parser changes. Original PDF identities and cached
PDFium/OCR evidence are authenticated independently of model opinions.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.diagnose_reference_corpus import save,digest,io_path

BASE=ROOT/'outputs/reference_corpus_diagnostic_20261018'
OUT=ROOT/'outputs/reference_findings_adjudication_20261020'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def collect(out):
    if (out/'collection-final.json').exists():
        return
    out.mkdir(parents=True,exist_ok=True)
    baseline=read(BASE/'pipeline-code.json')
    # A recoverable source tree, not only hashes, keeps old reports verifiable.
    for name,expected in baseline.items():
        target=out/'baseline-code'/name
        if target.exists():
            if digest(target)!=expected:raise ValueError('Archived baseline changed: '+name)
        else:
            if digest(ROOT/name)!=expected:raise ValueError('Baseline changed before archival: '+name)
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
    findings=[r for r in read(BASE/'diagnostic-findings.json') if r['verdict']=='needs_review']
    assert len(findings)==1608
    papers={p['id']:p for p in read(BASE/'manifest.json')['papers']}
    for index,pid in enumerate(dict.fromkeys(r['id'] for r in findings),1):
        target=out/'evidence'/(pid+'.json')
        if target.exists():continue
        paper=papers[pid]
        if digest(ROOT/paper['pdf'])!=paper['sha256']:raise ValueError('PDF identity changed: '+paper['pdf'])
        directory=BASE/'papers'/pid
        result=read(directory/'result.json');prepared=read(directory/'prepared.json')
        proof=prepared.get('pdf_evidence')
        if not proof or digest(proof['path'])!=proof['sha256']:raise ValueError('Source evidence unavailable or changed')
        source=read(proof['path'])
        if source.get('source_sha256')!=paper['sha256']:raise ValueError('Page evidence belongs to another PDF')
        chosen=[r for r in findings if r['id']==pid]
        numbers={r['number'] for r in chosen}
        rows=[r for r in result['rows'] if r['number'] in numbers]
        pages={r.get('source_view',{}).get('page',r['page']) for r in rows}|{r['page'] for r in rows}
        pages|={p+1 for p in pages if p<len(source['pages'])}
        save(target,{'paper':paper,'result_sha256':digest(directory/'result.json'),
            'source_proof':proof,'extractor':source.get('extractor'),'rows':rows,'findings':chosen,
            'pages':[p for p in source['pages'] if p['page'] in pages]})
        if index%50==0:print(json.dumps({'stage':'collect','pdfs':index,'total':839}),flush=True)
    ledger=[]
    for path in sorted((out/'evidence').glob('*.json')):
        value=read(path)
        rows={r['number']:r for r in value['rows']}
        for f in value['findings']:
            ledger.append({'key':f['id']+':'+str(f['number']),'finding':f,'row':rows[f['number']],
                'evidence_file':str(path.relative_to(out)),'evidence_sha256':digest(path),
                'pdf_sha256':value['paper']['sha256']})
    assert len(ledger)==1608 and len({r['key'] for r in ledger})==1608
    save(out/'cases.json',ledger)
    save(out/'collection-final.json',{'cases':len(ledger),'pdfs':len({r['finding']['id'] for r in ledger}),
        'cases_sha256':digest(out/'cases.json'),'original_findings_sha256':digest(BASE/'diagnostic-findings.json'),
        'baseline_code':baseline,'status':'source_evidence_collected','external_api_calls':0,'sql_writes':0})
    print(json.dumps({'cases':len(ledger),'status':'source_evidence_collected'}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=OUT)
    args=parser.parse_args();collect(args.out.resolve())
