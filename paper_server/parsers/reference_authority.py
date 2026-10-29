"""Resolve ambiguous surnames using pinned, reviewed publisher evidence only.

No lookup or surname guessing. Keep the source name and previous candidate.
"""
from __future__ import annotations

import hashlib
import json
import re

from paper_server.paths import ROOT
from paper_server.parsers.reference_normalization import compact, source_key

VERSION = 'reference-authority-v1'
REGISTRY = ROOT / 'config/reference_authorities.json'


def load_authorities():
    return json.loads(REGISTRY.read_text(encoding='utf-8'))['records']


def apply_author_resolution(row, records):
    title = row.get('title_display', row.get('title')) or ''
    title = re.sub(r'\s*\[(?:J|C)\]\s*[.;,]?\s*$', '', title)
    first_name = compact(row.get('raw', '').split(',', 1)[0])
    before = row.get('author_resolution', {}).get('before', row.get('author'))
    matches = []
    for record in records:
        given, family = record['first_author']['given'], record['first_author']['family']
        if (not given or not family or source_key(given) == source_key(family)
                or source_key(title) != source_key(record['title'])
                or row.get('year') != record['year']
                or source_key(record['journal']) not in source_key(row.get('raw'))
                or not re.search(r'\b' + re.escape(str(record['year'])) + r'\b', row.get('raw', ''))
                or first_name.casefold() not in (f'{family} {given}'.casefold(), f'{given} {family}'.casefold())
                or source_key(before) != source_key(given)):
            continue
        # An explicit conflicting DOI prevents this identity match. Never add
        # an absent DOI just because the author evidence contains one.
        doi = str(row.get('doi') or '').casefold().removeprefix('https://doi.org/').removeprefix('doi:')
        if doi and doi != record['doi'].casefold():
            continue
        matches.append(record)
    if len(matches) != 1:
        return row
    record = matches[0]
    resolution = {'version': VERSION, 'authority_id': record['id'],
        'authority_sha256': hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
        'raw_sha256': hashlib.sha256(row['raw'].encode()).hexdigest(),
        'before': before, 'after': record['first_author']['family'], 'source_first_author': first_name,
        'source_url': record['source_url'], 'evidence': record['evidence']}
    return {**row, 'author': record['first_author']['family'], 'author_resolution': resolution,
            'first_author_name':record['evidence']['full_name']}


def verified_author_resolution(row, records):
    if not row.get('author_resolution'):
        return False
    expected = apply_author_resolution(row, records)
    # A stale or mismatched record must not validate itself by returning row.
    return (expected is not row and row.get('author') == expected['author']
            and ('first_author_name' not in row or row['first_author_name']==expected.get('first_author_name'))
            and row['author_resolution'] == expected['author_resolution'])
