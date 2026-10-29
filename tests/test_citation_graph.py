"""Evidence checks for the independent scholarly citation map.

These tiny networks distinguish a shared bibliography, observed co-citation,
and a direct citation. No live provider or local database is needed.
"""
from copy import deepcopy
import unittest

from paper_server.citation_graph import build_citation_graph


def work(number, references=(), **metadata):
    return {
        'id': f'W{number}',
        'doi': f'10.1234/paper-{number}',
        'title': f'Paper {number}',
        'authors': 'A Researcher',
        'year': 2024,
        'source_name': 'Example Journal',
        'reference_ids': [f'W{reference}' for reference in references],
        'citation_count': 0,
        'abstract': '',
        **metadata,
    }


def snapshot(*works, seed_id='W1'):
    return {
        'seed_id': seed_id,
        'works': list(works),
        'fetched_at': '2026-10-11T12:00:00+00:00',
        'meta': {'provider': 'openalex', 'candidate_count': len(works)},
    }


def edge_between(graph, left, right):
    wanted = frozenset((left, right))
    return next(edge for edge in graph['edges']
                if frozenset((edge['source'], edge['target'])) == wanted)


class CitationEvidenceTests(unittest.TestCase):
    def test_saved_lineage_keeps_direct_edges_beyond_the_visible_similarity_map(self):
        graph = build_citation_graph(snapshot(work(1, [2]), work(2, [3]), work(3)), limit=1)
        self.assertEqual([row['id'] for row in graph['nodes']], ['W1'])
        self.assertEqual({row['id'] for row in graph['lineage_nodes']}, {'W1', 'W2', 'W3'})
        self.assertEqual(graph['lineage_edges'], [{'source': 'W2', 'target': 'W1'},
                                                 {'source': 'W3', 'target': 'W2'}])
        self.assertEqual(graph['citation_edges'], [])
        self.assertEqual(graph['meta']['lineage_scope'], 'saved_neighborhood')

    def test_timeline_arrows_follow_reference_direction_even_when_ids_or_years_disagree(self):
        graph = build_citation_graph(snapshot(work(1, [2], year=2021), work(2, year=2024)))
        self.assertEqual(graph['citation_edges'], [
            {'source': 'W2', 'target': 'W1', 'cited': 'W2', 'citing': 'W1'}])
        self.assertEqual(graph['meta']['citation_edge_direction'], 'cited_to_citing')

    def test_timeline_does_not_turn_shared_references_into_direct_citation(self):
        graph = build_citation_graph(snapshot(work(1, [100]), work(2, [100])))
        self.assertTrue(graph['edges'])
        self.assertEqual(graph['citation_edges'], [])

    def test_timeline_edges_are_complete_deduplicated_and_bounded_to_displayed_nodes(self):
        graph = build_citation_graph(snapshot(work(1, [2, 2, 3]), work(2, [3]), work(3, [1])), limit=2)
        ids = {node['id'] for node in graph['nodes']}
        self.assertTrue(all(edge['source'] in ids and edge['target'] in ids for edge in graph['citation_edges']))
        self.assertEqual(len(graph['citation_edges']), len({(edge['source'], edge['target']) for edge in graph['citation_edges']}))
        full = build_citation_graph(snapshot(work(1, [2, 2, 3]), work(2, [3]), work(3, [1])))
        self.assertEqual({(edge['source'], edge['target']) for edge in full['citation_edges']},
                         {('W2', 'W1'), ('W3', 'W1'), ('W3', 'W2'), ('W1', 'W3')})

    def test_shared_reference_connects_papers_without_a_direct_citation(self):
        graph = build_citation_graph(snapshot(
            work(1, [100, 101]), work(2, [100, 102]), work(3, [103])))
        self.assertEqual(graph['source'], 'citation_similarity')
        self.assertEqual(graph['seed_id'], 'W1')
        self.assertEqual({node['id'] for node in graph['nodes']}, {'W1', 'W2'})
        edge = edge_between(graph, 'W1', 'W2')
        self.assertEqual(edge['shared_references'], 1)
        self.assertEqual(edge['co_citations'], 0)
        self.assertFalse(edge['direct_citation'])
        neighbor = next(node for node in graph['nodes'] if node['id'] == 'W2')
        self.assertEqual(neighbor['related_by']['shared_references'], 1)

    def test_cocitation_requires_an_observed_paper_referencing_both(self):
        graph = build_citation_graph(snapshot(
            work(1, [100]), work(2, [200]), work(3, [1, 2])))
        edge = edge_between(graph, 'W1', 'W2')
        self.assertEqual(edge['co_citations'], 1)
        self.assertEqual(edge['shared_references'], 0)
        self.assertFalse(edge['direct_citation'])
        without_witness = build_citation_graph(snapshot(work(1, [100]), work(2, [200])))
        self.assertEqual(without_witness['edges'], [])

    def test_direct_citation_is_preserved_in_both_seed_directions(self):
        for seed_references, other_references in [([2], []), ([], [1])]:
            with self.subTest(seed_references=seed_references):
                graph = build_citation_graph(snapshot(
                    work(1, seed_references), work(2, other_references)))
                edge = edge_between(graph, 'W1', 'W2')
                self.assertTrue(edge['direct_citation'])
                self.assertEqual(edge['shared_references'], 0)
                self.assertEqual(edge['co_citations'], 0)
                self.assertGreater(edge['weight'], 0)

    def test_duplicate_reference_ids_cannot_inflate_the_evidence(self):
        graph = build_citation_graph(snapshot(
            work(1, [100, 100, 101]), work(2, [100, 100]), work(3, [1, 1, 2, 2])))
        edge = edge_between(graph, 'W1', 'W2')
        self.assertEqual(edge['shared_references'], 1)
        self.assertEqual(edge['co_citations'], 1)

    def test_matching_titles_and_provider_related_ids_are_not_citation_evidence(self):
        title = 'An identical PAM4 silicon photonic receiver title'
        graph = build_citation_graph(snapshot(
            work(1, title=title, related_works=['W2']),
            work(2, title=title, related_works=['W1'])))
        self.assertEqual([node['id'] for node in graph['nodes']], ['W1'])
        self.assertEqual(graph['edges'], [])

    def test_prior_and_derivative_lists_count_actual_citation_relationships(self):
        graph = build_citation_graph(snapshot(
            work(1, [10]), work(2, [10]), work(10, year=2020),
            work(20, [1, 2], year=2025)))
        prior = {item['id']: item for item in graph['prior_works']}
        derivative = {item['id']: item for item in graph['derivative_works']}
        self.assertIn('W10', prior)
        self.assertEqual(prior['W10']['relation_count'], 2)
        self.assertIn('W20', derivative)
        self.assertEqual(derivative['W20']['relation_count'], 2)

    def test_public_graph_omits_account_flags_and_preserves_source_input(self):
        evidence = snapshot(
            work(1, [100], is_favorite=True, account_id=9, pdf_local_path='private.pdf'),
            work(2, [100], is_favorite=False, owner='somebody', local_account_id=10))
        original = deepcopy(evidence)
        graph = build_citation_graph(evidence)
        for key in ('nodes', 'prior_works', 'derivative_works', 'lineage_nodes'):
            for item in graph[key]:
                for private_key in ('is_favorite', 'account_id', 'local_account_id',
                                    'pdf_local_path', 'owner'):
                    self.assertNotIn(private_key, item)
        self.assertEqual(evidence, original)


