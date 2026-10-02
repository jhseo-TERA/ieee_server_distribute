"""Guard against destructive metadata reconciliation and ambiguous DOI matches."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from scripts.metadata_enrichment import classify_changes, apply_proposals, import_new_records


class EnrichmentPlannerTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(id=1, article_number='123', doi=None, title='Original',
                        source_name='JSSC', pdf_available=1, pdf_local_path='custom/123.pdf', is_favorite=1)
        self.plan = dict(id=1, article_number='123', expected={'title': 'Original'},
                         new={'doi': '10.1109/JSSC.123'}, blank_doi_only=True, evidence='local exact key/title/source')

    def test_null_only_patch_and_protected_snapshot(self):
        accepted, skipped = classify_changes([self.plan], {'123': self.row})
        self.assertEqual(skipped, [])
        self.assertEqual(accepted[0]['new'], {'doi': '10.1109/jssc.123'})
        self.assertEqual(accepted[0]['protected']['is_favorite'], 1)
        self.assertEqual(accepted[0]['protected']['pdf_local_path'], 'custom/123.pdf')

    def test_stale_title_or_identity_never_overwritten(self):
        for changes in ({'title': 'Changed'}, {'id': 2}):
            accepted, skipped = classify_changes([self.plan], {'123': {**self.row, **changes}})
            self.assertEqual(accepted, [])
            self.assertIn(skipped[0]['reason'], {'stale_expected_fields', 'identity_changed'})

    def test_existing_doi_not_overwritten(self):
        accepted, skipped = classify_changes([self.plan], {'123': {**self.row, 'doi': '10.1109/other'}})
        self.assertEqual(accepted, [])
        self.assertEqual(skipped[0]['reason'], 'nonblank_doi_preserved')

    def test_duplicate_doi_other_document_is_held(self):
        other = {**self.row, 'id': 2, 'article_number': '456', 'doi': '10.1109/jssc.123'}
        accepted, skipped = classify_changes([self.plan], {'123': self.row, '456': other})
        self.assertEqual(accepted, [])
        self.assertEqual(skipped[0]['reason'], 'doi_collision')

    def test_rerun_is_noop(self):
        accepted, skipped = classify_changes([self.plan], {'123': {**self.row, 'doi': '10.1109/jssc.123'}})
        self.assertEqual(accepted, [])
        self.assertEqual(skipped[0]['reason'], 'already_applied')

    def test_protected_field_mutation_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan['new']['article_number'] = '456'
        with self.assertRaises(ValueError):
            classify_changes([plan], {'123': self.row})


@unittest.skipUnless(os.getenv('METADATA_MYSQL_TESTS') == '1', 'Opt-in local MySQL integration')
class EnrichmentMysqlTests(unittest.TestCase):
    def test_patch_insert_and_retry_preserve_original_state(self):
        import pymysql
        from scripts.import_excel_to_db import DB
        with pymysql.connect(**DB) as conn, tempfile.TemporaryDirectory() as directory:
            with conn.cursor() as cur:
                cur.execute('CREATE TEMPORARY TABLE papers ('
                            'id BIGINT AUTO_INCREMENT PRIMARY KEY, article_number VARCHAR(80) UNIQUE, '
                            'title TEXT,authors TEXT,year VARCHAR(10),source_name VARCHAR(50),'
                            'source_type VARCHAR(20),source_system VARCHAR(20),issue VARCHAR(120),'
                            'url VARCHAR(400),doi VARCHAR(255),pdf_local_path VARCHAR(400),'
                            'pdf_available BOOLEAN DEFAULT 0,is_favorite BOOLEAN DEFAULT 0) ENGINE=InnoDB')
                cur.execute("INSERT INTO papers (article_number,title,source_name,source_system,"
                            "pdf_local_path,pdf_available,is_favorite) VALUES ('123','Original','JSSC','ieee','custom.pdf',1,1)")
            conn.commit()
            plan = [dict(id=1,article_number='123',expected={'title':'Original','doi':None},
                         new={'doi':'10.1109/jssc.123'},blank_doi_only=True,evidence='fixture')]
            self.assertEqual(apply_proposals(conn,plan,Path(directory)/'first')['updated'],1)
            self.assertEqual(apply_proposals(conn,plan,Path(directory)/'retry')['updated'],0)
            row = dict(article_number='456',title='New paper',authors='A. Author',year='2011',
                       source_name='JSSC',source_type='journal',source_system='ieee',issue=None,
                       url='https://ieeexplore.ieee.org/document/456/',doi='10.1109/jssc.456')
            self.assertEqual(import_new_records(conn,[row],Path(directory)/'insert')['inserted'],1)
            self.assertEqual(import_new_records(conn,[row],Path(directory)/'insert_retry')['inserted'],0)
            with conn.cursor() as cur:
                cur.execute("SELECT id,pdf_local_path,pdf_available,is_favorite,doi FROM papers WHERE article_number='123'")
                self.assertEqual(cur.fetchone(),(1,'custom.pdf',1,1,'10.1109/jssc.123'))


if __name__ == '__main__':
    unittest.main()
