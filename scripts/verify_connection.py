"""Read-only acceptance of the deployed map; session credentials stay in memory."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from sqlalchemy import text
from web.app import app, engine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--public', action='store_true', help='Verify the existing archive hostname')
    parser.add_argument('--citation', action='store_true', help='Also verify the public citation map for the sample DOI')
    parser.add_argument('--saved-only', action='store_true', help='Never fetch remote citation metadata during acceptance')
    args = parser.parse_args()
    base = 'https://papers.example.com' if args.public else 'http://127.0.0.1:5001'
    assert requests.get(base + '/health', timeout=5).json()['ok']
    assert requests.get(base + '/api/connection/search', timeout=5).status_code == 401
    with engine.connect() as conn:
        account = dict(conn.execute(text(
            'SELECT id,username,role,session_version FROM accounts '
            'WHERE is_active=1 AND must_change_password=0 ORDER BY id LIMIT 1'
        )).mappings().one())
    signed = app.session_interface.get_signing_serializer(app).dumps(dict(
        authenticated=True, account_id=account['id'], username=account['username'],
        role=account['role'], session_version=account['session_version'],
        csrf_token='connection-read-only-verification',
    ))
    with requests.Session() as client:
        client.cookies.set(app.config['SESSION_COOKIE_NAME'], signed)
        page = client.get(base + '/connection', timeout=10, allow_redirects=False)
        assert page.status_code == 200, page.status_code
        assert 'connection.js' in page.text
        for control in ('graph-paper-list', 'tab-timeline', 'tab-list', 'filter-panel', 'timeline-map', 'detail-citation-meta', 'lineage-depth', 'lineage-direction', 'lineage-use-selected', 'citation-empty', 'use-local-empty'):
            assert 'id="' + control + '"' in page.text, control
        for asset in ('/static/connection.js', '/static/connection.css', '/static/connection-lineage.js'):
            assert client.get(base + asset, timeout=5).status_code == 200
        search = client.get(base + '/api/connection/search', params={'q': 'PAM4'}, timeout=15)
        search.raise_for_status()
        papers = search.json()['papers']
        assert papers, 'Expected a local PAM4 paper'
        assert all('citation_count' in paper and 'citation_source' in paper and 'citation_updated_at' in paper
                   for paper in papers)
        start = time.monotonic()
        response = client.get(base + '/api/connection/graph', params={
            'article_number': papers[0]['article_number'], 'limit': 30,
        }, timeout=30)
        response.raise_for_status()
        graph = response.json()
        graph_seconds = round(time.monotonic() - start, 2)
        ids = {node['id'] for node in graph['nodes']}
        assert graph['source'] == 'local_similarity'
        assert graph['seed_id'] in ids
        assert len(ids) == len(graph['nodes']) <= 30
        assert all(edge['source'] in ids and edge['target'] in ids for edge in graph['edges'])
        assert client.get(base + '/api/connection/graph', timeout=5).status_code == 400
        assert client.get(base + '/api/connection/graph', params={
            'article_number': 'connection-verification-missing-paper',
        }, timeout=10).status_code == 404
        citation_result = None
        if args.citation:
            start_citation = time.monotonic()
            citation_params = {'mode': 'citation', 'doi': '10.1109/JSSC.2024.3435378', 'limit': 30}
            if args.saved_only:
                citation_params['cached_only'] = 1
            deadline = start_citation + 120
            while True:
                citation = client.get(base + '/api/connection/graph', params=citation_params, timeout=20)
                if citation.status_code != 202 or time.monotonic() >= deadline:
                    break
                time.sleep(2)
            assert citation.status_code == 200, (citation.status_code, citation.json())
            citation = citation.json()
            citation_ids = {node['id'] for node in citation['nodes']}
            assert citation['source'] == 'citation_similarity'
            assert citation['seed_id'] == 'W4401507857'
            assert 1 < len(citation_ids) <= 30
            assert all(edge['source'] in citation_ids and edge['target'] in citation_ids
                       and (edge['shared_references'] or edge['co_citations'] or edge['direct_citation'])
                       for edge in citation['edges'])
            seed = next(node for node in citation['nodes'] if node['is_seed'])
            assert seed['article_number'] == '10633778' and seed['in_library']
            assert isinstance(seed['citation_count'], int) and seed['citation_count'] >= 0
            assert seed['citation_source'] == 'openalex' and seed['citation_updated_at']
            assert citation['prior_works'] and citation['derivative_works']
            assert citation['meta']['citation_edge_direction'] == 'cited_to_citing'
            assert citation['citation_edges'], 'Expected direct citations for this sample'
            lineage_ids = {node['id'] for node in citation['lineage_nodes']}
            assert citation_ids <= lineage_ids and len(lineage_ids) <= 180
            assert all(edge['source'] in lineage_ids and edge['target'] in lineage_ids
                       for edge in citation['lineage_edges'])
            assert all(edge['source'] in citation_ids and edge['target'] in citation_ids
                       and edge['source'] == edge['cited'] and edge['target'] == edge['citing']
                       for edge in citation['citation_edges'])
            citation_result = {'seed_id': citation['seed_id'], 'nodes': len(citation_ids),
                               'edges': len(citation['edges']), 'library_count': citation['meta']['library_count'],
                               'directed_citations': len(citation['citation_edges']),
                               'seed_citations': seed['citation_count'],
                               'citation_source': seed['citation_source'],
                               'prior_works': len(citation['prior_works']),
                               'derivative_works': len(citation['derivative_works']),
                               'candidate_count': citation['meta']['candidate_count'],
                               'saved_lineage_nodes': len(lineage_ids), 'saved_lineage_edges': len(citation['lineage_edges']),
                               'seconds': round(time.monotonic() - start_citation, 2)}
        print(json.dumps({
            'base': base, 'page': 'ok', 'authentication': 'enforced', 'search_results': len(papers),
            'seed_id': graph['seed_id'], 'nodes': len(ids), 'edges': len(graph['edges']),
            'graph_seconds': graph_seconds, 'meta': graph['meta'],
            'citation': citation_result,
        }, ensure_ascii=False))


if __name__ == '__main__':
    main()
