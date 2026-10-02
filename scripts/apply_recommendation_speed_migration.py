"""Add pair cache and seed only independently validated benchmark scores."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from sqlalchemy import text
from web.app import engine
from scripts.apply_recommendation_migration import snapshot
from scripts.preview_recommendation_ai import PreviewWorker
from local_ai_recommendations import MODEL
from recommendation_pair_cache import PairCache, pair_key, pair_input, validated_pairs


def main():
    output=ROOT/'outputs/recommendation_quality/speed_ab'
    summary=json.loads((output/'pair_summary.json').read_text(encoding='utf-8'))
    assert summary.get('quality_gate_passed'), 'Independent score quality gate must pass before applying/seeding the cache.'
    assert summary['approved_compact_retained']==8 and summary['approved_pair_retained']==8
    assert summary['replay_stats']['cached_count']==64 and summary['replay_stats']['new_attempted_count']==0
    result=json.loads((output/'pairs.json').read_text(encoding='utf-8'))
    digest=next(m['digest'] for m in PreviewWorker().runtime.status()['models'] if m['name']==MODEL)
    assert digest==result['digest'], 'Model changed since validation; do not seed cache.'
    frozen=json.loads((output/'inputs.json').read_text(encoding='utf-8'))
    values={}
    for body in frozen['bodies']:
        for row in body['candidates']:
            row={**row,'article_number':row['id']}
            anchor=next(p for p in body['favorites'] if p['id']==row['related_favorite_id'])
            anchor={**anchor,'article_number':anchor['id']}
            rating=result['ratings'][row['id']]
            check={'r':[{'i':'p0','f':'f0' if rating['ai_favorite_id']==anchor['id'] else '',
                         's':rating['ai_score'],'e':rating['ai_evidence']}]}
            valid,_=validated_pairs(check,[row],{anchor['id']:anchor})
            assert len(valid)==1
            key=pair_key(pair_input(row,anchor),MODEL,digest)
            values[key]={'score':rating['ai_score'],'evidence':rating['ai_evidence'],'favorite_id':anchor['id']}
    with engine.connect() as conn:
        assert not conn.execute(text("SELECT COUNT(*) FROM ai_jobs WHERE status IN ('queued','running')")).scalar(), 'Wait for active AI jobs.'
        before=snapshot(conn)
    with engine.begin() as conn:
        conn.execute(text((ROOT/'scripts/migrations/012_recommendation_pair_cache.sql').read_text(encoding='utf-8')))
    cache=PairCache(engine)
    cache.write(values)
    assert len(cache.read(values))==len(values)
    with engine.connect() as conn:
        after=snapshot(conn)
    report={'migration':'012_recommendation_pair_cache.sql','validated_pairs_seeded':len(values),
            'source_before':before,'source_after':after,'source_unchanged':before==after}
    (output/'migration.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
