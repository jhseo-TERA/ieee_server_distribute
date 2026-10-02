"""Survey source boundary tests using an isolated SQLite database only."""
import re
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, event, text
from werkzeug.exceptions import NotFound

import web.app as survey


class SerdesSourceScopeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")

        @event.listens_for(self.engine, "connect")
        def sqlite_functions(dbapi, _):
            dbapi.create_function("regexp", 2, lambda pattern, value: bool(re.search(pattern, value or "")))
            dbapi.create_function("CONCAT_WS", -1, lambda sep, *parts: sep.join(str(p) for p in parts if p is not None))

        with self.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE papers (id INTEGER PRIMARY KEY, article_number TEXT, title TEXT, url TEXT, source_system TEXT, source_name TEXT, issue TEXT, pdf_local_path TEXT, pdf_available INTEGER)")
            conn.exec_driver_sql("CREATE TABLE serdes_screening_runs (id INTEGER, venue TEXT, status TEXT, scope_name TEXT)")
            conn.exec_driver_sql("CREATE TABLE serdes_paper_screenings (paper_id INTEGER, run_id INTEGER, relevance_class TEXT, include_in_survey INTEGER)")
            conn.exec_driver_sql("INSERT INTO serdes_screening_runs VALUES (1,'OFC','complete','all-reviewed')")
            # Sources share the same qualifying title and screening state so
            # the boundary, rather than the text pattern, determines exclusion.
            sources = [(1, 'ieee'), (2, 'optica'), (3, 'nature'), (4, 'designcon'),
                       (5, 'optica'), (6, 'optica'), (7, 'ieee')]
            for pid, source in sources:
                conn.execute(text("INSERT INTO papers VALUES (:id,:article,'A 112-Gb/s PAM-4 Transceiver',:url,:source,'OFC','',NULL,0)"),
                             dict(id=pid, article=str(1000 + pid), source=source,
                                  url=f"https://opg.optica.org/abstract.cfm?uri={1000 + pid}"))
                if pid <= 5:
                    conn.execute(text("INSERT INTO serdes_paper_screenings VALUES (:id,1,'core',:included)"),
                                 dict(id=pid, included=int(pid != 5)))

    def tearDown(self):
        self.engine.dispose()

    def ids(self, args):
        where, params = survey._serdes_where(args)
        with self.engine.connect() as conn:
            return conn.execute(text(f"SELECT id FROM papers WHERE {where} ORDER BY id"), params).scalars().all()

    def test_default_adds_only_screened_optica_and_retains_ieee_fallback(self):
        self.assertEqual(self.ids({}), [1, 2, 7])
        self.assertEqual(self.ids({"screening": "included"}), [1, 2])

    def test_audit_can_show_excluded_optica_but_not_other_sources(self):
        self.assertEqual(self.ids({"screening": "screened"}), [1, 2, 5])

    def test_new_partial_run_would_hide_old_population_without_carry_forward(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO serdes_screening_runs VALUES (2,'OFC','complete','all-review')")
            conn.exec_driver_sql("UPDATE serdes_paper_screenings SET run_id=2 WHERE paper_id=2")
        self.assertEqual(self.ids({"screening": "included"}), [2])
        with self.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE serdes_paper_screenings SET run_id=2 WHERE run_id=1")
        self.assertEqual(self.ids({"screening": "included"}), [1, 2])

    def test_evidence_endpoint_accepts_optica_numeric_id_and_rejects_other_sources(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE serdes_implementation_papers (paper_id INTEGER, implementation_id INTEGER)")
            conn.exec_driver_sql("CREATE TABLE serdes_measurements (id INTEGER, implementation_id INTEGER, operating_point_key TEXT)")
            conn.exec_driver_sql("CREATE TABLE serdes_measurement_evidence (measurement_id INTEGER, field_name TEXT, source_kind TEXT, source_url TEXT, source_locator TEXT, evidence_text TEXT, extraction_method TEXT, extractor_version TEXT, confidence REAL, review_status TEXT, updated_at TEXT)")
            conn.exec_driver_sql("INSERT INTO serdes_implementation_papers VALUES (2,20)")
            conn.exec_driver_sql("INSERT INTO serdes_measurements VALUES (200,20,'measured')")
            conn.exec_driver_sql("INSERT INTO serdes_measurement_evidence VALUES (200,'lane_rate_gbps','pdf','https://opg.optica.org/','p3','Measured 112 Gb/s','manual_pdf','test',1,'reviewed',NULL)")
        with patch.object(survey, "engine", self.engine), survey.app.test_request_context():
            result = survey.api_serdes_evidence("1002").get_json()
            self.assertEqual(result['url'], 'https://opg.optica.org/abstract.cfm?uri=1002')
            self.assertEqual(result['evidence'][0]['text'], 'Measured 112 Gb/s')
            for article in ('1003', '1004'):
                with self.subTest(article=article), self.assertRaises(NotFound):
                    survey.api_serdes_evidence(article)

    def test_diagram_lookup_has_same_source_boundary(self):
        with patch.object(survey, "engine", self.engine), survey.app.test_request_context():
            self.assertEqual(survey._diagram_paper('1002')['source_system'], 'optica')
            with self.assertRaises(NotFound):
                survey._diagram_paper('1004')

    def test_performance_preserves_optica_paper_and_evidence_urls(self):
        row = dict(id=1, implementation_id=20, canonical_paper_id=2, article_number='1002',
                   paper_source_system='optica', paper_url='https://opg.optica.org/paper',
                   evidence_url='https://opg.optica.org/pdf', lane_rate_gbps=112,
                   review_status='reviewed', source_kind='pdf')
        point = survey._performance_point(row)
        self.assertEqual(point['url'], row['paper_url'])
        self.assertEqual(point['evidence_url'], row['evidence_url'])
        self.assertEqual(point['source_system'], 'optica')
        self.assertEqual(survey._serdes_paper_url('1002', 'ieee', 'https://proxy/'),
                         'https://ieeexplore.ieee.org/document/1002/')

    def test_chart_query_filters_matched_sources_and_retains_unmatched_references(self):
        connection = Mock()
        connection.execute.return_value.mappings.return_value.all.return_value = []
        engine = Mock()
        engine.connect.return_value.__enter__ = Mock(return_value=connection)
        engine.connect.return_value.__exit__ = Mock(return_value=False)
        with patch.object(survey, 'engine', engine):
            survey.build_serdes_performance({}, strict=True)
        sql = str(connection.execute.call_args.args[0])
        self.assertIn("p.source_system IN ('ieee', 'optica')", sql)
        self.assertIn("i.canonical_paper_id IS NULL AND m.source_kind='reference_xlsx'", sql)
        self.assertIn('p.source_system paper_source_system, p.url paper_url', sql)


if __name__ == '__main__':
    unittest.main()
