"""A reviewed publisher identity can override an agreed but wrong surname."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_server.parsers.reference_authority import (
    apply_author_resolution, load_authorities, verified_author_resolution,
)
from paper_server.parsers.reference_normalization import apply_title_normalization
from scripts import benchmark_reference_bulk as bulk
from scripts.audit_reference_bulk import audit


RAW = ('Chen Shuai, Li Hao, Shi Xiaobing. A low-power high-swing voltagemode transmitter[J]. '
       'Journal of Semiconductors, 2012, 33(4).')


def source_row():
    return {'number': 1, 'page': 4, 'raw': RAW,
            'title': 'A low-power high-swing voltagemode transmitter[J]',
            'author': 'Shuai', 'year': 2012, 'doi': None, 'journal': 'Journal of Semiconductors'}


class ReferenceAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.records = load_authorities()

    def test_resolves_only_surname_and_retains_previous_candidate_and_source(self):
        row = source_row()
        before = deepcopy(row)
        resolved = apply_author_resolution(row, self.records)
        self.assertEqual(resolved['author'], 'Chen')
        self.assertEqual(resolved['author_resolution']['before'], 'Shuai')
        self.assertTrue(verified_author_resolution(resolved, self.records))
        self.assertEqual({k: resolved[k] for k in row if k != 'author'}, {k: row[k] for k in row if k != 'author'})
        self.assertEqual(row, before)
        self.assertEqual(apply_author_resolution(resolved, self.records), resolved)

    def test_wrong_title_year_journal_full_name_or_conflicting_doi_cannot_match(self):
        for change in ({'title': 'A different transmitter'}, {'year': 2013}, {'author': 'Smith'},
                       {'raw': RAW.replace('Chen Shuai', 'Chen Wei')},
                       {'raw': RAW.replace('Journal of Semiconductors', 'Other Journal')},
                       {'raw': RAW.replace('2012', '2013')}, {'doi': '10.1234/other'}):
            with self.subTest(change=change):
                row = {**source_row(), **change}
                self.assertEqual(apply_author_resolution(row, self.records), row)

    def test_initials_ambiguous_records_and_already_correct_surname_stay_unchanged(self):
        for row, records in (({**source_row(), 'raw': RAW.replace('Chen Shuai', 'C. Shuai')}, self.records),
                             (source_row(), self.records * 2),
                             ({**source_row(), 'author': 'Chen'}, self.records),
                             (source_row(), [])):
            with self.subTest(row=row, record_count=len(records)):
                self.assertEqual(apply_author_resolution(row, records), row)

    def test_western_order_and_display_title_can_use_same_verified_identity(self):
        row = apply_title_normalization({**source_row(), 'raw': RAW.replace('Chen Shuai', 'Shuai Chen')})
        resolved = apply_author_resolution(row, self.records)
        self.assertEqual(resolved['author'], 'Chen')
        self.assertTrue(verified_author_resolution(resolved, self.records))

    def test_tampered_or_stale_evidence_cannot_waive_author_disagreement(self):
        resolved = apply_author_resolution(source_row(), self.records)
        for change in ({'source_url': 'https://example.invalid/'}, {'authority_sha256': '0' * 64},
                       {'before': 'Smith'}, {'after': 'Shuai'}, {'raw_sha256': '0' * 64}):
            row = {**resolved, 'author_resolution': {**resolved['author_resolution'], **change}}
            self.assertFalse(verified_author_resolution(row, self.records))
        self.assertFalse(verified_author_resolution({**resolved, 'author': 'Shuai'}, self.records))
        changed = deepcopy(self.records)
        changed[0]['verified_on'] = '2099-01-01'
        self.assertFalse(verified_author_resolution(resolved, changed))

    def test_bulk_pipeline_overrides_wrong_parser_agreement_with_publisher_evidence(self):
        row = source_row()
        paper = {'id': 'test', 'pdf': 'test.pdf'}
        entry = {k: row[k] for k in ('number', 'page', 'raw')}
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            bulk.save(out / 'papers/test/prepared.json', {'entries': [entry], 'status': 'ready',
                'entry_source': 'mineru', 'warnings': [], 'errors': []})
            with patch.object(bulk, 'grobid_raw', return_value=([row], 0)), \
                 patch.object(bulk, 'llm_batch', return_value=([{**row, 'author': 'Chen'}], [], [])):
                bulk.enrich_group([paper], out, None, None, {'model': 'test'})
            result = bulk.read(out / 'papers/test/result.json')
            self.assertEqual(result['rows'][0]['author'], 'Chen')
            self.assertEqual(result['rows'][0]['author_resolution']['before'], 'Shuai')
            self.assertEqual(result['grobid'][0]['author'], 'Shuai')
            self.assertEqual(result['rows'][0]['raw'], RAW)
            self.assertEqual(result['rows'][0]['title_display'], 'A low-power high-swing voltagemode transmitter')

    def test_audit_requires_verified_evidence_for_agreed_author_override(self):
        old = source_row()
        resolved = apply_author_resolution(old, self.records)
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            bulk.save(out / 'manifest.json', {'papers': [{'id': 'test', 'pdf': 'test.pdf'}]})
            bulk.save(out / 'papers/test/prepared.json', {'entries': [old], 'status': 'ready', 'scan': {'page_count': 4}})
            for row, expected in ((resolved, True), ({**resolved, 'author_resolution': {}}, False)):
                bulk.save(out / 'papers/test/result.json', {'rows': [row], 'regex': [old], 'grobid': [old]})
                result = audit(out)
                flags = [f for r in result['role_review_candidates'] for f in r['flags']]
                self.assertEqual('agreed_author_changed_or_removed' not in flags, expected)

    def test_registry_changes_do_not_invalidate_inference_caches(self):
        old = {'model_digest': 'same', 'author_resolution_registry': 'old', 'author_resolution_code': 'old'}
        new = {**old, 'author_resolution_registry': 'new', 'author_resolution_code': 'new'}
        self.assertEqual(bulk.llm_cache_signature(old), bulk.llm_cache_signature(new))
        self.assertEqual(bulk.extraction_cache_signature(old), bulk.extraction_cache_signature(new))


if __name__ == '__main__':
    unittest.main()
