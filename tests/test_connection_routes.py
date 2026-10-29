"""Integration checks for the map's existing login and account boundary."""
import unittest
from unittest.mock import patch

from web.app import app


class ConnectionRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = patch.dict(app.config, TESTING=True, ACCOUNT_AUTH_ENABLED=False)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.client = app.test_client()

    def test_anonymous_page_redirects_and_apis_require_login(self):
        response = self.client.get('/connection')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login?next=/connection', response.headers['Location'])
        for path in ('/api/connection/search', '/api/connection/graph?article_number=123'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json['error'], 'authentication required')

    def test_viewer_can_open_map_with_existing_security_headers(self):
        with self.client.session_transaction() as session:
            session.update(authenticated=True, username='viewer', role='viewer')
        response = self.client.get('/connection')
        self.assertEqual(response.status_code, 200)
        self.assertIn('SQL'.encode(), response.data)
        self.assertIn(b'connection.js', response.data)
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_existing_navigation_exposes_connection(self):
        with self.client.session_transaction() as session:
            session.update(authenticated=True, username='viewer', role='viewer')
        for path in ('/', '/serdes'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'href="/connection"', response.data)


if __name__ == '__main__':
    unittest.main()