class CitationMapShapeTests(unittest.TestCase):
    def test_deterministic_bounded_map_remains_connected_to_the_seed(self):
        seed = work(1, [100, 101])
        neighbors = [work(number, [100, 101]) for number in range(2, 24)]
        graph = build_citation_graph(snapshot(seed, *neighbors), limit=5)
        reversed_graph = build_citation_graph(snapshot(*reversed(neighbors), seed), limit=5)
        self.assertEqual(graph, reversed_graph)
        self.assertLessEqual(len(graph['nodes']), 5)
        self.assertIn('W1', {node['id'] for node in graph['nodes']})
        reached = {'W1'}
        while True:
            new = {endpoint for edge in graph['edges']
                   if edge['source'] in reached or edge['target'] in reached
                   for endpoint in (edge['source'], edge['target'])}
            if new <= reached:
                break
            reached |= new
        self.assertEqual(reached, {node['id'] for node in graph['nodes']})
        self.assertTrue(all(0 < edge['weight'] <= 1 for edge in graph['edges']))
        self.assertEqual(len({frozenset((edge['source'], edge['target']))
                              for edge in graph['edges']}), len(graph['edges']))

    def test_missing_optional_metadata_and_empty_bibliography_are_usable(self):
        graph = build_citation_graph(snapshot(work(
            1, title=None, authors=None, doi=None, year=None, source_name=None,
            reference_ids=None, citation_count=None, abstract=None)))
        self.assertEqual(len(graph['nodes']), 1)
        self.assertEqual(graph['nodes'][0]['id'], 'W1')
        self.assertEqual(graph['edges'], [])
        self.assertEqual(graph['nodes'][0]['similarity'], 1)
        self.assertEqual(graph['nodes'][0]['reference_count'], 0)

    def test_one_node_limit_retains_seed_without_dangling_edges(self):
        graph = build_citation_graph(snapshot(work(1, [2]), work(2)), limit=1)
        self.assertEqual([node['id'] for node in graph['nodes']], ['W1'])
        self.assertEqual(graph['edges'], [])

    def test_unrelated_component_is_excluded_even_when_it_has_dense_evidence(self):
        graph = build_citation_graph(snapshot(
            work(1, [100]), work(2, [100]),
            work(3, [200, 201]), work(4, [200, 201]), work(5, [3, 4])))
        self.assertEqual({node['id'] for node in graph['nodes']}, {'W1', 'W2'})
        self.assertNotIn('W3', {edge['source'] for edge in graph['edges']})


if __name__ == '__main__':
    unittest.main()
