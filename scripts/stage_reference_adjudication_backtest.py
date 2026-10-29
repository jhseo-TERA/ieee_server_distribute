"""Reuse this run's fresh preparation under identical preparation signatures.

The fresh preparation process continues independently. No old final rows or gold
are parsing inputs. Final-stage code is frozen separately at the handoff.
"""
from pathlib import Path
import json,sys,time,shutil,ast,hashlib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import backtest_reference_flow as flow
from scripts import benchmark_reference_bulk as bulk

SOURCE=ROOT/'outputs/reference_flow_adjudication_200_20261020_v2'
OUT=ROOT/'outputs/reference_flow_adjudication_200_20261020_v4'


def preparation_module_equivalent(expected):
    archived=ROOT/'outputs/reference_findings_adjudication_20261020/baseline-code/scripts/benchmark_reference_bulk.py'
    if bulk.digest_file(archived)!=expected:raise ValueError('Unknown preparation module baseline')
    def tree(path):
        module=ast.parse(path.read_text(encoding='utf-8'))
        module.body=[n for n in module.body if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) or n.name!='enrich']
        return ast.dump(module,include_attributes=False)
    before,after=tree(archived),tree(ROOT/'scripts/benchmark_reference_bulk.py')
    if before!=after:raise ValueError('Preparation-relevant module changed')
    return hashlib.sha256(after.encode()).hexdigest()


def main():
    if not OUT.exists():flow.setup(OUT)
    flow.verify(OUT)
    manifest=bulk.read(OUT/'manifest.json');origins=[];copied=set()
    while True:
        flow.verify(OUT)
        for paper in manifest['papers']:
            pid=paper['id'];source=SOURCE/'papers'/pid/'prepared.json';target=OUT/'papers'/pid/'prepared.json'
            if pid in copied or not source.exists():continue
            data=bulk.read(source)
            if data['source']['sha256']!=paper['sha256'] or data['status']=='error':raise ValueError('Preparation identity/error')
            for key,relative in {'preparation_code':'scripts/benchmark_reference_bulk.py',
                'references_code':'paper_server/parsers/references.py','boundary_code':'paper_server/parsers/reference_segmentation.py',
                'role_url_code':'paper_server/parsers/reference_urls.py','evidence_worker':'scripts/reference_evidence_worker.py',
                'ocr_policy_code':'paper_server/parsers/reference_ocr.py','ocr_worker_code':'scripts/reference_ocr_worker.py',
                'ocr_engine_code':'scripts/reference_ocr.cjs','worker_code':'scripts/mineru_reference_worker.py'}.items():
                if data['signature'][key]!=bulk.digest_file(ROOT/relative):
                    if key=='preparation_code':preparation_module_equivalent(data['signature'][key])
                    else:raise ValueError('Preparation code changed: '+key)
            proof=data.get('pdf_evidence')
            if proof and bulk.digest_file(Path(proof['path']))!=proof['sha256']:raise ValueError('Preparation evidence changed')
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
            if bulk.digest_file(source)!=bulk.digest_file(target):raise ValueError('Preparation copy differs')
            origins.append({'id':pid,'source':str(source),'sha256':bulk.digest_file(source),
                'started_at':data['started_at'],'finished_at':data['finished_at'],'signature':data['signature']})
            copied.add(pid)
        bulk.save(OUT/'fresh-preparation-origins.json',origins)
        bulk.save(OUT/'prepare-status.json',{'status':'complete' if len(copied)==len(manifest['papers']) else 'running',
            'source':str(SOURCE),'copied':len(copied),'total':len(manifest['papers']),
            'note':'Fresh source preparation from this adjudication run, exact code/inputs/evidence verified; final-stage code frozen at handoff.'})
        if len(copied)==len(manifest['papers']):break
        print(json.dumps({'fresh_preparations_available':len(copied)}),flush=True)
        time.sleep(15)
    flow.log(OUT,'prepare_finish',seconds=sum(bulk.read(SOURCE/'papers'/p['id']/'prepared.json')['elapsed_seconds'] for p in manifest['papers']),
             origin='fresh preparation source recorded per PDF')
    flow.verify(OUT)
    print('All fresh preparations authenticated and staged.',flush=True)


if __name__=='__main__':main()
