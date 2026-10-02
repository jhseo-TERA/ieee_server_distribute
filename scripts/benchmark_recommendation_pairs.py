"""After compact A/B passes, compare independent pair scores and cache replay."""
import json
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.benchmark_recommendation_speed import OUT,wait_idle
from scripts.preview_recommendation_ai import PreviewWorker
from recommendation_pair_cache import PairRanker,MemoryPairCache,contextualize,pair_input
from local_ai_recommendations import MODEL,dump,SYSTEM,validated_ranks
from recommendation_ranking import compact_request,expand_ranks
from web.app import _title_terms


def main():
    summary=json.loads((OUT/'summary.json').read_text(encoding='utf-8'))
    old,new=summary['legacy'],summary['compact']
    assert new['seconds']<old['seconds'] and new['validated']>=old['validated'] and not new['errors']
    frozen=json.loads((OUT/'inputs.json').read_text(encoding='utf-8'))
    rows=[{**row,'article_number':row['id']} for body in frozen['bodies'] for row in body['candidates']]
    favorites={p['id']:{**p,'article_number':p['id']} for body in frozen['bodies'] for p in body['favorites']}
    worker=PreviewWorker()
    digest=next(m['digest'] for m in worker.runtime.status()['models'] if m['name']==MODEL)
    assert digest, 'A model artifact digest is required for cache validation.'
    original=worker._chat
    def guarded(*args,**kwargs):
        wait_idle()
        return original(*args,**kwargs)
    worker._chat=guarded
    cache=MemoryPairCache()
    req={'kind':'recommendations','model':MODEL,'num_ctx':16384,'max_tokens':2048,'thinking':'low'}
    # User-approved positives are the quality gate; old model scores alone are
    # not ground truth. These controls are never published as recommendations.
    approved_report=json.loads((OUT.parent/'ai_grounding_regression.json').read_text(encoding='utf-8'))
    approved=[approved_report['items'][i-1] for i in (1,2,5,6,7,8,10,13)]
    approved_rows=[{**row,'abstract':''} for row in approved]
    approved_favorites={str(r['related_favorite_id']):{'article_number':str(r['related_favorite_id']),
                       'title':r['related_favorite_title']} for r in approved_rows}
    approved_body={'profile':frozen['bodies'][0]['profile'],
                   'favorites':[{'id':p['article_number'],'title':p['title']} for p in approved_favorites.values()],
                   'candidates':[{'id':str(r['article_number']),'title':r['title'][:350],'abstract':'',
                                  'year':r.get('year'),'venue':r.get('source_name'),
                                  'related_favorite_id':r['related_favorite_id'],
                                  'shared_terms':r.get('shared_terms',[]),'measurements':[],
                                  'survey_missing':[]} for r in approved_rows]}
    control_path=OUT/'approved-compact.json'
    if control_path.exists():
        control=json.loads(control_path.read_text(encoding='utf-8'))
    else:
        body,schema,aliases=compact_request(approved_body)
        answer,usage=worker._chat(req,[{'role':'system','content':SYSTEM},{'role':'user','content':dump(body)}],schema,threading.Event())
        ratings,rejected=validated_ranks(expand_ranks(answer,aliases),approved_rows,approved_body['profile'],list(approved_favorites.values()))
        control={'ratings':ratings,'rejected':rejected,'usage':usage}
        control_path.write_text(json.dumps(control,ensure_ascii=False,indent=2),encoding='utf-8')
    assert len(control['ratings'])==8 and all(r['ai_score']>=70 for r in control['ratings'].values()), 'Approved positive lost in compact format.'
    print('Compact gate passed: faster, coverage preserved, all 8 user-approved papers retained.',flush=True)
    result_path=OUT/'pairs.json'
    if result_path.exists():
        result=json.loads(result_path.read_text(encoding='utf-8'))
        assert result['digest']==digest, 'Model changed; use a new benchmark fixture.'
        cache.values=result['cache']
    else:
        ranker=PairRanker(worker,cache)
        start=time.monotonic()
        ratings,stats=ranker.run(rows,favorites,req,threading.Event(),digest=digest,progress=lambda x:print(x,flush=True))
        result={'seconds':round(time.monotonic()-start,3),'ratings':ratings,'stats':stats,'cache':cache.values,'digest':digest}
        result_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    pair_control_path=OUT/'approved-pairs.json'
    if pair_control_path.exists():
        pair_control=json.loads(pair_control_path.read_text(encoding='utf-8'))
    else:
        pair_ratings,pair_stats=PairRanker(worker,MemoryPairCache()).run(approved_rows,approved_favorites,req,threading.Event(),digest=digest)
        pair_control={'ratings':pair_ratings,'stats':pair_stats}
        pair_control_path.write_text(json.dumps(pair_control,ensure_ascii=False,indent=2),encoding='utf-8')
    assert len(pair_control['ratings'])==8 and all(r['ai_score']>=70 for r in pair_control['ratings'].values()), 'Approved positive lost in independent scoring.'
    # No model call is allowed on an identical-input replay.
    def unexpected(*args,**kwargs):raise AssertionError('Identical pair caused another inference.')
    worker._chat=unexpected
    start=time.monotonic()
    replay,stats=PairRanker(worker,cache).run(rows,favorites,req,threading.Event(),digest=digest)
    contextual=contextualize(replay,rows,list(favorites.values()),frozen['bodies'][0]['profile'],_title_terms)
    qualified={key for key,r in contextual.items() if r['ai_score']>=70}
    report={'cold_seconds':result['seconds'],'replay_seconds':round(time.monotonic()-start,4),
            'cold_stats':result['stats'],'replay_stats':stats,'qualified':sorted(qualified),
            'retained_compact_qualified':len(qualified&set(new['qualified'])),
            'retained_legacy_qualified':len(qualified&set(old['qualified'])),
            'approved_compact_retained':8,'approved_pair_retained':8}
    approved_inputs={str(r['article_number']):pair_input(r,approved_favorites[str(r['related_favorite_id'])])
                     for r in approved_rows}
    mixed_approved=[str(r['article_number']) for r in rows
                    if approved_inputs.get(str(r['article_number']))==pair_input(r,favorites[str(r['related_favorite_id'])])]
    lost={key:result['ratings'][key]['ai_score'] for key in mixed_approved if result['ratings'][key]['ai_score']<70}
    report.update(mixed_approved_evaluated=len(mixed_approved),
                  mixed_approved_retained=len(mixed_approved)-len(lost),mixed_approved_lost=lost,
                  quality_gate_passed=not lost)
    (OUT/'pair_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(dump(report),flush=True)
    assert report['quality_gate_passed'], 'A known positive was lost in mixed evaluation; pair cache deployment is blocked.'


if __name__=='__main__':main()
