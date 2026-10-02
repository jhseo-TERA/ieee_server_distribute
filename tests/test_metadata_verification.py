"""Final audit verification without database connections or publisher calls."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from scripts.metadata_enrichment import INSERT_FIELDS, write_json
from scripts import verify_metadata_enrichment as verification


class MetadataVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run = Path(self.temporary.name) / 'run'
        self.output = Path(self.temporary.name) / 'verification'
        self.paper = {
            'article_number': '123', 'title': 'Original title', 'authors': 'A. Author',
            'year': '2007', 'source_name': 'VLSI-Circuits', 'source_type': 'conference',
            'source_system': 'ieee', 'issue': None,
            'url': 'https://ieeexplore.ieee.org/document/123/', 'doi': '10.1109/vlsic.2008.123',
        }

    def audit(self, relative, record, order):
        path = self.run / relative
        write_json(path, record)
        timestamp = 1_700_000_000_000_000_000 + order * 1_000_000_000
        os.utime(path, ns=(timestamp, timestamp))
        return path

    def insert(self, relative='insert/changes.json', order=1):
        return self.audit(relative, {
            'status': 'committed', 'new_records': [self.paper],
            'inserted': [{'id': 7, 'article_number': '123'}],
        }, order)

    def patch_audit(self, relative, fields, order, status='committed', identity=7):
        return self.audit(relative, {'status': status, 'changes': [{
            'id': identity, 'article_number': '123', 'new': fields,
        }]}, order)

    def test_insert_verification_checks_every_inserted_field_and_id(self):
        self.insert()
        audited = verification.load_audited_expectations(self.run)
        expected = audited['expected']['123']
        self.assertEqual(set(expected), set(INSERT_FIELDS) | {'id'})
        self.assertEqual(verification.compare_expected_rows(audited['expected'], {'123': expected}), [])
        for field in expected:
            with self.subTest(field=field):
                changed = {**expected, field: 'different value'}
                self.assertEqual(verification.compare_expected_rows(audited['expected'], {'123': changed}),
                                 [{'article_number': '123', 'fields': [field]}])

    def test_later_patches_override_only_their_fields_in_commit_write_order(self):
        self.insert('z_initial/changes.json', order=1)
        self.patch_audit('b_repair/batch_00000.json', {'title': 'Recovered title', 'year': '2008'}, 2)
        self.patch_audit('a_later/batch_00000.json', {'year': '2009'}, 3)
        audited = verification.load_audited_expectations(self.run)
        expected = audited['expected']['123']
        self.assertEqual(expected, {**self.paper, 'id': 7, 'title': 'Recovered title', 'year': '2009'})
        self.assertEqual(audited['inserted_keys'], {'123'})
        self.assertEqual(audited['patched_keys'], {'123'})
        self.assertEqual(audited['patched_field_updates'], 3)
        self.assertEqual([row['kind'] for row in audited['ordered_audits']], ['insert', 'patch', 'patch'])

    def test_existing_row_patch_is_verified_without_any_insert_audit(self):
        self.patch_audit('doi_fill/batch_00000.json', {'doi': self.paper['doi']}, 1)
        audited = verification.load_audited_expectations(self.run)
        self.assertEqual(audited['inserted_keys'], set())
        actual = {'123': {'id': 7, 'article_number': '123', 'doi': None}}
        self.assertEqual(verification.compare_expected_rows(audited['expected'], actual),
                         [{'article_number': '123', 'fields': ['doi']}])
        self.assertEqual(verification.compare_expected_rows(audited['expected'], {}),
                         [{'article_number': '123', 'fields': ['row_missing']}])

    def test_uncommitted_patches_and_inserts_are_reported_and_never_replayed(self):
        self.insert()
        pending = self.patch_audit('pending/batch_00000.json', {'title': 'Uncommitted'}, 2, status='prepared')
        failed = self.audit('failed/changes.json', {
            'status': 'rolled_back', 'new_records': [], 'inserted': [],
        }, 3)
        audited = verification.load_audited_expectations(self.run)
        self.assertEqual(audited['expected']['123']['title'], 'Original title')
        self.assertEqual(audited['uncommitted_patch_audits'], [str(pending)])
        self.assertEqual(audited['uncommitted_insert_audits'], [str(failed)])
        self.assertEqual(audited['patched_keys'], set())

    def test_audit_identity_changes_are_rejected(self):
        self.insert()
        self.patch_audit('repair/batch_00000.json', {'year': '2008'}, 2, identity=999)
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            verification.load_audited_expectations(self.run)

    def test_final_verifier_fails_for_regressed_doi_on_a_preexisting_row(self):
        self.patch_audit('doi_fill/batch_00000.json', {'doi': self.paper['doi']}, 1)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        # Five summary queries, then the audited-row query. No real DB is used.
        cursor.fetchall.side_effect = [[], [], [], [], [], [{**self.paper, 'id': 7, 'doi': None}]]
        cursor.fetchone.return_value = {'doi': 1, 'authors': 0, 'title': 0}
        intact = {'missing_original_ids': [], 'changed_protected_ids': [], 'changed_references': []}
        with patch('pymysql.connect') as connect, \
             patch.object(verification, 'capture', return_value={'paper_rows': 1}), \
             patch.object(verification, 'compare', return_value=intact), \
             patch('sys.argv', ['verify', '--run', str(self.run), '--output', str(self.output)]), \
             redirect_stdout(io.StringIO()) as output:
            connect.return_value.__enter__.return_value = connection
            with self.assertRaises(AssertionError):
                verification.main()
        result = json.loads((self.output / 'verification.json').read_text(encoding='utf-8'))
        self.assertEqual(result['patched_audit_rows'], 1)
        self.assertEqual(result['metadata_mismatch_count'], 1)
        self.assertEqual(result['patched_mismatches'], ['123'])
        self.assertEqual(result['metadata_mismatch_samples'], [{'article_number': '123', 'fields': ['doi']}])
        self.assertNotIn(self.paper['title'], output.getvalue())
        self.assertNotIn(self.paper['doi'], output.getvalue())


if __name__ == '__main__':
    unittest.main()
