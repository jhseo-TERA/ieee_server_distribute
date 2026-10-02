"""Opt-in integration check using a connection-local TEMPORARY papers table."""
import os
import unittest

from scripts.import_excel_to_db import DB, import_rows


@unittest.skipUnless(os.getenv('METADATA_MYSQL_TESTS') == '1', 'Opt-in local MySQL test')
class MetadataMysqlTests(unittest.TestCase):
    def test_upsert_preserves_user_and_pdf_state_and_saves_doi(self):
        import pymysql
        with pymysql.connect(**DB) as conn:
            with conn.cursor() as cur:
                # MySQL shadows the persistent table only on this connection;
                # it removes the temporary table when the connection closes.
                cur.execute('CREATE TEMPORARY TABLE papers ('
                            'article_number VARCHAR(80) PRIMARY KEY, title TEXT, authors TEXT, '
                            'year VARCHAR(10), source_name VARCHAR(50), source_type VARCHAR(10), '
                            'source_system VARCHAR(10), issue VARCHAR(120), url VARCHAR(400), '
                            'pdf_local_path VARCHAR(400), pdf_available BOOLEAN DEFAULT 0, '
                            'is_favorite BOOLEAN DEFAULT 0, doi VARCHAR(255)) ENGINE=InnoDB')
                cur.execute('INSERT INTO papers (article_number,title,authors,source_name,source_type,source_system,'
                            'pdf_local_path,pdf_available,is_favorite) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                            ('metadata-test', 'Before', 'Original author', 'JSSC', 'journal', 'ieee',
                             'custom/location.pdf', 1, 1))
            conn.commit()
            import_rows([('metadata-test', 'After', None, '2026', 'JSSC', 'journal', 'ieee',
                          'Vol. 61', 'https://ieeexplore.ieee.org/document/123/', None, 0,
                          '10.1109/jssc.2026.123')], conn)
            with conn.cursor() as cur:
                cur.execute('SELECT title,authors,pdf_local_path,pdf_available,is_favorite,doi FROM papers')
                self.assertEqual(cur.fetchone(), ('After', 'Original author', 'custom/location.pdf', 1, 1,
                                                 '10.1109/jssc.2026.123'))


if __name__ == '__main__':
    unittest.main()
