"""Keep URL-only bibliography slots as logged resources, without fetching them."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import urlsplit

VERSION = 'reference-url-policy-v1'
SOURCE_KEYS = ('number', 'page', 'raw', 'source_raw', 'bbox', 'block',
               'boundary_style', 'boundary_version', 'extractor', 'recovered_adjacent_text')
BIBLIOGRAPHIC_FIELDS = ('title', 'author', 'year', 'doi', 'journal')


def standalone_url(raw):
    """Match an entire URL entry, never a paper citation that contains a URL."""
    match = re.fullmatch(
        r'\s*(?:\[Online\]\.?\s*)?(?:Available\s*:\s*)?'
        r'((?:https?://|www\.)[^\s<>"“”]+)\s*', str(raw or ''), re.I)
    if not match:
        return None
    value = match[1].rstrip('.,;')
    try:
        host = urlsplit(value if '://' in value else 'https://' + value).hostname
    except ValueError:
        return None
    return value if host and '.' in host and not host.startswith('.') else None


def logged_url_row(entry):
    """Discard stale inferred fields while preserving the printed slot and source."""
    url = standalone_url(entry.get('raw'))
    if not url:
        raise ValueError('Only a standalone URL can be logged without metadata')
    result = {k: entry[k] for k in SOURCE_KEYS if k in entry}
    result.update({k: None for k in BIBLIOGRAPHIC_FIELDS})
    result.update(reference_kind='url_only', processing_status='logged_only', url=url,
                  url_policy_version=VERSION, parser='url-only-resource', warnings=[],
                  needs_llm=False, needs_review=False, needs_review_before_normalization=False,
                  llm_status='not_needed')
    return result


def write_url_log(result, directory):
    """Record skipped metadata, separately from the count of bibliography slots."""
    events = []
    for row in result['rows']:
        url = standalone_url(row.get('raw'))
        if url:
            events.append({'event': 'url_only_metadata_skipped', 'number': row['number'],
                'page': row['page'], 'raw': row['raw'], 'url': url,
                'status': 'logged_only', 'url_fetch_attempted': False})
    result['reference_counts'] = {'total_entries': len(result['rows']),
        'bibliographic_entries': len(result['rows']) - len(events), 'url_only_logged': len(events)}
    if events:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'url-only-references.json'
        source = result.get('source', {})
        payload = {'version': VERSION,
            'source': {k: source[k] for k in ('id', 'pdf', 'sha256') if k in source}, 'events': events}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        result['url_reference_log'] = path.name
    return result
