import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from paper_server.serdes.enrichment import evidence_tier
from paper_server.serdes.mineru import (
    ADAPTER_VERSION, _cache_path, _settings, build_evidence_report,
    extract_mineru_evidence, latest_cached_mineru_report, mineru_environment,
    source_sha256,
)
from scripts import serdes_data_pipeline as pipeline


def document(*blocks):
    return {"metadata": {"producer": {"name": "mineru", "version": "4.0.10"}},
            "pages": [{"page_idx": 4, "blocks": list(blocks)}]}


class MinerUEvidenceTests(unittest.TestCase):
    def test_real_mineru4_plural_annotations_and_asset_schema(self):
        payload = document({"type": "doc_title", "content": "A receiver"}, {
            "type": "table", "bbox": [.1, .2, .9, .8],
            "content": '<table><tr><td>Metric</td><td>ThisWork</td></tr><tr><td>Power (mW)</td><td>35</td></tr></table>',
            "captions": [{"bbox": [.1, .1, .9, .2], "content": "Measured results at 0.8 V"}],
            "footnotes": [{"bbox": [.1, .8, .9, .9], "content": "Excluding DSP"}],
            "image_source": "images/page_4_table_2.jpg"})
        report = build_evidence_report(payload, source_hash="a" * 64)
        block = report["blocks"][1]
        self.assertEqual(block["caption"], "Measured results at 0.8 V")
        self.assertEqual(block["footnotes"], "Excluding DSP")
        self.assertEqual(block["image_path"], "images/page_4_table_2.jpg")
        self.assertEqual(block["bbox_coordinate_system"], "normalized_top_left_0_1")
        self.assertIn("<table>", block["table_html"])
        self.assertEqual(report["candidates"][0]["performance"]["power_mw"], 35)

    def test_table_uses_only_this_work_column_and_preserves_cell_evidence(self):
        payload = document({"type": "table", "bbox": [10, 20, 500, 300],
            "content": "<table><tr><th>Metric</th><th>[7]</th><th>This Work</th></tr>"
                       "<tr><td>Data rate (Gb/s)</td><td>224</td><td>112</td></tr>"
                       "<tr><td>Power (mW)</td><td>90</td><td>35</td></tr>"
                       "<tr><td>Energy (pJ/bit)</td><td>0.4</td><td>0.31</td></tr></table>",
            "caption": "Measured receiver performance at 0.8 V, excluding DSP."})
        report = build_evidence_report(payload, source_hash="a" * 64)
        self.assertEqual(len(report["candidates"]), 3)
        rate, power, energy = report["candidates"]
        self.assertEqual(rate["performance"]["reported_rate_gbps"], 112)
        self.assertEqual(power["performance"]["power_mw"], 35)
        self.assertEqual(energy["performance"]["energy_pj_bit"], .31)
        self.assertIsNone(energy["performance"]["power_mw"])
        self.assertEqual(energy["table_column"], 3)
        self.assertEqual(energy["table_row"], 4)
        self.assertEqual(energy["page"], 5)
        self.assertEqual(energy["bbox"], [10, 20, 500, 300])
        self.assertEqual(energy["conditions"]["supply_voltage"], ["0.8 V"])
        self.assertEqual(energy["energy_component_scope"], "unknown")

    def test_prior_work_numbers_do_not_become_current_paper_candidates(self):
        report = build_evidence_report(document({"type": "text", "content":
            "Prior work [7] achieved 224 Gb/s at 2.0 pJ/bit. "
            "This work achieves 112 Gb/s at 1.0 pJ/bit."}), source_hash="a" * 64)
        self.assertEqual(len(report["candidates"]), 1)
        metrics = report["candidates"][0]["performance"]
        self.assertEqual(metrics["reported_rate_gbps"], 112)
        self.assertEqual(metrics["energy_pj_bit"], 1.0)

    def test_mixed_attribution_sentence_does_not_form_false_operating_point(self):
        report = build_evidence_report(document({"type": "text", "content":
            "Compared with prior work [7] consuming 2.0 pJ/bit, this work achieves 112 Gb/s."}),
            source_hash="a" * 64)
        self.assertEqual(report["candidates"], [])
        self.assertIn("mixed_attribution_requires_review", report["blocks"][0]["warnings"])

    def test_comparison_without_this_work_and_spans_are_flagged_not_flattened(self):
        for html, warning in [
            ("<table><tr><td>[1]</td><td>[2]</td></tr><tr><td>56 Gb/s</td><td>112 Gb/s</td></tr></table>",
             "table_requires_unique_this_work_label"),
            ("<table><tr><td>Metric</td><td colspan='2'>This work</td></tr><tr><td>Rate</td><td>56 Gb/s</td></tr></table>",
             "table_spans_require_manual_review"),
            ("<table><tr><td>Metric</td><td>This work</td><td>[2]</td></tr><tr><td>Rate</td><td>56 Gb/s</td></tr></table>",
             "table_ragged_rows_require_manual_review"),
            ("<table><tr><td>Metric</td><td>Proposed [7]</td></tr><tr><td>Rate</td><td>56 Gb/s</td></tr></table>",
             "table_requires_unique_this_work_label"),
        ]:
            report = build_evidence_report(document({"type": "table", "content": html}), source_hash="a" * 64)
            self.assertEqual(report["candidates"], [])
            self.assertTrue(any(warning in item for item in report["warnings"]))

    def test_different_conditions_are_not_merged_to_a_single_operating_point(self):
        report = build_evidence_report(document(
            {"type": "text", "content": "This work achieves 56 Gb/s at 0.5 pJ/bit and 0.8 V."},
            {"type": "text", "content": "This work achieves 112 Gb/s at 0.9 pJ/bit and 1.0 V."},
        ), source_hash="b" * 64)
        self.assertEqual(len(report["candidates"]), 2)
        self.assertEqual({x["field"] for x in report["conflicts"]}, {"reported_rate_gbps", "energy_pj_bit"})
        self.assertTrue(all(x["review_status"] == "needs_review" for x in report["candidates"]))
        self.assertNotIn("performance", report)

    def test_candidate_identity_changes_when_interpretation_context_changes(self):
        block = {"type": "text", "content": "This work consumes 35 mW.", "bbox": [0, 0, 100, 100]}
        first = build_evidence_report(document(block), source_hash="a" * 64)["candidates"][0]
        block["caption"] = "Estimated power excluding DSP."
        changed = build_evidence_report(document(block), source_hash="a" * 64)["candidates"][0]
        self.assertNotEqual(first["candidate_id"], changed["candidate_id"])

    def test_ambiguous_metric_is_retained_only_for_review(self):
        report = build_evidence_report(document({"type": "text", "content":
            "This receiver consumes 35 mW or 50 mW depending on mode."}), source_hash="a" * 64)
        candidate = report["candidates"][0]
        self.assertIsNone(candidate["performance"]["power_mw"])
        self.assertIn("power_mw", candidate["performance"]["ambiguous_fields"])

    def test_flash_and_simulation_never_claim_verified_measurement(self):
        report = build_evidence_report(document({"type": "text", "content":
            "This work has simulated energy of 0.8 pJ/bit."}), source_hash="a" * 64, tier="flash")
        flags = report["candidates"][0]["flags"]
        self.assertIn("not_confirmed_measured", flags)
        self.assertIn("flash_native_parse_requires_layout_review", flags)
        self.assertEqual(evidence_tier("pdf", "needs_review"), "needs_review")
        self.assertEqual(evidence_tier("pdf", "verified"), "verified")

    def test_cache_invalidates_when_pdf_bytes_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"old-pdf")
            sha = source_sha256(pdf)
            cache = _cache_path(sha, _settings("basic", 30, 1), root / "cache")
            cache.mkdir(parents=True)
            report = build_evidence_report(document(), source_hash=sha)
            (cache / "evidence.json").write_text(json.dumps(report), encoding="utf-8")
            self.assertIsNotNone(latest_cached_mineru_report(pdf, cache_root=root / "cache"))
            pdf.write_bytes(b"replacement-pdf")
            self.assertIsNone(latest_cached_mineru_report(pdf, cache_root=root / "cache"))

    def test_model_runtime_is_local_and_bounded(self):
        with patch.dict("os.environ", {"MINERU_MODEL_VLM_SERVER_URL": "https://unexpected.invalid"}):
            env = mineru_environment()
        self.assertEqual(env["MINERU_MODEL_SOURCE"], "local")
        self.assertEqual(env["MINERU_MODEL_VLM_SERVER_URL"], "")
        self.assertEqual(env["HF_HUB_OFFLINE"], "1")
        with self.assertRaises(ValueError):
            extract_mineru_evidence("missing.pdf", max_pages=301)

    def test_timeout_never_creates_successful_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf, python = root / "paper.pdf", root / "python.exe"
            pdf.write_bytes(b"%PDF-test")
            python.touch()
            with patch("paper_server.serdes.mineru.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 1)):
                with self.assertRaisesRegex(RuntimeError, "timed out"):
                    extract_mineru_evidence(pdf, python_path=python, cache_root=root / "cache", timeout=1)
            self.assertIsNone(latest_cached_mineru_report(pdf, cache_root=root / "cache"))

    def test_import_preserves_existing_reviewed_candidate(self):
        class Cursor:
            def __init__(self):
                self.responses = iter([
                    {"id": 7, "title": "Paper", "year": "2025", "article_number": "123"},
                    {"id": 44, "review_status": "verified"}])
                self.queries = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, query, args): self.queries.append(query)
            def fetchone(self): return next(self.responses)
            def fetchall(self): return [{"implementation_id": 9}]
        class Connection:
            def __init__(self): self.cur = Cursor()
            def cursor(self): return self.cur
            def commit(self): pass
            def rollback(self): pass
        report = build_evidence_report(document({"type": "text", "content":
            "This work achieves 112 Gb/s."}), source_hash="a" * 64)
        conn = Connection()
        with patch("paper_server.ai.documents.DocumentStore.resolve_path", return_value=Path("paper.pdf")), \
             patch("paper_server.serdes.mineru.source_sha256", return_value="a" * 64):
            result = pipeline.import_mineru_evidence(conn, 7, report)
        self.assertEqual(result["preserved"], 1)
        self.assertEqual(result["inserted"], 0)
        self.assertTrue(all("INSERT" not in query and "UPDATE serdes" not in query for query in conn.cur.queries))

    def test_new_import_keeps_pdf_hash_and_non_ieee_source(self):
        class Cursor:
            def __init__(self):
                self.responses = iter([
                    {"id": 7, "title": "Paper", "year": "2025", "article_number": "123",
                     "source_system": "optica", "url": "https://opg.optica.org/jlt/abstract.cfm?uri=jlt-1"}, None])
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, *args): pass
            def fetchone(self): return next(self.responses)
            def fetchall(self): return [{"implementation_id": 9}, {"implementation_id": 11}]
        class Connection:
            def __init__(self): self.cur = Cursor()
            def cursor(self): return self.cur
            def commit(self): pass
            def rollback(self): pass
        report = build_evidence_report(document({"type": "text", "content":
            "This work achieves 112 Gb/s."}), source_hash="a" * 64)
        with patch("paper_server.ai.documents.DocumentStore.resolve_path", return_value=Path("paper.pdf")), \
             patch("paper_server.serdes.mineru.source_sha256", return_value="a" * 64), \
             patch.object(pipeline, "upsert_implementation", return_value=22) as implementation, \
             patch.object(pipeline, "upsert_measurement", return_value=44) as measurement, \
             patch.object(pipeline, "upsert_evidence") as evidence:
            result = pipeline.import_mineru_evidence(Connection(), 7, report)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(implementation.call_args.args[1], "mineru-paper:7")
        self.assertEqual(measurement.call_args.args[1]["implementation_id"], 22)
        self.assertEqual(measurement.call_args.args[1]["review_status"], "needs_review")
        self.assertTrue(measurement.call_args.kwargs["preserve_existing"])
        self.assertEqual(evidence.call_args.kwargs["source_sha256_override"], "a" * 64)
        self.assertTrue(evidence.call_args.args[5].startswith("https://opg.optica.org/"))

    def test_internal_parallel_output_rate_is_explicitly_flagged(self):
        report = build_evidence_report(document({"type": "text", "content":
            "The data recovery circuit outputs 16-lane 4-Gb/s recovered data."}), source_hash="a" * 64)
        self.assertIn("internal_or_parallel_rate_scope_requires_review", report["candidates"][0]["flags"])


if __name__ == "__main__":
    unittest.main()
