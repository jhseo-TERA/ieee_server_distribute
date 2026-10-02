"""Frozen 64-candidate legacy/compact A/B, no production jobs/profiles published."""
import json
from pathlib import Path
import sys
import threading
import time
from copy import deepcopy
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
from local_ai_recommendations import MODEL, RANK_SCHEMA, SYSTEM, dump, validated_ranks
from recommendation_ranking import compact_request, expand_ranks
from scripts.preview_recommendation_ai import PreviewService, PreviewWorker
import web.app as app

OUT = ROOT/'outputs/recommendation_quality/speed_ab'


def idle():
    with app.engine.connect() as conn:
        return not conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued','running')")).scalar()


def wait_idle():
    for _ in range(180):
        if idle():
            return
        time.sleep(10)
    raise RuntimeError('Production AI stayed busy; no competing inference started.')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    worker = PreviewWorker()
    req = {'kind':'recommendations','model':MODEL,'num_ctx':16384,'max_tokens':2048,'thinking':'low'}
    event = threading.Event()
    freeze = OUT/'inputs.json'
    if freeze.exists():
        frozen = json.loads(freeze.read_text(encoding='utf-8'))
    else:
        print('Waiting for production AI before freezing benchmark inputs.',flush=True)
        wait_idle()
        bodies = []
        service = PreviewService(app.engine, worker, app.build_sql_recommendations)
        original = worker._chat
        def capture(request, messages, schema, cancel, *args):
            if schema is RANK_SCHEMA:
                body = json.loads(messages[-1]['content'])
                bodies.append(body)
                return {'rankings': []}, {}
            return original(request,messages,schema,cancel,*args)
        worker._chat = capture
        try:
            with patch('local_ai_recommendations.RANKING_MODE', 'legacy'):
                snap = service.snapshot()
                service.run('benchmark-capture', {'signature':snap['signature'],**req},event)
        except ValueError as exc:
            if not bodies:
                raise
        finally:
            service.close()
            worker._chat = original
        frozen = {'signature':snap['signature'],'bodies':bodies}
        freeze.write_text(json.dumps(frozen,ensure_ascii=False,indent=2),encoding='utf-8')
    assert sum(len(body['candidates']) for body in frozen['bodies']) == 64
    summary = {}
    # Alternate order to reduce warmup/order bias; completed batches are resumable.
    for i, body in enumerate(frozen['bodies']):
        for variant in (('legacy','compact') if i%2==0 else ('compact','legacy')):
            target = OUT/f'{variant}-{i}.json'
            if target.exists():
                continue
            wait_idle()
            payload, schema, aliases = (body,RANK_SCHEMA,None) if variant=='legacy' else compact_request(body)
            started=time.monotonic()
            try:
                answer, usage=worker._chat(req,[{'role':'system','content':SYSTEM},
                                               {'role':'user','content':dump(payload)}],schema,event)
                expanded=answer if variant=='legacy' else expand_ranks(answer,aliases)
                candidates=[{**row,'article_number':row['id']} for row in body['candidates']]
                seeds=[{**row,'article_number':row['id']} for row in body['favorites']]
                ratings,rejected=validated_ranks(expanded,candidates,body['profile'],seeds)
                result={'ratings':ratings,'rejected':rejected,'usage':usage,'answer':answer,'error':None}
            except (ValueError,RuntimeError) as exc:
                result={'ratings':{},'error':str(exc),'usage':{}}
            result['seconds']=round(time.monotonic()-started,3)
            target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(dump({'variant':variant,'batch':i+1,'seconds':result['seconds'],
                        'validated':len(result['ratings']),'error':result['error']}),flush=True)
    # Retry only unvalidated compact items once, without replacing valid scores.
    for i,body in enumerate(frozen['bodies']):
        first=json.loads((OUT/f'compact-{i}.json').read_text(encoding='utf-8'))
        missing=[row for row in body['candidates'] if row['id'] not in first['ratings']]
        target=OUT/f'compact-retry-{i}.json'
        if not missing or target.exists():
            continue
        wait_idle()
        needed={row['related_favorite_id'] for row in missing}
        retry_body={**body,'candidates':missing,'favorites':[p for p in body['favorites'] if p['id'] in needed]}
        payload,schema,aliases=compact_request(retry_body)
        started=time.monotonic()
        answer,usage=worker._chat(req,[{'role':'system','content':SYSTEM},{'role':'user','content':dump(payload)}],schema,event)
        ratings,rejected=validated_ranks(expand_ranks(answer,aliases),[{**r,'article_number':r['id']} for r in missing],
                                        body['profile'],[{**p,'article_number':p['id']} for p in retry_body['favorites']])
        result={'ratings':ratings,'rejected':rejected,'usage':usage,'answer':answer,'error':None,
                'attempted':len(missing),'seconds':round(time.monotonic()-started,3)}
        target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(dump({'variant':'compact-retry','batch':i+1,'seconds':result['seconds'],'validated':len(ratings)}),flush=True)
    for variant in ('legacy','compact'):
        parts=[json.loads((OUT/f'{variant}-{i}.json').read_text(encoding='utf-8')) for i in range(8)]
        first_pass=sum(len(p['ratings']) for p in parts)
        retries=[json.loads(p.read_text(encoding='utf-8')) for p in OUT.glob('compact-retry-*.json')] if variant=='compact' else []
        parts+=retries
        ratings={key:value for part in parts for key,value in part['ratings'].items()}
        summary[variant]={'seconds':round(sum(p['seconds'] for p in parts),2),'validated':len(ratings),
                          'qualified':[key for key,r in ratings.items() if r['ai_score']>=70],
                          'output_tokens':sum(p['usage'].get('eval_count',0) for p in parts),
                          'errors':sum(bool(p['error']) for p in parts),'first_pass_validated':first_pass,
                          'retried_count':sum(p['attempted'] for p in retries)}
    summary['retained_legacy_qualified']=len(set(summary['legacy']['qualified'])&set(summary['compact']['qualified']))
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(dump(summary),flush=True)


if __name__=='__main__':
    main()
