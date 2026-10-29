"""Read-only citation graph API and per-account library hydration checks."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from paper_server.accounts import account_scope
from paper_server.citation_graph import CitationGraphService
from paper_server.connection import create_connection_blueprint
from tests.test_citation_graph import snapshot, work


class CitationApiTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / 'tmp'
        temporary_root.mkdir(exist_ok=True)
        self.directory = self.enterContext(TemporaryDirectory(dir=temporary_root))
        self.provider = Mock()
        self.service = CitationGraphService(self.provider, self.directory)
        self.addCleanup(self.service.close)
        self.service._store('10.1234/paper-1', snapshot(
            work(1, [100]), work(2, [100]), work(3, [100])))
        self.engine = create_engine('sqlite://')
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as connection:
            connection.execute(text('CREATE TABLE papers(id INTEGER PRIMARY KEY,'
                'article_number TEXT,title TEXT,authors TEXT,year TEXT,source_name TEXT,'
                'source_system TEXT,doi TEXT,pdf_available INTEGER,is_favorite INTEGER,'
                'citation_count INTEGER,citation_source TEXT,citation_updated_at TEXT)'))
            connection.execute(text('CREATE TABLE accounts(id INTEGER,role TEXT,is_active INTEGER,'
                                    'favorites_source_account_id INTEGER)'))
            connection.execute(text("INSERT INTO accounts VALUES (1,'member',1,NULL),"
                                    "(2,'member',1,NULL),(3,'viewer',1,1)"))
            connection.execute(text('CREATE TABLE account_favorites(account_id INTEGER,paper_id INTEGER)'))
            connection.execute(text('INSERT INTO account_favorites VALUES (1,1),(2,2)'))
            connection.execute(text('CREATE TABLE paper_current_abstracts(paper_id INTEGER,'
                                    'abstract_text TEXT,is_current INTEGER)'))
            connection.execute(text("INSERT INTO paper_current_abstracts VALUES (1,'Saved local abstract.',1)"))
            connection.execute(text('INSERT INTO papers (id,article_number,title,authors,year,source_name,'
                'source_system,doi,pdf_available,is_favorite) VALUES '
                "(1,'101','Local title 1','Local author','2024','JSSC','ieee','10.1234/PAPER-1',1,0),"
                "(2,'102','Local title 2','Local author','2024','JSSC','ieee','10.1234/paper-2',0,0),"
                "(3,'103','No DOI','Local author','2024','JSSC','ieee',NULL,0,0)"))
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.app.register_blueprint(create_connection_blueprint(self.engine, self.service))
        self.client = self.app.test_client()

    def request_graph(self, account=1, **parameters):
        with account_scope(account):
            return self.client.get('/api/connection/graph', query_string={
                'mode': 'citation', 'article_number': '101', **parameters})

    def test_cached_public_graph_is_hydrated_fresh_for_each_account(self):
        for account, expected in ((1, {'W1'}), (2, {'W2'}), (3, {'W1'}), (1, {'W1'})):
            with self.subTest(account=account):
                response = self.request_graph(account, account_id=1)
                self.assertEqual(response.status_code, 200)
                nodes = response.json['nodes']
                self.assertEqual({node['id'] for node in nodes if node['is_favorite']}, expected)
                self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        cached = self.service._path('10.1234/paper-1').read_text(encoding='utf-8')
        self.assertNotIn('is_favorite', cached)
        self.assertNotIn('article_number', cached)
        self.assertNotIn('Saved local abstract.', cached)
        self.provider.fetch.assert_not_called()

    def test_full_saved_lineage_is_hydrated_for_each_account_even_when_map_limit_is_one(self):
        for account, expected in ((1, {'W1'}), (2, {'W2'}), (3, {'W1'})):
            response = self.request_graph(account, limit=1, cached_only=1)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json['nodes']), 1)
            lineage = response.json['lineage_nodes']
            self.assertEqual(len(lineage), 3)
            self.assertEqual({row['id'] for row in lineage if row['is_favorite']}, expected)
            local = next(row for row in lineage if row['id'] == 'W1')
            self.assertEqual(local['article_number'], '101')
            self.assertEqual(local['abstract'], 'Saved local abstract.')
        self.provider.fetch.assert_not_called()

    def test_saved_only_missing_snapshot_never_starts_a_provider_job(self):
        with account_scope(1):
            response = self.client.get('/api/connection/graph', query_string={
                'mode': 'citation', 'doi': '10.1234/not-in-cache', 'cached_only': 1})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.service.jobs, {})
        self.provider.fetch.assert_not_called()

    def test_local_doi_match_supplies_pdf_and_abstract_without_creating_external_rows(self):
        before = self._row_count()
        response = self.request_graph()
        self.assertEqual(response.status_code, 200)
        nodes = {node['id']: node for node in response.json['nodes']}
        self.assertEqual(nodes['W1']['article_number'], '101')
        self.assertTrue(nodes['W1']['in_library'])
        self.assertTrue(nodes['W1']['pdf_available'])
        self.assertEqual(nodes['W1']['title'], 'Local title 1')
        self.assertEqual(nodes['W1']['abstract'], 'Saved local abstract.')
        self.assertIsNone(nodes['W3']['article_number'])
        self.assertFalse(nodes['W3']['in_library'])
        self.assertFalse(nodes['W3']['pdf_available'])
        self.assertFalse(nodes['W3']['is_favorite'])
        self.assertEqual(response.json['meta']['library_count'], 2)
        self.assertEqual(before, self._row_count())

    def test_openalex_zero_preserves_its_provenance_and_only_unknown_counts_use_sql_fallback(self):
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE papers SET citation_count=999,citation_source='ieee',"
                              "citation_updated_at='2026-10-15T00:00:00Z' WHERE id=1"))
            conn.execute(text("UPDATE papers SET citation_count=17,citation_source='crossref',"
                              "citation_updated_at='2026-10-14T00:00:00Z' WHERE id=2"))
        self.service._store('10.1234/paper-1', snapshot(
            work(1, [100], citation_count=0), work(2, [100], citation_count=None),
            work(3, [100], citation_count=None)))
        nodes = {row['id']: row for row in self.request_graph().json['nodes']}
        self.assertEqual(nodes['W1']['citation_count'], 0)
        self.assertEqual(nodes['W1']['citation_source'], 'openalex')
        self.assertEqual(nodes['W1']['citation_updated_at'], '2026-10-11T12:00:00+00:00')
        self.assertEqual(nodes['W2']['citation_count'], 17)
        self.assertEqual(nodes['W2']['citation_source'], 'crossref')
        self.assertEqual(nodes['W2']['citation_updated_at'], '2026-10-14T00:00:00Z')
        self.assertIsNone(nodes['W3']['citation_count'])
        cached = self.service._path('10.1234/paper-1').read_text(encoding='utf-8')
        self.assertNotIn('crossref', cached)
        self.assertNotIn('999', cached)

    def _row_count(self):
        with self.engine.connect() as connection:
            return connection.execute(text('SELECT COUNT(*) FROM papers')).scalar_one()

    def test_external_doi_can_be_used_as_seed_without_local_article_number(self):
        with account_scope(1):
            response = self.client.get('/api/connection/graph', query_string={
                'mode': 'citation', 'doi': 'https://doi.org/10.1234/PAPER-1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['seed_id'], 'W1')

    def test_missing_local_paper_or_doi_does_not_call_provider(self):
        self.assertEqual(self.request_graph(article_number='unknown').status_code, 404)
        self.assertEqual(self.request_graph(article_number='103').status_code, 422)
        self.provider.fetch.assert_not_called()

    def test_invalid_doi_mode_limit_and_identifier_are_rejected_before_sql(self):
        cases = [
            {'doi': 'http://127.0.0.1/admin'},
            {'doi': '10.1234/paper-1\nsecret'},
            {'mode': 'unsupported'}, {'limit': '61'}, {'limit': '-1'}, {'cached_only': 'maybe'},
            {'article_number': '101\x00'}, {'article_number': ''},
        ]
        for parameters in cases:
            with self.subTest(parameters=parameters), patch.object(self.engine, 'connect') as connect:
                response = self.request_graph(**parameters)
                self.assertEqual(response.status_code, 400)
                connect.assert_not_called()
        self.provider.fetch.assert_not_called()

    def test_pending_result_and_rate_limit_have_retry_headers_and_no_sql_hydration(self):
        for status, body in ((202, {'status': 'building', 'retry_after': 2}),
                             (429, {'error': 'Budget reached.', 'retry_after': 60})):
            with self.subTest(status=status), patch.object(self.service, 'get', return_value=(body, status)), \
                    patch.object(self.engine, 'connect') as connect:
                with account_scope(1):
                    response = self.client.get('/api/connection/graph', query_string={
                        'mode': 'citation', 'doi': '10.1234/paper-1'})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers['Retry-After'], str(body['retry_after']))
                self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
                connect.assert_not_called()

    def test_database_failure_is_sanitized(self):
        with patch.object(self.engine, 'connect', side_effect=OperationalError(
                'SELECT private data', {}, RuntimeError('private-password'))):
            response = self.request_graph()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-password', json.dumps(response.json))
        self.assertNotIn('SELECT', json.dumps(response.json))

    def test_graph_requests_execute_only_read_queries(self):
        statements = []
        listener = lambda conn, cursor, statement, parameters, context, many: statements.append(statement)
        event.listen(self.engine, 'before_cursor_execute', listener)
        try:
            self.assertEqual(self.request_graph().status_code, 200)
        finally:
            event.remove(self.engine, 'before_cursor_execute', listener)
        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith('SELECT') for statement in statements))


if __name__ == '__main__':
    unittest.main()
