"""Validated, shared publication registry for every metadata entry point."""
import json
import re
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parents[1] / 'config' / 'metadata_sources.json'


def load_registry(path=REGISTRY_PATH):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('version') != 1 or not 0 < data.get('minimum_count_ratio', 0) <= 1:
        raise ValueError('Invalid metadata registry version/count ratio')
    names = set()
    for item in data['sources']:
        name = item['name']
        if name.casefold() in names or not re.fullmatch(r'[\w-]+', name):
            raise ValueError(f'Duplicate or unsafe source name: {name}')
        names.add(name.casefold())
        if item['type'] not in {'journal', 'conference'} or not isinstance(item['enabled'], bool):
            raise ValueError(f'Invalid type/enabled: {name}')
        if item['start_year'] > item.get('end_year', 9999):
            raise ValueError(f'Reversed publication years: {name}')
        if item['collector'] not in {'ieee_crossref', 'optica_crossref', 'nature_crossref', 'ofc_crossref', 'manual'}:
            raise ValueError(f'Unknown collector: {name}')
        if item['enabled'] and item['pipeline'] not in {'ieee', 'optica', 'nature'}:
            raise ValueError(f'Missing weekly owner: {name}')
        if item['type'] == 'journal' and not re.fullmatch(r'\d{4}-\d{3}[\dX]', item.get('issn', '')):
            raise ValueError(f'Missing/invalid ISSN: {name}')
        if item['collector'] == 'ieee_crossref' and item['type'] == 'conference':
            if any(not re.fullmatch(r'10\.\d{4,9}', prefix) for prefix in item.get('doi_prefixes', ['10.1109'])):
                raise ValueError(f'Invalid DOI prefix: {name}')
            for key in ('query', 'title_pattern', 'doi_pattern'):
                if not item.get(key):
                    raise ValueError(f'Missing {key}: {name}')
            re.compile(item['title_pattern'])
            re.compile(item['doi_pattern'])
    return data


def sources(collector=None, pipeline=None, kind=None, enabled=True):
    return [item for item in load_registry()['sources']
            if (collector is None or item['collector'] == collector)
            and (pipeline is None or item['pipeline'] == pipeline)
            and (kind is None or item['type'] == kind)
            and (enabled is None or item['enabled'] == enabled)]


def source(name):
    return next(item for item in sources(enabled=None) if item['name'].casefold() == name.casefold())


def select_sources(items, names):
    if names is None:
        return list(items)
    wanted = {name.strip().casefold() for name in names.split(',') if name.strip()}
    known = {item['name'].casefold() for item in items}
    if not wanted or wanted - known:
        raise ValueError(f'Empty or unknown source selection: {names}')
    return [item for item in items if item['name'].casefold() in wanted]


def publication_range(item, start_year, end_year):
    high = min(max(start_year, end_year), item.get('end_year', 9999))
    low = max(min(start_year, end_year), item['start_year'])
    return (high, low) if high >= low else None
