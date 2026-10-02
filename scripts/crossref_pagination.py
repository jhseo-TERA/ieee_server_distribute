"""Reject incomplete/stalled API pagination instead of accepting partial data."""


class PageAudit:
    def __init__(self):
        self.expected = None
        self.scanned = 0
        self.dois = set()

    def next_cursor(self, data, page_size):
        if self.expected is None:
            self.expected = int(data.get('total-results', 0))
        items = data.get('items', [])
        keys = {item['DOI'].lower() for item in items if item.get('DOI')}
        if items and keys and not (keys - self.dois):
            raise RuntimeError('Crossref returned a repeated page without progress')
        self.dois.update(keys)
        self.scanned += len(items)
        if len(items) < page_size:
            if self.scanned < self.expected:
                raise RuntimeError(f'Incomplete Crossref response: {self.scanned}/{self.expected}')
            return None
        cursor = data.get('next-cursor')
        if not cursor:
            raise RuntimeError('Crossref full page omitted the next cursor')
        return cursor
