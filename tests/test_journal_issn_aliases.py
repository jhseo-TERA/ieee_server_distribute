import contextlib
import io
import unittest
from unittest.mock import MagicMock, patch

from scripts.fetch_ieee_crossref import JOURNALS, fetch_journal, journal_container_matches


def paper(doi='10.1109/tcsi.2005.862068', key='1629236', title='Verified circuit paper',
          container='IEEE Transactions on Circuits and Systems I: Regular Papers'):
    return {'DOI': doi, 'title': [title], 'author': [{'given': 'A', 'family': 'Author'}],
            'published-print': {'date-parts': [[2006, 5]]}, 'volume': '53', 'issue': '5',
            'page': '100-105', 'container-title': [container],
            'resource': {'primary': {'URL': f'https://ieeexplore.ieee.org/document/{key}/'}}}


class JournalAliasTests(unittest.TestCase):
    def collect(self, name, responses):
        client = MagicMock()
        client.get_message.side_effect = responses
        with contextlib.redirect_stdout(io.StringIO()):
            rows = fetch_journal(name, 2006, 2006, client)
        return rows, client

    def test_verified_tcas_and_mwcl_aliases_are_registered(self):
        self.assertEqual(JOURNALS['TCAS-I']['issn_aliases'], ['1057-7122'])
        self.assertEqual(JOURNALS['TCAS-II']['issn_aliases'], ['1057-7130'])
        self.assertEqual(JOURNALS['MWCL']['issn_aliases'], ['1531-1309'])

    def test_missing_early_paper_is_found_in_legacy_issn(self):
        rows, client = self.collect('TCAS-I', [
            {'items': [], 'total-results': 0}, {'items': [paper()], 'total-results': 1},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual([call.args[0] for call in client.get_message.call_args_list],
                         ['/journals/1549-8328/works', '/journals/1057-7122/works'])
        self.assertEqual(client.get_message.call_args_list[0].args[1]['filter'],
                         client.get_message.call_args_list[1].args[1]['filter'])

    def test_same_doi_from_both_identifiers_is_not_duplicated(self):
        page = {'items': [paper()], 'total-results': 1}
        rows, _ = self.collect('TCAS-I', [page, page])
        self.assertEqual(len(rows), 1)

    def test_alias_does_not_import_predecessor_or_other_journal(self):
        rows, _ = self.collect('TCAS-I', [
            {'items': [], 'total-results': 0},
            {'items': [paper(container='IEEE Transactions on Circuits and Systems I: Fundamental Theory and Applications'),
                       paper(doi='10.1109/tcsii.2006.1', container='IEEE Transactions on Circuits and Systems II: Express Briefs')],
             'total-results': 2},
        ])
        self.assertEqual(rows, [])

    def test_title_normalization_accepts_punctuation_but_not_other_parts(self):
        item = paper(container='IEEE Transactions on Circuits and Systems—I: Regular Papers')
        self.assertTrue(journal_container_matches(item, JOURNALS['TCAS-I']))
        self.assertFalse(journal_container_matches(item, JOURNALS['TCAS-II']))

    def test_conflicting_same_doi_across_identifiers_is_not_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, 'Conflicting metadata'):
            self.collect('TCAS-I', [
                {'items': [paper()], 'total-results': 1},
                {'items': [paper(title='Different paper')], 'total-results': 1},
            ])

    def test_incomplete_alias_response_still_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            self.collect('TCAS-I', [{'items': [], 'total-results': 0}, {'items': [], 'total-results': 5}])

    def test_alias_without_journal_identity_allowlist_is_rejected(self):
        with patch.dict(JOURNALS, {'TCAS-I': {'issn': '1549-8328', 'issn_aliases': ['1057-7122']}}):
            with self.assertRaisesRegex(ValueError, 'title allowlist'):
                self.collect('TCAS-I', [])


if __name__ == '__main__':
    unittest.main()
