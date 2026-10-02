"""Loopback-only UI fixture: all favorites and API data are synthetic/in memory."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import render_template
from web.app import app

papers = [dict(article_number=f'test-{i}', title=f'TEST ONLY — {venue} receiver {i}',
               source_name=venue, year='2026', ai_score=90, ai_topic='I/O', ai_reason='UI fixture',
               recommendation_basis='ai', ai_favorite_id='fixture', ai_favorite_title='Test favorite',
               ai_evidence='receiver', ai_evidence_basis='title_only')
          for i, venue in enumerate(['JSSC', 'JSSC', 'OFC', 'ISSCC', 'SSCL'])]
favorites = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, body, content_type='application/json'):
        payload = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/':
            with app.test_request_context('/'):
                page = render_template('index.html', username='TEST ONLY — in-memory fixture',
                                       role='admin', csrf_token='fixture', can_edit_favorites=True)
            self.send(page, 'text/html')
        elif self.path.startswith('/api/recommendations?'):
            items = [p for p in papers if p['article_number'] not in favorites]
            groups = [dict(name=name, type='journal' if name in ('JSSC', 'SSCL') else 'conference')
                      for name in ['OFC', 'JSSC', 'ISSCC', 'SSCL'] if any(p['source_name']==name for p in items)]
            self.send(dict(items=items, groups=groups, fav_count=len(favorites)+10,
                           source_count=len(groups), per_source=20, total_limit=60, engine='hybrid',
                           ai={'status':'complete'}, refresh={'status':'ready'}))
        elif self.path == '/api/meta':
            self.send(dict(total=5, pdf_total=0, fav_total=len(favorites)+10, years=[], sources=[]))
        elif self.path.startswith('/api/papers?'):
            self.send(dict(items=[],total=0,page=1,pages=1,size=50))
        else:
            self.send(dict(items=[], available=False))

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/api/favorite':
            article = body['article_number']
            before = article in favorites
            if body['favorite']:
                favorites.add(article)
            else:
                favorites.discard(article)
            self.send(dict(ok=True, favorite_delta=int(article in favorites)-int(before)))
        else:
            self.send(dict(ok=False,error='Unsupported fixture action'))


if __name__ == '__main__':
    print('Synthetic UI fixture: http://127.0.0.1:5999 (no production writes)', flush=True)
    HTTPServer(('127.0.0.1',5999),Handler).serve_forever()
