"""Compare priority stages using the original committed SQL functions in isolation."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
import web.app as app
from recommendation_policy import FavoriteSimilarity, weighted_profile, SOURCE_PROFILE_CAP


def main():
    source = subprocess.run(['git', 'show', 'HEAD:web/app.py'], cwd=ROOT, check=True,
                            capture_output=True, encoding='utf-8').stdout
    names = {'_title_terms', '_build_profile', '_local_profile_weight', '_merge_profile_terms',
             '_title_key', '_is_low_quality_title', '_year_number', '_rank_and_filter_candidates',
             '_select_daily_recommendations', '_search_source', '_assemble_source_recommendations',
             'build_sql_recommendations'}
    functions = [n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names
    original = dict(vars(app))
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<original-recommendations>', 'exec'), original)
    with app.engine.connect() as conn:
        favorites = [dict(r) for r in conn.execute(text(
            'SELECT article_number,title,source_name FROM papers WHERE is_favorite=1 ORDER BY article_number')).mappings()]
    output = ROOT / 'outputs/recommendation_quality'
    baseline = json.loads((output/'baseline.json').read_text(encoding='utf-8'))
    assert favorites == baseline['favorites'], 'Favorites changed: rerun the baseline.'
    stages = []

    def measure(label, builder):
        start = time.monotonic()
        data = builder(20)
        items = data['items']
        stats = {'stage': label, 'items': len(items), 'sources': len(data['groups']),
                 'overlap_lte2': sum(r.get('keyword_overlap', 0) <= 2 for r in items),
                 'seconds': round(time.monotonic()-start, 3), 'profile_terms': data['profile_terms'],
                 'allocation': {g['name']:g['count'] for g in data['groups']}}
        stages.append(stats)
        print(json.dumps(stats, ensure_ascii=False), flush=True)
        return data

    measured = measure('baseline', original['build_sql_recommendations'])
    assert {r['article_number'] for r in measured['items']} == {r['article_number'] for r in baseline['result']['items']}, 'Original baseline does not match.'
    old_profile = original['_build_profile']
    original['_build_profile'] = lambda titles, *args, **kwargs: (
        weighted_profile(favorites, app._title_terms) if len(titles) == len(favorites)
        else old_profile(titles, *args, **kwargs))
    measure('1_source_weight_cap', original['build_sql_recommendations'])
    similarity = FavoriteSimilarity(favorites, app._title_terms)
    original['_rank_and_filter_candidates'] = lambda rows, terms, keys, limit, current_year=None: (
        app._rank_and_filter_candidates(rows, terms, keys, limit, current_year, similarity))
    measure('2_relevance_and_duplicate_gate', original['build_sql_recommendations'])
    measure('3_global_quality_budget', app.build_sql_recommendations)
    from collections import Counter
    counts = Counter(r['source_name'] for r in favorites)
    total_weight = sum(min(SOURCE_PROFILE_CAP, n) for n in counts.values())
    result = {'same_favorites': True, 'stages': stages, 'source_weight_shares': {
        source: {'before_percent': round(n/len(favorites)*100, 2),
                 'after_percent': round(min(SOURCE_PROFILE_CAP, n)/total_weight*100, 2)}
        for source, n in counts.items()}}
    (output/'ablation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
