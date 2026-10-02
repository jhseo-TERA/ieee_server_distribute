"""Regression coverage for historical edition discovery and source boundaries."""
from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch

from scripts.fetch_ieee_conference_crossref import (
    UnresolvedEditionError, doi_matches, fetch_year, item_to_row, title_matches,
)
from scripts.metadata_sources import source


def item(doi, container, key='123'):
    return {'DOI': doi, 'container-title': [container], 'title': ['A circuit'],
            'author': [{'given': 'A', 'family': 'Author'}],
            'resource': {'primary': {'URL': f'https://ieeexplore.ieee.org/document/{key}/'}}}


class HistoricalConferenceTests(unittest.TestCase):
    def test_verified_2006_anchors_are_still_checked_after_discovery(self):
        for name, expected_doi in [('ISSCC', '10.1109/isscc.2006.1696052'),
                                   ('ESSCIRC', '10.1109/esscir.2006.307495')]:
            with self.subTest(name=name):
                config = source(name)
                title = config['edition_anchors']['2006'][0]['title']
                client = MagicMock()
                client.get_message.return_value = {'total-results': 1, 'items': [item(expected_doi, title)]}
                rows, report = fetch_year(config, 2006, client)
                self.assertEqual(rows[0]['DOI'], expected_doi)
                self.assertEqual(report['status'], 'verified')
                self.assertEqual(report['editions'][0]['anchor_dois_found'], [expected_doi])
                self.assertNotIn('query.container-title', client.get_message.call_args.args[1])

    def test_missing_anchor_fails_closed(self):
        config = source('ISSCC')
        title = config['edition_anchors']['2006'][0]['title']
        client = MagicMock()
        client.get_message.return_value = {'total-results': 1, 'items': [item('10.1109/isscc.2006.999', title)]}
        with self.assertRaisesRegex(RuntimeError, 'missing configured DOI anchors'):
            fetch_year(config, 2006, client)

    def test_vlsi_circuits_supports_both_prefixes_and_historical_suffixes(self):
        config = source('VLSI-Circuits')
        for doi, year in [('10.1109/VLSIC.2006.1705309', 2006),
                          ('10.23919/VLSICircuits52068.2021.9492426', 2021)]:
            self.assertIsNotNone(item_to_row(item(doi, f'{year} Symposium on VLSI Circuits'), config, year))

    def test_undated_comma_title_edition_uses_textual_year_without_date_filter(self):
        config = source('VLSI-Circuits')
        title = config['edition_anchors']['2006'][0]['title']
        undated = item('10.1109/vlsic.2006.1705374', title, '1705374')
        undated['issued'] = {'date-parts': [[None]]}
        client = MagicMock()
        client.get_message.return_value = {'total-results': 1, 'items': [undated]}
        rows, report = fetch_year(config, 2006, client)
        criteria = client.get_message.call_args.args[1]['filter']
        self.assertNotIn('pub-date', criteria)
        self.assertNotIn('isbn:', criteria)
        self.assertEqual(client.get_message.call_args.args[1]['query.container-title'], 'VLSI')
        self.assertEqual(client.get_message.call_args.args[1]['query.bibliographic'], '2006')
        self.assertEqual(rows[0]['Year'], '2006')
        self.assertEqual(report['editions'][0]['retrieval_method'], 'bounded_textual_year')

    def test_discovery_isbn_does_not_exclude_papers_missing_isbn(self):
        config = source('ICTA')
        title = '2025 IEEE International Conference on Integrated Circuits, Technologies and Applications (ICTA)'
        record = item('10.1109/icta68203.2025.11329834', title)
        record['ISBN'] = ['978-1-2345-6789-0']
        client = MagicMock()
        client.get_message.side_effect = [{'items': [record]}, {'total-results': 1, 'items': [record]}]
        _, report = fetch_year(config, 2025, client)
        self.assertEqual(report['editions'][0]['retrieval_method'], 'bounded_textual_year')
        self.assertEqual(report['editions'][0]['isbn'], ['9781234567890'])
        self.assertNotIn('isbn:', client.get_message.call_args.args[1]['filter'])

    def test_exact_container_fetch_retains_undated_records(self):
        config = source('ISSCC')
        title = '2011 IEEE International Solid-State Circuits Conference'
        record = item('10.1109/isscc.2011.123', title)
        client = MagicMock()
        client.get_message.side_effect = [{'items': [record]}, {'total-results': 1, 'items': [record]}]
        rows, _ = fetch_year(config, 2011, client)
        self.assertEqual(len(rows), 1)
        self.assertNotIn('pub-date', client.get_message.call_args.args[1]['filter'])

    def test_wholly_undated_edition_can_be_discovered_without_date_filter(self):
        config = dict(source('ISSCC'), query='ISSCC', discovery_queries=[])
        title = '2011 IEEE International Solid-State Circuits Conference'
        record = item('10.1109/isscc.2011.123', title)
        page = {'total-results': 1, 'items': [record]}
        client = MagicMock()
        client.get_message.side_effect = [page, page]
        rows, report = fetch_year(config, 2011, client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['discovery'][-1]['method'], 'bounded_discovery')
        self.assertNotIn('pub-date', client.get_message.call_args_list[0].args[1]['filter'])

    def test_other_solid_state_and_vlsi_events_are_rejected(self):
        for name in ['ISSCC', 'ESSCIRC']:
            config = source(name)
            self.assertFalse(title_matches('2006 IEEE Asian Solid-State Circuits Conference', config, 2006))
            self.assertFalse(doi_matches('10.1109/asscc.2006.357666', config, 2006))
        config = source('VLSI-Circuits')
        for title, doi in [
            ('2021 International Symposium on VLSI Technology, Systems and Applications (VLSI-TSA)',
             '10.1109/vlsi-tsa.2021.123'),
            ('2021 International Symposium on VLSI Design, Automation and Test (VLSI-DAT)',
             '10.1109/vlsi-dat.2021.123'),
            ('2021 Symposium on VLSI Technology', '10.23919/vlsit.2021.123'),
        ]:
            self.assertIsNone(item_to_row(item(doi, title), config, 2021))

    def test_discovery_advances_beyond_first_page(self):
        config = dict(source('ISSCC'), discovery_queries=['ISSCC'])
        target = item('10.1109/isscc.2010.123', '2010 IEEE International Solid-State Circuits Conference')
        unrelated = item('10.1109/asscc.2010.9', '2010 Asian Solid-State Circuits Conference')
        client = MagicMock()
        client.get_message.side_effect = [
            {'items': [unrelated], 'total-results': 2, 'next-cursor': 'second'},
            {'items': [target], 'total-results': 2, 'next-cursor': 'third'},
            {'items': [], 'total-results': 2},
            {'items': [target], 'total-results': 1},
        ]
        with patch('scripts.fetch_ieee_conference_crossref.DISCOVERY_PAGE_SIZE', 1):
            rows, report = fetch_year(config, 2010, client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['discovery'][0]['pages'], 3)
        self.assertEqual(client.get_message.call_args_list[1].args[1]['cursor'], 'second')
        self.assertTrue(report['editions'][0]['pagination_complete'])

    def test_stalled_discovery_is_not_accepted(self):
        unrelated = item('10.1109/asscc.2010.9', '2010 Asian Solid-State Circuits Conference')
        client = MagicMock()
        client.get_message.return_value = {'items': [unrelated], 'total-results': 3, 'next-cursor': 'same'}
        with patch('scripts.fetch_ieee_conference_crossref.DISCOVERY_PAGE_SIZE', 1):
            with self.assertRaisesRegex(RuntimeError, 'repeated page'):
                fetch_year(source('ISSCC'), 2010, client)

    def test_first_matching_page_does_not_hide_later_container_variants(self):
        first = item('10.1109/isscc19947.2020.1',
                     '2020 IEEE International Solid-State Circuits Conference - (ISSCC)')
        second = item('10.1109/isscc19947.2020.2',
                      '2020 IEEE International Solid- State Circuits Conference - (ISSCC)')
        client = MagicMock()
        client.get_message.side_effect = [
            {'items': [first], 'total-results': 2, 'next-cursor': 'second'},
            {'items': [second], 'total-results': 2, 'next-cursor': 'end'},
            {'items': [], 'total-results': 2},
            # Exact title order follows lexicographic sorting.
            {'items': [second], 'total-results': 1},
            {'items': [first], 'total-results': 1},
        ]
        with patch('scripts.fetch_ieee_conference_crossref.DISCOVERY_PAGE_SIZE', 1):
            rows, report = fetch_year(source('ISSCC'), 2020, client)
        self.assertEqual({row['DOI'] for row in rows}, {first['DOI'], second['DOI']})
        self.assertEqual(len(report['editions']), 2)
        self.assertEqual(report['discovery'][0]['pages'], 3)
        self.assertTrue(report['discovery'][0]['complete'])

    def test_known_anchor_does_not_skip_additional_containers(self):
        config = source('ISSCC')
        anchor = config['edition_anchors']['2006'][0]
        anchored = item(anchor['doi'], anchor['title'])
        other = item('10.1109/isscc.2006.1', '2006 International Solid-State Circuits Conference')
        client = MagicMock()
        client.get_message.side_effect = [
            {'items': [other], 'total-results': 1},
            {'items': [anchored], 'total-results': 1},
            {'items': [other], 'total-results': 1},
        ]
        rows, report = fetch_year(config, 2006, client)
        self.assertEqual(len(rows), 2)
        self.assertEqual(report['discovery'][1]['method'], 'bounded_discovery')

    def test_all_configured_queries_run_after_first_query_matches(self):
        config = source('ESSCIRC')
        first = item('10.1109/esscirc.2010.1', '2010 ESSCIRC')
        second = item('10.1109/esscirc.2010.2',
                      '2010 European Solid-State Circuits Conference')
        client = MagicMock()
        client.get_message.side_effect = [
            {'items': [first], 'total-results': 1},
            {'items': [second], 'total-results': 1},
            {'items': [first], 'total-results': 1},
            {'items': [second], 'total-results': 1},
        ]
        rows, report = fetch_year(config, 2010, client)
        self.assertEqual(len(rows), 2)
        self.assertEqual([entry['query'] for entry in report['discovery']], ['ESSCIRC', 'Solid'])
        self.assertTrue(all(entry['complete'] for entry in report['discovery']))

    def test_matching_title_does_not_allow_incomplete_or_unbounded_discovery(self):
        record = item('10.1109/isscc.2020.1',
                      '2020 IEEE International Solid-State Circuits Conference')
        for count, error in [(2, 'Incomplete'), (10001, 'not bounded')]:
            client = MagicMock()
            client.get_message.return_value = {'items': [record], 'total-results': count}
            with self.subTest(count=count), self.assertRaisesRegex(RuntimeError, error):
                fetch_year(source('ISSCC'), 2020, client)

    def test_official_historical_title_variants(self):
        examples = [
            ('ISSCC', 2018, '2018 IEEE International Solid - State Circuits Conference - (ISSCC)'),
            ('ISCAS', 2011, '2011 IEEE International Symposium of Circuits and Systems (ISCAS)'),
            ('APCCAS', 2021, '2021 IEEE Asia Pacific Conference on Circuit and Systems (APCCAS)'),
            ('NEWCAS', 2006, '2006 IEEE North-East Workshop on Circuits and Systems'),
            ('NEWCAS', 2008, '2008 Joint 6th International IEEE Northeast Workshop on Circuits and Systems and TAISA Conference'),
        ]
        for name, year, title in examples:
            with self.subTest(name=name, year=year):
                self.assertTrue(title_matches(title, source(name), year))

    def test_ipec_alias_is_restricted_to_asscc_2012_with_its_container(self):
        config = source('ASSCC')
        record = item('10.1109/ipec.2012.6522617',
                      '2012 IEEE Asian Solid State Circuits Conference (A-SSCC)')
        self.assertIsNotNone(item_to_row(record, config, 2012))
        self.assertFalse(doi_matches('10.1109/ipec.2013.123', config, 2013))
        self.assertIsNone(item_to_row(dict(record, **{'container-title': ['2012 International Power Electronics Conference']}), config, 2012))

    def test_vlsi_technology_2020_registered_doi_stem(self):
        config = source('VLSI-Tech')
        # This identity is present in the saved 2020 Crossref discovery page.
        record = item('10.1109/vlsitechnology18217.2020.9265008',
                      '2020 IEEE Symposium on VLSI Technology', '9265008')
        self.assertIsNotNone(item_to_row(record, config, 2020))
        self.assertIsNone(item_to_row(record, config, 2021))
        self.assertIsNone(item_to_row(record, source('VLSI-Circuits'), 2020))
        tsa = item('10.1109/vlsi-tsa48913.2020.9203589',
                   '2020 International Symposium on VLSI Technology, Systems and Applications (VLSI-TSA)')
        self.assertIsNone(item_to_row(tsa, config, 2020))

    def test_esscirc_2020_educational_events_are_not_regular_proceedings(self):
        client = MagicMock()
        rows, report = fetch_year(source('ESSCIRC'), 2020, client)
        self.assertEqual(rows, [])
        self.assertEqual(report['status'], 'confirmed_absence')
        self.assertIn('educational events', report['exception']['reason'])
        self.assertIn('eds.ieee.org', report['exception']['evidence_url'])
        client.get_message.assert_not_called()

    def test_published_vlsi_editions_without_crossref_deposits_remain_unresolved(self):
        for name, years in [('VLSI-Circuits', [2009, 2011, 2013]),
                            ('VLSI-Tech', [2009, 2011, 2013, 2021])]:
            for year in years:
                client = MagicMock()
                client.get_message.return_value = {'items': [], 'total-results': 0}
                with self.subTest(name=name, year=year), self.assertRaises(UnresolvedEditionError):
                    fetch_year(source(name), year, client)

    def test_newcas_2011_full_name_is_discovered_without_acronym(self):
        config = source('NEWCAS')
        title = '2011 IEEE 9th International New Circuits and systems conference'
        record = item('10.1109/newcas.2011.5980825', title, '5980825')
        empty = {'items': [], 'total-results': 0}
        page = {'items': [record], 'total-results': 1}
        client = MagicMock()
        client.get_message.side_effect = [empty, empty, empty, page, page]
        rows, report = fetch_year(config, 2011, client)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['discovery'][3]['query'], 'New')
        self.assertEqual(report['editions'][0]['title'], title)

    def test_newcas_2012_exact_yearless_title_exception_has_narrow_scope(self):
        config = source('NEWCAS')
        title = '10th IEEE International NEWCAS Conference'
        record = item('10.1109/newcas.2012.6328941', title, '6328941')
        self.assertIsNotNone(item_to_row(record, config, 2012))
        self.assertIsNone(item_to_row(record, config, 2011))
        self.assertIsNone(item_to_row(dict(record, DOI='10.1109/newcas.2011.123'), config, 2012))
        self.assertIsNone(item_to_row(dict(record, DOI='10.1109/iscas.2012.123'), config, 2012))
        self.assertFalse(title_matches(title, config, 2013))
        self.assertFalse(title_matches('11th IEEE International NEWCAS Conference', config, 2012))
        self.assertFalse(title_matches('IEEE International NEWCAS Conference', config, 2012))

    def test_unknown_historical_zero_has_evidence_and_fails(self):
        client = MagicMock()
        client.get_message.return_value = {'items': [], 'total-results': 0}
        with self.assertRaises(UnresolvedEditionError) as caught:
            fetch_year(source('ISSCC'), 2010, client)
        self.assertEqual(caught.exception.evidence['status'], 'discovery_unresolved')
        self.assertTrue(caught.exception.evidence['discovery'])

    def test_current_zero_is_pending_never_verified(self):
        client = MagicMock()
        client.get_message.return_value = {'items': [], 'total-results': 0}
        rows, report = fetch_year(source('ISSCC'), datetime.now().year, client)
        self.assertEqual(rows, [])
        self.assertEqual(report['status'], 'not_yet_indexed')

    def test_confirmed_absence_requires_evidence(self):
        config = dict(source('ESSCIRC'), nonpublication_exceptions={'2020': {'reason': 'cancelled'}})
        with self.assertRaisesRegex(ValueError, 'evidence_url'):
            fetch_year(config, 2020, MagicMock())
        config['nonpublication_exceptions']['2020']['evidence_url'] = 'https://example.org/official-event-record'
        client = MagicMock()
        _, report = fetch_year(config, 2020, client)
        self.assertEqual(report['status'], 'confirmed_absence')
        client.get_message.assert_not_called()

    def test_apccas_biennial_exceptions_do_not_skip_2019_annual_edition(self):
        config = source('APCCAS')
        self.assertEqual(set(config['nonpublication_exceptions']),
                         {'2007', '2009', '2011', '2013', '2015', '2017'})
        for year in range(2007, 2018, 2):
            client = MagicMock()
            rows, report = fetch_year(config, year, client)
            self.assertEqual(rows, [])
            self.assertEqual(report['status'], 'confirmed_absence')
            client.get_message.assert_not_called()

    def test_exclusions_and_raw_counts_reconcile(self):
        title = '2012 Symposium on VLSI Circuits'
        good = item('10.1109/vlsic.2012.123', title)
        no_authors = dict(item('10.1109/vlsic.2012.456', title), author=[])
        wrong = item('10.1109/vlsit.2012.9', '2012 Symposium on VLSI Technology')
        client = MagicMock()
        client.get_message.side_effect = [
            {'items': [good]}, {'items': [good, no_authors, wrong], 'total-results': 3},
        ]
        rows, report = fetch_year(source('VLSI-Circuits'), 2012, client)
        edition = report['editions'][0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(edition['candidate_count'], edition['accepted_count'] + edition['excluded_count'])
        self.assertEqual(edition['excluded_reasons'], {'missing_authors': 1, 'other_container': 1})


if __name__ == '__main__':
    unittest.main()
