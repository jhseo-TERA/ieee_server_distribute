"""Read-only acceptance checks for the staged or published figure upgrade."""
import argparse
from collections import Counter
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.request import Request, urlopen

from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.append(str(ROOT/'.venv/Lib/site-packages'))
import pypdfium2 as pdfium
from serdes_figures import FigureStore, VERSION, valid_rect
import web.app as app_module


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--staging',action='store_true')
    parser.add_argument('--live',action='store_true',help='Also check the existing local server on port 5001')
    parser.add_argument('--output',type=Path,help='Write a separate acceptance report without replacing prior evidence')
    parser.add_argument('--recovery-points',action='store_true',help='Check representative-point visibility for the current recovery batch')
    parser.add_argument('--review-batch',default='20260915',help='Explicit review batch used for point checks')
    parser.add_argument('--summary-only',action='store_true')
    args=parser.parse_args()
    if args.staging and args.live:raise ValueError('Live checks require published cache')
    store=FigureStore(ROOT,'staging-architecture-v5') if args.staging else FigureStore(ROOT)
    inventory=json.loads((ROOT/'outputs/serdes_figure_audit/inventory.json').read_text(encoding='utf-8'))
    reviews=json.loads((ROOT/'config/serdes_figure_overrides.json').read_text(encoding='utf-8'))
    papers={p['article_number']:p for p in inventory['papers']}
    counts=Counter();transitions=Counter();originals=0
    for article,paper in papers.items():
        record=store.lookup(paper)
        assert record.get('version')==VERSION,(article,record.get('status'))
        assert record['status'] in ('available','review_required','not_found','no_top_diagram')
        counts[record['status']]+=1
        transitions[paper['figure']['status']+' -> '+record['status']]+=1
        before=paper['figure'].get('fingerprint')
        if before:
            assert record['fingerprint']==before,(article,'PDF metadata changed')
            originals+=1
        if record['status']=='review_required':
            assert 'image_url' not in store.public(paper)
            assert store.image_path(paper) is None
    client=app_module.app.test_client()
    with client.session_transaction() as session:
        session.update(authenticated=True,username='figure-acceptance-check',role='viewer')
    # The test session stays on loopback and is never logged or persisted.
    cookie=client.get_cookie(app_module.app.config.get('SESSION_COOKIE_NAME','session'))
    def get(path):
        if args.live:
            request=Request('http://127.0.0.1:5001'+path,headers={'Cookie':f'{cookie.key}={cookie.value}'})
            with urlopen(request,timeout=15) as response:return response.read()
        with patch.object(app_module,'serdes_figures',store):
            response=client.get(path)
            assert response.status_code==200,(path,response.status_code)
            content=response.data;response.close();return content
    verified=[]
    absences=[]
    for article,review in reviews.items():
        paper=papers[article];record=store.lookup(paper)
        if review.get('disposition')=='no_top_diagram':
            assert record['status']=='no_top_diagram' and record['verified'],article
            path=store.documents.resolve_path(paper)
            assert hashlib.sha256(path.read_bytes()).hexdigest()==review['pdf_sha256'],article
            with pdfium.PdfDocument(str(path)) as doc:
                assert len(doc)==review['page_count']
                assert review['source_pages_reviewed']==list(range(1,len(doc)+1))
            data=json.loads(get(f'/api/serdes/papers/{article}/block-diagram'))
            assert data['status']=='no_top_diagram' and data['verified'] and 'image_url' not in data
            assert store.image_path(paper) is None
            absences.append(article)
            continue
        assert record['status']=='available' and record['verified'],article
        assert record['selection']=='source_review'
        for key in ('page','figure_number','bbox','scope'):
            assert record[key]==review[key],(article,key)
        path=store.documents.resolve_path(paper)
        assert hashlib.sha256(path.read_bytes()).hexdigest()==review['pdf_sha256'],article
        with pdfium.PdfDocument(str(path)) as doc:
            page=doc[review['page']-1]
            bounds=page.get_bbox()
            assert valid_rect(review['bbox'],bounds[2]-bounds[0],bounds[3]-bounds[1])
            page.close()
        data=json.loads(get(f'/api/serdes/papers/{article}/block-diagram'))
        assert data['verified'] and data['figure_number']==review['figure_number'],article
        with Image.open(BytesIO(get(data['image_url']))) as image:
            assert image.format=='PNG' and image.size==(data['width'],data['height'])
        verified.append(article)
    point_checks=[]
    for article,measurement in [('7993601',2180),('11082806',2282)]:
        data=json.loads(get(f'/api/serdes/papers/{article}/block-diagram?measurement_id={measurement}'))
        assert data['status']=='available' and data['verified'],(article,data)
        assert data['measurement_link'] in ('reviewed','implementation'),(article,data)
        point_checks.append({'article_number':article,'measurement_id':measurement,'link':data['measurement_link']})
    acquired=json.loads((ROOT/'outputs/serdes_figure_audit/acquisition.json').read_text(encoding='utf-8'))
    for paper in acquired['papers']:
        assert paper['status']=='validated'
        assert hashlib.sha256(Path(paper['path']).read_bytes()).hexdigest()==paper['sha256']
    report={'version':VERSION,'staging':args.staging,'live_http':args.live,'counts':dict(counts),
            'transitions':dict(transitions),'source_reviewed':verified,'point_checks':point_checks,
            'source_confirmed_absences':absences,
            'existing_pdf_fingerprints_unchanged':originals,'acquired_pdf_hashes_unchanged':len(acquired['papers'])}
    if args.recovery_points:
        performance=json.loads(get('/api/serdes/performance'))
        representative=set(performance['chart_sets']['representative'])
        recovered={a for a,r in reviews.items() if r.get('review_batch')==args.review_batch}
        checked=[]
        for point in performance['points']:
            article=str(point.get('article_number'));measurement=point.get('performance',{}).get('measurement_id')
            if article not in recovered or measurement not in representative:continue
            data=json.loads(get(f'/api/serdes/papers/{article}/block-diagram?measurement_id={measurement}'))
            checked.append({'article_number':article,'measurement_id':measurement,'implementation_id':point['implementation_id'],
                            'status':data['status'],'scope':data.get('scope'),'measurement_link':data.get('measurement_link'),
                            'association_verified':data.get('association_verified',False),
                            'review_reasons':data.get('review_reasons',[]),'component_scope':point['performance'].get('component_scope'),
                            'title':point['title']})
        report['recovery_representative_points']=checked
        report['recovery_point_counts']=dict(Counter(p['status'] for p in checked))
        if args.review_batch=='20260915-complete':
            from complete_serdes_figure_review import crop_digest
            batch_dir=ROOT/'outputs/serdes_figure_complete_20260915'
            frozen=json.loads((batch_dir/'baseline.json').read_text(encoding='utf-8'))
            seals=json.loads((batch_dir/'crop-approval-digests.json').read_text(encoding='utf-8'))
            frozen_counts=Counter()
            for paper in frozen:
                article=paper['article_number'];record=store.lookup(paper)
                assert record['status'] in ('available','no_top_diagram') and record['verified'],article
                frozen_counts[record['status']]+=1
                if record['status']=='available':
                    assert seals[article]==crop_digest(store,article,record),(article,'crop changed after QA')
            assert dict(frozen_counts)=={'available':473,'no_top_diagram':7}
            batch_points=[p for p in checked if p['article_number'] in {r['article_number'] for r in frozen}]
            assert len(batch_points)==480
            assert all(p['status'] in ('available','reference_only','no_top_diagram') for p in batch_points)
            report['completed_480']=dict(frozen_counts)
            report['completed_480_point_counts']=dict(Counter(p['status'] for p in batch_points))
            report['content_bound_crop_approvals_checked']=len(seals)
    destination=args.output or ROOT/'outputs/serdes_figure_upgrade'/('acceptance-staging.json' if args.staging else 'acceptance.json')
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('source_reviewed','recovery_representative_points')} if args.summary_only else report))


if __name__=='__main__':main()
