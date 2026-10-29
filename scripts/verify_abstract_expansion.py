"""Verify paired pool preservation and grounding against saved canonical text."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import text
import web.app as server
from paper_server.accounts import account_scope, favorite_sql
from paper_server.recommendation.abstract_expansion import concepts, domains
from paper_server.recommendation.policy import identity_text, excluded_electrical_multicarrier

OUT = ROOT / 'outputs/recommendation_retrieval_v7'


def main():
    baseline = json.loads((OUT / 'baseline.json').read_text(encoding='utf-8'))
    current = json.loads((OUT / 'candidate.json').read_text(encoding='utf-8'))
    assert current['same_inputs'], 'Favorites/corpus changed: rerun paired comparison before making preservation claims'
    before = {row['article_number']: row for row in baseline['pool']}
    after = {row['article_number']: row for row in current['pool']}
    assert before.keys() <= after.keys()
    for article, row in before.items():
        for field, value in row.items():
            if field != 'retrieval_basis':
                assert after[article].get(field) == value, (article, field)
    extra = [row for article, row in after.items() if article not in before]
    with account_scope(current['account_id']), server.engine.connect() as conn:
        abstracts = {str(row.article_number): row.abstract_text for row in conn.execute(text(
            'SELECT p.article_number,a.abstract_text FROM papers p '
            'JOIN paper_current_abstracts a ON a.paper_id=p.id '
            f'WHERE {favorite_sql("p")}=1 AND a.is_current=1'))}
    for row in extra:
        terms = set(row['abstract_expansion_terms'])
        seed_title = set(concepts(row['related_favorite_title']))
        seed_abstract = concepts(abstracts[row['related_favorite_id']], abstract=True)
        candidate_terms = set(concepts(row['title']))
        assert terms and terms <= candidate_terms & seed_abstract.keys()
        assert not terms & seed_title
        assert seed_title & candidate_terms or row['abstract_context']
        assert all(seed_abstract[term] == phrase for term, phrase in row['abstract_expansion_evidence'].items())
        assert not excluded_electrical_multicarrier(row)
        assert row['article_number'] != '4145386', 'Generic similar/LSB regression'
    assert len({identity_text(row['title']) for row in current['pool']}) == len(current['pool'])
    report = {'same_inputs': True, 'title_candidates_preserved': len(before), 'title_fields_unchanged': True,
              'additional_grounded_candidates': len(extra), 'generic_similar_regression_rejected': True,
              'abstract_candidates_displayed': {mode: sum(bool(row.get('abstract_expansion_terms')) for row in rows)
                                                for mode, rows in current['lists'].items()},
              'display_list_changes': current['new_to_baseline'],
              'note': 'Grounding and regression checks are not human relevance labels for all candidates.'}
    (OUT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
