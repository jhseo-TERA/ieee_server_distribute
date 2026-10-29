"""Offline checks for read-only, account-scoped SQL research maps."""
import unittest
from unittest.mock import patch

from flask import Flask, jsonify
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from paper_server.accounts import account_scope
from paper_server.connection import (
    MAX_CANDIDATES, ConnectionRepository, build_graph, create_connection_blueprint,
)


def paper(article, title, **extra):
    return {'db_id': int(article), 'article_number': str(article), 'title': title,
            'authors': None, 'year': '2025', 'source_name': 'JSSC',
            'source_system': 'ieee', 'doi': None, 'pdf_available': False,
            'is_favorite': False, **extra}


class SimilarityTests(unittest.TestCase):
    def test_close_papers_rank_ahead_of_partial_overlap_and_unrelated_are_omitted(self):
        seed = paper(1, 'PAM4 receiver with decision feedback equalizer')
        rows = [paper(2, 'Adaptive PAM4 receiver DFE'),
                paper(3, 'PAM4 receiver with optical microring resonator laser'),
                paper(4, 'Brillouin acoustic waveguide scattering')]
        graph = build_graph(seed, rows, {})
        self.assertEqual([node['id'] for node in graph['nodes']][:2], ['1', '2'])
        self.assertNotIn('4', [node['id'] for node in graph['nodes']])
        self.assertIn('dfe', graph['nodes'][1]['shared_terms'])
        self.assertTrue(all(0 < edge['weight'] <= 1 for edge in graph['edges']))

    def test_broad_words_do_not_connect_unrelated_papers(self):
        graph = build_graph(paper(1, 'CMOS optical receiver'),
                            [paper(2, 'Silicon optical receiver')], {})
        self.assertEqual(len(graph['nodes']), 1)
        self.assertEqual(graph['edges'], [])

    def test_process_and_efficiency_alone_do_not_connect_rf_to_wireline(self):
        seed = paper(1, 'A NRZ/PAM4 SST TX in 5nm FinFET CMOS with 3-tap FFE '
                        'and 0.7pJ/b efficiency at 100 Gb/s PAM4')
        graph = build_graph(seed, [
            paper(2, 'n-FinFET and p-FinFET 48GHz High Efficiency Power Amplifiers '
                     'in 14nm CMOS Technology'),
            paper(3, 'A 112 Gb/s PAM-4 56 Gb/s NRZ Reconfigurable Transmitter '
                     'With Three-Tap FFE in 10-nm FinFET')], {})
        self.assertEqual({node['id'] for node in graph['nodes']}, {'1', '3'})

    def test_device_research_still_matches_through_specific_physics(self):
        graph = build_graph(paper(1, 'FinFET random dopant fluctuation analysis'),
                            [paper(2, 'Random dopant fluctuation impact in FinFET technology')], {})
        self.assertEqual(len(graph['nodes']), 2)
        self.assertIn('dopant', graph['nodes'][1]['shared_terms'])

    def test_only_related_displayed_pairs_get_edges(self):
        graph = build_graph(paper(1, 'PAM4 receiver DFE equalizer calibration'), [
            paper(2, 'PAM4 receiver DFE'), paper(3, 'Equalizer calibration amplifier')], {})
        self.assertEqual({node['id'] for node in graph['nodes']}, {'1', '2', '3'})
        self.assertEqual({frozenset((edge['source'], edge['target'])) for edge in graph['edges']},
                         {frozenset(('1', '2')), frozenset(('1', '3'))})

    def test_saved_abstract_concepts_can_reveal_a_grounded_connection(self):
        seed = paper(1, 'Broadband optical interface')
        other = paper(2, 'Inductive peaking optical receiver')
        self.assertEqual(len(build_graph(seed, [other], {})['nodes']), 1)
        graph = build_graph(seed, [other], {1: 'This circuit employs inductive peaking.'})
        self.assertEqual(len(graph['nodes']), 2)
        self.assertIn('peaking', graph['nodes'][1]['shared_terms'])
        self.assertEqual(graph['meta']['abstract_count'], 1)

    def test_negated_abstract_method_does_not_create_connection(self):
        graph = build_graph(paper(1, 'Broadband optical interface'),
                            [paper(2, 'Inductive peaking optical receiver')],
                            {1: 'This circuit operates without inductive peaking.'})
        self.assertEqual(len(graph['nodes']), 1)

    def test_missing_metadata_seed_only_and_limit_one_are_valid(self):
        seed = paper(1, None, authors=None, year=None, source_name=None)
        graph = build_graph(seed, [], {}, limit=1)
        self.assertEqual(graph['nodes'][0]['title'], '')
        self.assertIsNone(graph['nodes'][0]['authors'])
        self.assertEqual(graph['nodes'][0]['similarity'], 1)
        self.assertTrue(graph['nodes'][0]['is_seed'])
        self.assertEqual(graph['edges'], [])
        graph = build_graph(paper(1, 'PAM4 receiver DFE'),
                            [paper(2, 'PAM4 receiver DFE')], {}, limit=1)
        self.assertEqual(len(graph['nodes']), 1)

    def test_deterministic_ties_and_candidate_bound(self):
        seed = paper(1, 'PAM4 receiver DFE')
        candidates = [paper(i, 'PAM4 receiver DFE') for i in range(2, 20)]
        left = build_graph(seed, candidates, {}, limit=5)
        right = build_graph(seed, list(reversed(candidates)), {}, limit=5)
        self.assertEqual(left, right)
        huge = build_graph(seed, [paper(i, 'PAM4 receiver DFE')
                                 for i in range(2, MAX_CANDIDATES + 20)], {}, limit=2)
        self.assertEqual(huge['meta']['candidate_count'], MAX_CANDIDATES)


class ConnectionApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.execute(text('CREATE TABLE papers(id INTEGER PRIMARY KEY,article_number TEXT UNIQUE,'
                              'title TEXT,authors TEXT,year TEXT,source_name TEXT,source_system TEXT,'
                              'doi TEXT,pdf_available INT,is_favorite INT,citation_count INT,'
                              'citation_source TEXT,citation_updated_at TEXT)'))
            conn.execute(text('CREATE TABLE accounts(id INT,role TEXT,is_active INT,'
                              'favorites_source_account_id INT)'))
            conn.execute(text("INSERT INTO accounts VALUES (1,'member',1,NULL),(2,'member',1,NULL),"
                              "(3,'viewer',1,1)"))
            conn.execute(text('CREATE TABLE account_favorites(account_id INT,paper_id INT)'))
            conn.execute(text('INSERT INTO account_favorites VALUES (1,1),(1,2),(2,3)'))
            conn.execute(text('CREATE TABLE paper_current_abstracts(paper_id INT,abstract_text TEXT,'
                              'is_current INT)'))
            records = [paper(1, 'PAM4 receiver DFE'), paper(2, 'Adaptive PAM4 receiver DFE'),
                       paper(3, 'Brillouin acoustic waveguide scattering'),
                       paper(4, 'Efficiency of a 50% receiver', doi='10.1/test',
                             authors='An Author', year='2026')]
            conn.execute(text('INSERT INTO papers (id,article_number,title,authors,year,source_name,'
                              'source_system,doi,pdf_available,is_favorite) VALUES (:db_id,:article_number,:title,:authors,'
                              ':year,:source_name,:source_system,:doi,:pdf_available,:is_favorite)'), records)
        self.app = Flask(__name__)
        self.app.register_blueprint(create_connection_blueprint(self.engine))
        self.client = self.app.test_client()

    def test_search_returns_metadata_and_bounded_results(self):
        response = self.client.get('/api/connection/search', query_string={'q': 'PAM4', 'limit': 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['papers']), 1)
        self.assertTrue(response.json['has_more'])
        self.assertIsInstance(response.json['papers'][0]['article_number'], str)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_saved_citation_counts_are_returned_in_search_and_local_graph_for_nonfavorites(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE papers SET citation_count=0,citation_source='ieee',"
                              "citation_updated_at='2026-10-15T00:00:00Z' WHERE id=1"))
            conn.execute(text("UPDATE papers SET citation_count=27,citation_source='crossref' WHERE id=2"))
        with account_scope(2):
            search = self.client.get('/api/connection/search?q=PAM4').json['papers']
            graph = self.client.get('/api/connection/graph?article_number=1').json['nodes']
        for rows in (search, graph):
            indexed = {row['article_number']: row for row in rows}
            self.assertEqual(indexed['1']['citation_count'], 0)
            self.assertEqual(indexed['1']['citation_source'], 'ieee')
            self.assertEqual(indexed['1']['citation_updated_at'], '2026-10-15T00:00:00Z')
            self.assertEqual(indexed['2']['citation_count'], 27)
            self.assertFalse(indexed['1']['is_favorite'])

    def test_unqueried_citation_count_remains_unknown(self):
        row = self.client.get('/api/connection/search?q=PAM4').json['papers'][0]
        self.assertIsNone(row['citation_count'])
        self.assertIsNone(row['citation_source'])
        self.assertIsNone(row['citation_updated_at'])

    def test_empty_search_uses_current_accounts_favorites(self):
        with account_scope(1):
            response = self.client.get('/api/connection/search')
        self.assertEqual({row['article_number'] for row in response.json['papers'][:2]}, {'1', '2'})
        with account_scope(2):
            response = self.client.get('/api/connection/search')
        self.assertEqual(response.json['papers'][0]['article_number'], '3')

    def test_graph_favorites_do_not_leak_between_accounts_and_viewer_scope_is_respected(self):
        for account, expected in [(1, True), (2, False), (3, True), (2, False)]:
            with self.subTest(account=account), account_scope(account):
                response = self.client.get('/api/connection/graph?article_number=1')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['nodes'][0]['is_favorite'], expected)
                self.assertEqual(response.json['nodes'][1]['is_favorite'], expected)

    def test_sql_injection_is_only_data_and_wildcards_are_literal(self):
        response = self.client.get('/api/connection/search', query_string={'q': "' OR 1=1 --"})
        self.assertEqual(response.json['papers'], [])
        response = self.client.get('/api/connection/graph',
                                   query_string={'article_number': "1' OR 1=1 --"})
        self.assertEqual(response.status_code, 404)
        response = self.client.get('/api/connection/search', query_string={'q': '%'})
        self.assertEqual([row['article_number'] for row in response.json['papers']], ['4'])
        self.assertEqual(self.client.get('/api/connection/search?q=10.1/test').json['papers'][0]['article_number'], '4')

    def test_validation_happens_before_querying_database(self):
        invalid = ['/api/connection/graph', '/api/connection/graph?article_number=1&limit=0',
                   '/api/connection/graph?article_number=1&limit=61',
                   '/api/connection/search?limit=-1', '/api/connection/search?limit=20.5',
                   '/api/connection/search?limit=abc', '/api/connection/search?limit=51',
                   '/api/connection/search?q=' + 'x' * 201,
                   '/api/connection/graph?article_number=' + 'x' * 256,
                   '/api/connection/search?q=bad%00value']
        with patch.object(self.engine, 'connect', side_effect=AssertionError('must not query')):
            for path in invalid:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('error', response.json)

    def test_imported_identifiers_longer_than_legacy_schema_are_supported(self):
        article = 'imported-' + 'x' * 100
        with self.engine.begin() as conn:
            conn.execute(text('UPDATE papers SET article_number=:article WHERE id=1'),
                         {'article': article})
        response = self.client.get('/api/connection/graph', query_string={'article_number': article})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['seed_id'], article)

    def test_read_only_uses_existing_current_abstracts(self):
        with self.engine.begin() as conn:
            conn.execute(text("INSERT INTO paper_current_abstracts VALUES "
                              "(1,'A PAM4 receiver with DFE.',1),(2,'Old abstract.',0)"))
            conn.execute(text('PRAGMA query_only=ON'))
        response = self.client.get('/api/connection/graph?article_number=1')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['nodes'][0]['abstract_available'])
        self.assertFalse(response.json['nodes'][1]['abstract_available'])
        self.assertEqual(response.json['meta']['abstract_count'], 1)
        self.assertEqual(self.client.get('/api/connection/search').status_code, 200)

    def test_missing_optional_abstract_view_keeps_title_graph_available(self):
        with self.engine.begin() as conn:
            conn.execute(text('DROP TABLE paper_current_abstracts'))
        response = self.client.get('/api/connection/graph?article_number=1')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json['meta']['abstracts_supported'])
        self.assertEqual(len(response.json['nodes']), 2)

    def test_database_errors_return_safe_json(self):
        with patch.object(ConnectionRepository, 'graph', side_effect=OperationalError(
                'secret SQL', {}, RuntimeError('secret host/password'))):
            response = self.client.get('/api/connection/graph?article_number=1')
        self.assertEqual(response.status_code, 503)
        self.assertIn('error', response.json)
        self.assertNotIn('secret', response.get_data(as_text=True))

    def test_app_auth_runs_before_blueprint_endpoints(self):
        @self.app.before_request
        def deny():
            return jsonify(error='authentication required'), 401
        with patch.object(ConnectionRepository, 'search', side_effect=AssertionError('auth bypassed')):
            response = self.client.get('/api/connection/search')
        self.assertEqual(response.status_code, 401)

    def test_queries_are_select_only_and_candidate_fetch_is_bounded(self):
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append((statement, parameters))
        event.listen(self.engine, 'before_cursor_execute', capture)
        self.addCleanup(event.remove, self.engine, 'before_cursor_execute', capture)
        self.assertEqual(self.client.get('/api/connection/graph?article_number=1').status_code, 200)
        self.assertTrue(all(sql.lstrip().upper().startswith('SELECT') for sql, _ in statements))
        self.assertTrue(any('LIMIT' in sql and MAX_CANDIDATES in params for sql, params in statements))


if __name__ == '__main__':
    unittest.main()
