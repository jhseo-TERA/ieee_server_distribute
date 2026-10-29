"""Read-only, source-bound access to precomputed SerDes parser evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from werkzeug.exceptions import NotFound

import paper_server.serdes.mineru as serdes_mineru
import web.app as survey


class SerdesEvidenceApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "ieee-pdf").mkdir()
        self.pdf = self.root / "ieee-pdf" / "123.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nread-only source fixture")
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE papers (id INTEGER PRIMARY KEY, article_number TEXT, title TEXT, url TEXT, source_system TEXT, pdf_local_path TEXT)")
            conn.exec_driver_sql("INSERT INTO papers VALUES (1,'123','112-Gb/s receiver','https://ieeexplore.ieee.org/document/123/','ieee',NULL), (2,'456','Outside survey','https://example.org/','nature',NULL), (3,'789','Escaping path','','ieee','../outside.pdf')")
            conn.exec_driver_sql("CREATE TABLE serdes_implementation_papers (paper_id INTEGER, implementation_id INTEGER)")
            conn.exec_driver_sql("CREATE TABLE serdes_measurements (id INTEGER, implementation_id INTEGER, operating_point_key TEXT)")
            conn.exec_driver_sql("CREATE TABLE serdes_measurement_evidence (measurement_id INTEGER, field_name TEXT, source_kind TEXT, source_url TEXT, source_locator TEXT, evidence_text TEXT, extraction_method TEXT, extractor_version TEXT, confidence REAL, review_status TEXT, updated_at TEXT)")
            conn.exec_driver_sql("INSERT INTO serdes_implementation_papers VALUES (1,10)")
            conn.exec_driver_sql("INSERT INTO serdes_measurements VALUES (100,10,'112G PAM4')")
            conn.exec_driver_sql("INSERT INTO serdes_measurement_evidence VALUES (100,'lane_rate_gbps','pdf','https://ieeexplore.ieee.org/document/123/','PDF p.2','Measured 112 Gb/s','manual_pdf','test',1,'reviewed',NULL)")
        self.report = {
            "status": "complete", "source_sha256": serdes_mineru.source_sha256(self.pdf),
            "adapter_version": serdes_mineru.ADAPTER_VERSION, "tier": "basic",
            "created_at": "2026-10-06T01:00:00+00:00", "producer": {"version": "4.0.10"},
            "source_path": "secret/source.pdf", "cache_dir": "secret/cache",
            "stats": {"pages": 2, "blocks": 12}, "review_status": "reviewed",
            "candidates": [{"candidate_id": "candidate1", "page": 2, "block_type": "table",
                            "block_id": "p2-b1", "text": "Energy: 1.2 pJ/bit",
                            "table_row": 2, "table_column": 3, "image_path": "secret/image.png",
                            "performance": {"energy_pj_bit": 1.2, "untrusted": "extra"},
                            "conditions": {"modulation": ["PAM4"], "excluded_components": ["excluding CDR"], "other": "secret"},
                            "attribution": "this_work", "energy_component_scope": "rx",
                            "flags": ["table_cell_requires_operating_point_review"], "review_status": "verified"}],
            "conflicts": [{"field": "energy_pj_bit", "values": {"1.2": ["candidate1"], "2.4": ["candidate2"]}}],
            "blocks": [{"raw_html": "<table>untrusted</table>"}],
        }

    def response(self, article="123"):
        with patch.object(survey, "engine", self.engine), patch.object(survey, "ROOT", str(self.root)), survey.app.test_request_context():
            return survey.api_serdes_evidence(article)

    def test_registered_evidence_keeps_operating_point_and_parser_candidates_stay_pending(self):
        with patch.object(survey, "latest_cached_mineru_report", return_value=self.report) as cached, \
                patch.object(serdes_mineru, "extract_mineru_evidence") as parse:
            response = self.response()
        data = response.get_json()
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(data["evidence"][0]["measurement_id"], 100)
        self.assertEqual(data["evidence"][0]["operating_point_key"], "112G PAM4")
        structured = data["structured_evidence"]
        self.assertEqual(structured["review_status"], "needs_review")
        candidate = structured["candidates"][0]
        self.assertEqual(candidate["review_status"], "needs_review")
        self.assertEqual(candidate["pdf_url"], "/pdf/123#page=2")
        self.assertEqual(candidate["conditions"]["excluded_components"], ["excluding CDR"])
        self.assertEqual(structured["conflicts"][0]["values"], ["1.2", "2.4"])
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("raw_html", json.dumps(data))
        self.assertEqual(candidate["performance"], {"energy_pj_bit": 1.2})
        cached.assert_called_once_with(self.pdf.resolve(), cache_root=self.root / "outputs" / "serdes_mineru")
        parse.assert_not_called()

    def test_disallowed_source_and_escaping_local_path_never_read_parser_cache(self):
        with patch.object(survey, "latest_cached_mineru_report") as cached:
            with self.assertRaises(NotFound):
                self.response("456")
            data = self.response("789").get_json()
        self.assertEqual(data["structured_evidence"]["status"], "missing_pdf")
        cached.assert_not_called()

    def test_reviewed_evidence_cannot_be_displaced_by_new_parser_candidates(self):
        with self.engine.begin() as conn:
            for index in range(90):
                conn.execute(text("INSERT INTO serdes_measurement_evidence VALUES (100,'energy_pj_bit','pdf','','p2',:quote,'mineru_basic','test',0.5,'needs_review','2026-10-06')"), {"quote": f"candidate-{index}"})
        with patch.object(survey, "latest_cached_mineru_report", return_value=None):
            result = self.response().get_json()
        self.assertEqual(len(result["evidence"]), 80)
        self.assertEqual(result["evidence"][0]["review_status"], "reviewed")
        self.assertEqual(result["evidence"][0]["text"], "Measured 112 Gb/s")

    def test_pdf_replacement_hides_stale_structured_evidence_without_running_parser(self):
        cache = self.root / "outputs" / "serdes_mineru" / self.report["source_sha256"] / "settings"
        cache.mkdir(parents=True)
        (cache / "evidence.json").write_text(json.dumps(self.report), encoding="utf-8")
        with patch.object(serdes_mineru, "extract_mineru_evidence") as parse:
            self.assertEqual(self.response().get_json()["structured_evidence"]["status"], "available")
            self.pdf.write_bytes(b"%PDF-1.4\nreplacement source")
            result = self.response().get_json()
        self.assertEqual(result["structured_evidence"]["status"], "not_prepared")
        self.assertEqual(len(result["evidence"]), 1)
        parse.assert_not_called()

    def test_corrupt_cache_shape_is_bounded_and_does_not_break_registered_evidence(self):
        self.report["candidates"] = [None, {"page": -1}, *[self.report["candidates"][0]] * 200]
        self.report["conflicts"] = [None, {"field": "untrusted", "values": {"x": []}}]
        with patch.object(survey, "latest_cached_mineru_report", return_value=self.report):
            result = self.response().get_json()
        self.assertEqual(result["structured_evidence"]["candidate_count"], 202)
        self.assertEqual(len(result["structured_evidence"]["candidates"]), 126)
        self.assertEqual(result["structured_evidence"]["conflicts"], [])
        self.assertEqual(len(result["evidence"]), 1)

    def test_unauthenticated_request_cannot_read_cached_evidence(self):
        client = survey.app.test_client()
        with patch.object(survey, "latest_cached_mineru_report") as cached:
            self.assertEqual(client.get("/api/serdes/papers/123/evidence").status_code, 401)
        cached.assert_not_called()


if __name__ == "__main__":
    unittest.main()
