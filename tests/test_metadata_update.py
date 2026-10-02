import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from scripts import import_excel_to_db as importer
from scripts import run_metadata_update as runner
from scripts.crossref_pagination import PageAudit
from scripts.fetch_ieee_conference_crossref import item_to_row, fetch_year, title_matches, doi_matches
from scripts.metadata_health import BrowserRun, capture_failure, verify_ieee_session
from scripts.metadata_sources import load_registry, sources, source, publication_range, select_sources
from scripts.verify_crossref_import import verify_report


def paper(name='JSSC', key='12345', year='2025'):
    return {'Journal': name, 'Year': year, 'Title': 'Verified paper', 'Authors': 'A. Author',
            'URL': f'https://ieeexplore.ieee.org/document/{key}/', 'DOI': f'10.1109/jssc.2025.{key}'}


class RegistryTests(unittest.TestCase):
    def test_each_enabled_source_has_exactly_one_weekly_owner(self):
        registry = load_registry()
        items = [item for item in registry['sources'] if item['enabled']]
        self.assertEqual(len(items), len({item['name'] for item in items}))
        self.assertEqual(source('ICTA')['pipeline'], 'ieee')
        self.assertEqual(source('OJSSC')['pipeline'], 'ieee')
        self.assertEqual(source('JLT')['system'], 'optica')
        self.assertEqual(len(sources(collector='optica_crossref')), 5)

    def test_historical_names_are_not_expected_to_publish_current_papers(self):
        for name in ('ESSCIRC', 'VLSI-Circuits', 'MWCL'):
            self.assertIsNone(publication_range(source(name), 2026, 2025))
        self.assertEqual(publication_range(source('ESSERC'), 2026, 2023), (2026, 2024))

    def test_selection_rejects_typos_and_empty_selection(self):
        for names in ('', ',', 'JSSC,typo'):
            with self.assertRaises(ValueError):
                select_sources(sources(pipeline='ieee'), names)
        self.assertEqual(select_sources(sources(), 'jssc')[0]['name'], 'JSSC')

    def test_registry_rejects_duplicate_names_and_reversed_years(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sources.json'
            for mutation in ('duplicate', 'years'):
                data = load_registry()
                if mutation == 'duplicate':
                    data['sources'].append(dict(data['sources'][0]))
                else:
                    data['sources'][0]['end_year'] = 1900
                path.write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_registry(path)


class PaginationTests(unittest.TestCase):
    def test_premature_empty_page_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            PageAudit().next_cursor({'total-results': 10, 'items': []}, 1000)

    def test_cursor_may_be_reused_when_papers_advance(self):
        audit = PageAudit()
        for key in ('one', 'two'):
            self.assertEqual(audit.next_cursor({'total-results': 2, 'items': [{'DOI': key}], 'next-cursor': 'same'}, 1), 'same')
        self.assertIsNone(audit.next_cursor({'total-results': 2, 'items': []}, 1))

    def test_repeated_page_is_failure(self):
        audit = PageAudit()
        page = {'total-results': 2, 'items': [{'DOI': 'one'}], 'next-cursor': 'same'}
        audit.next_cursor(page, 1)
        with self.assertRaisesRegex(RuntimeError, 'repeated page'):
            audit.next_cursor(page, 1)


class ConferenceTests(unittest.TestCase):
    def test_doi_and_title_and_year_all_must_match(self):
        config = source('ISSCC')
        item = {'DOI': '10.1109/isscc49661.2025.10904819', 'title': ['A circuit'],
                'container-title': ['2025 IEEE International Solid-State Circuits Conference (ISSCC)'],
                'author': [{'given': 'A', 'family': 'Author'}],
                'resource': {'primary': {'URL': 'https://ieeexplore.ieee.org/document/10904819/'}}}
        self.assertEqual(item_to_row(item, config, 2025)['Conference'], 'ISSCC')
        self.assertIsNone(item_to_row(item, config, 2026))
        item['container-title'] = ['2025 Some Other Conference']
        self.assertIsNone(item_to_row(item, config, 2025))

    def test_combined_vlsi_stays_under_existing_tech_name(self):
        title = '2026 IEEE/JSAP Symposium on VLSI Technology and Circuits (VLSI Technology and Circuits)'
        self.assertTrue(title_matches(title, source('VLSI-Tech'), 2026))
        self.assertFalse(title_matches(title, source('VLSI-Circuits'), 2026))
        self.assertTrue(doi_matches('10.1109/vlsitechnologyandcir65830.2026.11577550', source('VLSI-Tech'), 2026))
        self.assertTrue(doi_matches('10.23919/vlsitechnologyandcir65189.2025.11074819', source('VLSI-Tech'), 2025))

    def test_discovery_is_not_treated_as_the_paper_list(self):
        client = MagicMock()
        title = '2025 IEEE International Solid-State Circuits Conference (ISSCC)'
        client.get_message.side_effect = [
            {'items': [{'DOI': '10.1109/isscc.2025.1', 'container-title': [title]}]},
            {'items': [], 'total-results': 0},
        ]
        with self.assertRaisesRegex(RuntimeError, 'zero records'):
            fetch_year(source('ISSCC'), 2025, client)
        self.assertIn('container-title:' + title, client.get_message.call_args.args[1]['filter'])

    def test_comma_title_uses_bounded_token_search_with_strict_membership(self):
        config = source('ICTA')
        item = {'DOI': '10.1109/icta68203.2025.11329834', 'title': ['A circuit'],
                'container-title': ['2025 IEEE International Conference on Integrated Circuits, Technologies and Applications (ICTA)'],
                'author': [{'given': 'A', 'family': 'Author'}],
                'resource': {'primary': {'URL': 'https://ieeexplore.ieee.org/document/11329834/'}}}
        unrelated = dict(item, DOI='10.1109/ictai.2025.999')
        client = MagicMock()
        client.get_message.side_effect = [{'items': [item]}, {'items': [item, unrelated], 'total-results': 2}]
        rows, _ = fetch_year(config, 2025, client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(client.get_message.call_args.args[1]['query.container-title'], 'Integrated')

    def test_textual_year_is_separate_from_container_query(self):
        client = MagicMock()
        client.get_message.return_value = {'items': []}
        fetch_year(source('VLSI-Tech'), 2026, client)
        self.assertEqual(client.get_message.call_args.args[1]['query.container-title'], 'VLSI')
        self.assertEqual(client.get_message.call_args.args[1]['query.bibliographic'], '2026')
        self.assertNotIn('pub-date', client.get_message.call_args.args[1]['filter'])


class ImportTests(unittest.TestCase):
    def test_opg_doi_only_url_gets_a_stable_safe_key(self):
        key, system = importer.extract_key('https://opg.optica.org/oe/abstract.cfm?doi=10.1364/OE.540104')
        self.assertEqual(system, 'optica')
        self.assertEqual(key, importer.optica_doi_key('10.1364/oe.540104'))
        self.assertLessEqual(len(key), 80)
        self.assertIsNone(importer.extract_key('https://evil.example/abstract.cfm?doi=10.1364/OE.540104')[0])

    def test_later_uri_keeps_existing_early_access_doi_key(self):
        conn = MagicMock()
        doi = '10.1364/oe.540104'
        key = importer.optica_doi_key(doi)
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(doi, key)]
        row = ('oe-34-12-3456', 'Title', 'Author', '2026', 'OE', 'journal', 'optica', None, 'url', None, 0, doi)
        self.assertEqual(importer.reconcile_doi_keys([row], conn)[0][0], key)

    def test_doi_only_url_reuses_existing_publisher_uri_key(self):
        conn = MagicMock()
        doi = '10.1364/oe.540104'
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [(doi, 'oe-34-12-3456')]
        row = (importer.optica_doi_key(doi), 'Title', 'Author', '2026', 'OE', 'journal', 'optica', None, 'url', None, 0, doi)
        self.assertEqual(importer.reconcile_doi_keys([row], conn)[0][0], 'oe-34-12-3456')

    def test_verifier_rejects_modified_run_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'JSSC.xlsx'
            path.write_bytes(b'changed after validation')
            report = Path(directory) / 'report.json'
            report.write_text(json.dumps({'status': 'completed', 'sources': [
                {'name': 'JSSC', 'status': 'validated', 'file': str(path), 'sha256': 'different'}]}))
            with self.assertRaisesRegex(ValueError, 'changed after validation'):
                verify_report(MagicMock(), report)

    def test_explicit_file_selection_cannot_import_an_older_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / 'old.xlsx', Path(directory) / 'new.xlsx'
            pd.DataFrame([paper(key='111')]).to_excel(old, index=False)
            pd.DataFrame([paper(key='222')]).to_excel(new, index=False)
            with patch.object(importer, 'META_DIR', directory), contextlib.redirect_stdout(io.StringIO()):
                rows = importer.load_rows([new], strict=True)
            self.assertEqual([row[0] for row in rows], ['222'])
            self.assertEqual(rows[0][-1], '10.1109/jssc.2025.222')

    def test_explicit_corrupt_or_empty_workbook_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty.xlsx'
            pd.DataFrame().to_excel(path, index=False)
            with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                importer.load_rows([path], strict=True)

    def test_import_rolls_back_when_post_write_keys_are_missing(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = []
        row = ('missing',) + (None,) * 11
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            importer.import_rows([row], conn)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()


class CountTests(unittest.TestCase):
    def test_zero_source_and_lost_previous_year_fail(self):
        for counts, previous, db in [({'2025': 0}, {}, {}),
                                     ({'2025': 0, '2026': 100}, {'2025': 100}, {}),
                                     ({'2025': 20}, {}, {'2025': 100})]:
            with self.assertRaises(ValueError):
                runner.validate_counts(counts, previous, db, .7)

    def test_unpublished_future_edition_does_not_fail_a_healthy_source(self):
        runner.validate_counts({'2025': 100, '2026': 0}, {'2025': 100}, {'2025': 100}, .7)

    def test_history_ignores_failed_runs_other_windows_and_latest_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            base = {'status': 'completed', 'range': [2026, 2025], 'sources': [
                {'name': 'JSSC', 'status': 'validated', 'config_hash': 'abc', 'counts': {'2025': 100}}]}
            Path(directory, '1.json').write_text(json.dumps(base))
            base['sources'][0]['counts']['2025'] = 1000
            Path(directory, 'latest_ieee.json').write_text(json.dumps(base))
            base['status'] = 'failed'
            Path(directory, '2.json').write_text(json.dumps(base))
            with patch.object(runner, 'REPORT_DIR', Path(directory)):
                self.assertEqual(runner.historical_counts('JSSC', 'abc', 2026, 2025)['2025'], 100)


class BrowserTests(unittest.TestCase):
    def test_zero_rows_and_partial_failures_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            for rows in ([], [paper()]):
                run = BrowserRun('test', ['JSSC', 'TCAS-I'])
                run.directory = Path(directory)
                with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
                    run.finish(rows)

    def test_login_timeout_is_explicit(self):
        wait = MagicMock()
        wait.until.side_effect = TimeoutError()
        with self.assertRaisesRegex(RuntimeError, 'login not completed'):
            verify_ieee_session(MagicMock(), wait)

    def test_diagnostic_url_drops_query_secrets(self):
        driver = MagicMock()
        driver.current_url = 'https://example.org/login?token=do-not-store'
        driver.title = 'Login'
        driver.find_elements.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            path = capture_failure(driver, directory, 'JSSC', 'login', TimeoutError())
            result = Path(path).read_text()
        self.assertNotIn('do-not-store', result)
        self.assertIn('https://example.org/login', result)


class RunTests(unittest.TestCase):
    def run_mocked(self, directory, names, collector, collect_only=False):
        args = argparse.Namespace(pipeline='ieee', sources=names, start_year=2026, end_year=2025,
                                  full=False, collect_only=collect_only)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
        with patch('pymysql.connect', return_value=conn), \
             patch.object(runner, 'coverage_audit', return_value={}), \
             patch.object(runner, 'database_counts', return_value={}), \
             patch.object(runner, 'backup_existing', return_value=0), \
             patch.object(runner, 'collect_source', side_effect=collector), \
             patch.object(runner, 'REPORT_DIR', Path(directory) / 'reports'), \
             patch.object(runner, 'RUN_DIR', Path(directory) / 'runs'), \
             patch.object(importer, 'import_rows') as write, contextlib.redirect_stdout(io.StringIO()):
            code = runner.run(args)
            report = json.loads((Path(directory) / 'reports' / 'latest_ieee.json').read_text())
            return code, report, write

    def test_partial_failure_blocks_entire_import(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report, write = self.run_mocked(directory, 'JSSC,TCAS-I', [([paper()], []), TimeoutError('offline')])
        self.assertEqual(code, 1)
        self.assertEqual(report['failed_sources'], ['TCAS-I'])
        self.assertEqual(report['failure_count'], 1)
        write.assert_not_called()

    def test_success_imports_only_this_runs_validated_records(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report, write = self.run_mocked(directory, 'JSSC', [([paper()], [])])
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['imported_count'], 1)
        self.assertEqual(write.call_args.args[0][0][0], '12345')

    def test_collect_only_never_writes_database(self):
        with tempfile.TemporaryDirectory() as directory:
            code, report, write = self.run_mocked(directory, 'JSSC', [([paper()], [])], True)
        self.assertEqual(code, 0)
        self.assertEqual(report['status'], 'collected')
        write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
