"""Recheck final-stage changes against the same freshly prepared source inputs."""
from pathlib import Path
import argparse,contextlib,shutil,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import backtest_reference_flow as flow
from scripts import benchmark_reference_bulk as bulk
from scripts.stage_reference_adjudication_backtest import preparation_module_equivalent


def run(source,out):
    source,out=source.resolve(),out.resolve()
    flow.setup(out)
    papers=bulk.read(out/'manifest.json')['papers'];origins=[]
    for paper in papers:
        src=source/'papers'/paper['id']/'prepared.json';dst=out/'papers'/paper['id']/'prepared.json'
        data=bulk.read(src)
        if data['source']['sha256']!=paper['sha256']:raise ValueError('PDF identity mismatch')
        proof=data.get('pdf_evidence')
        if proof and bulk.digest_file(Path(proof['path']))!=proof['sha256']:raise ValueError('Source evidence changed')
        dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
        origins.append({'kind':'fresh_preparation','source':str(src),'target':str(dst.relative_to(out)),'sha256':bulk.digest_file(src)})
    for kind in ('grobid','llm'):
        for src in (source/kind).rglob('*'):
            if not src.is_file():continue
            dst=out/kind/src.relative_to(source/kind);dst.parent.mkdir(parents=True,exist_ok=True)
            if dst.exists() and bulk.digest_file(dst)!=bulk.digest_file(src):raise ValueError('Upstream cache collision')
            shutil.copy2(src,dst)
            origins.append({'kind':kind,'source':str(src),'target':str(dst.relative_to(out)),'sha256':bulk.digest_file(src)})
    bulk.save(out/'same_run_upstream-origins.json',origins)
    sample=bulk.read(out/'papers'/papers[0]['id']/'prepared.json')
    bulk.save(out/'prepare-origin-verification.json',{'ast_without_final_enrich_sha256':preparation_module_equivalent(sample['signature']['preparation_code']),
        'source':str(source),'runner_sha256':bulk.digest_file(Path(__file__)),
        'explanation':'Fresh preparation from this adjudication run; only final stages changed. Preparation AST and exact upstream inputs are unchanged. Old final rows and gold are never parser inputs.'})
    bulk.save(out/'prepare-status.json',{'status':'complete','source':str(source),'prepared':len(papers)})
    flow.log(out,'prepare_finish',seconds=sum(bulk.read(out/'papers'/p['id']/'prepared.json')['elapsed_seconds'] for p in papers),
             origin='fresh same-run preparation, authenticated upstream reuse')
    with (out/'enrich-console.log').open('w',encoding='utf-8') as log,contextlib.redirect_stdout(log):flow.enrich(out)
    return flow.report(out)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();run(args.source,args.out)
