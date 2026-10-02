import copy
import tempfile
from pathlib import Path
import unittest

from scripts.build_serdes_review_queue import csv_cell, write_package
from serdes_review_queue import _enrich_measurement, build_review_package


def measurement(mid, **updates):
    row = {
        "measurement_id": mid,
        "implementation_id": mid,
        "paper_id": mid,
        "article_number": str(100000 + mid),
        "doi": f"10.1109/test.{mid}",
        "title": "A 112-Gb/s PAM-4 Receiver in 7-nm CMOS",
        "authors": "A. Author; B. Author",
        "year": "2025",
        "venue": "JSSC",
        "citation_count": 0,
        "pdf_available": 0,
        "paper_url": None,
        "paper_abstract": "We present the proposed receiver.",
        "relevance_class": "core",
        "link_medium": "electrical",
        "link_class": "backplane",
        "component_scope": "rx",
        "source_kind": "abstract",
        "review_status": "extracted",
        "overall_confidence": 0.8,
        "operating_point_key": "abstract-current",
        "reported_rate_gbps": 112.0,
        "reported_rate_min_gbps": None,
        "reported_rate_max_gbps": None,
        "rate_scope": "lane",
        "lane_rate_gbps": 112.0,
        "lane_count": 1,
        "aggregate_rate_gbps": 112.0,
        "power_mw": None,
        "power_scope": "unknown",
        "energy_pj_bit": 1.0,
        "energy_scope": "reported",
        "energy_basis": "reported_energy",
        "process_text": "7-nm CMOS",
        "process_nm": 7.0,
        "channel_loss_db": 20.0,
        "ber": 1e-12,
        "evidence_count": 1,
    }
    row.update(updates)
    return row


def evidence(mid, text="The receiver achieves 1.0 pJ/bit.", **updates):
    row = {
        "evidence_id": mid * 10,
        "measurement_id": mid,
        "field_name": "energy_pj_bit",
        "source_kind": "abstract",
        "source_url": f"https://example.test/{mid}",
        "source_locator": "Abstract",
        "evidence_text": text,
        "extraction_method": "regex",
        "extractor_version": "test",
        "confidence": 0.8,
        "review_status": "extracted",
    }
    row.update(updates)
    return row


class SerdesReviewQueueTests(unittest.TestCase):
    def test_csv_text_neutralizes_excel_formula_prefixes(self):
        for value in ("=1+1", "+SUM(A1:A2)", "-2+3", "@cmd", "\t=1", "\r=1"):
            with self.subTest(value=value):
                self.assertEqual(csv_cell(value), "'" + value)
        self.assertEqual(csv_cell(-3.5), -3.5)

    def test_stored_scope_and_subtype_override_reclassification(self):
        row = measurement(
            1,
            energy_component_scope="tx",
            scope_source="manual",
            scope_confidence=1.0,
            scope_reason_codes='["manual_override"]',
            link_subtype="die_to_die",
            subtype_source="manual",
            subtype_confidence=1.0,
        )

        enriched = _enrich_measurement(row, [evidence(1)])

        self.assertEqual(enriched["energy_component_scope"], "tx")
        self.assertEqual(enriched["scope_reason_codes"], ["manual_override"])
        self.assertEqual(enriched["link_subtype"], "die_to_die")

    def test_unit_collision_preserves_original_and_avoids_mixed_false_positive(self):
        rows = [
            measurement(1, energy_pj_bit=0.0071),
            measurement(2, energy_pj_bit=0.092),
            measurement(3, energy_pj_bit=0.6),
        ]
        original = copy.deepcopy(rows)
        evidences = [
            evidence(1, "The efficiency is 7.1fJ/b/mm."),
            evidence(2, "The receiver achieves 0.092pJ/b and 7.7fJ/b/dB."),
            evidence(3, "The link efficiency is 0.6pJ/bit/mm."),
        ]

        package = build_review_package(rows, evidences, generated_at="test")
        unit_flags = [
            item for item in package["measurement_flags"]
            if item["flag_code"] == "NORMALIZED_UNIT_AS_ENERGY"
        ]

        self.assertEqual([item["measurement_id"] for item in unit_flags], [1, 3])
        self.assertEqual(unit_flags[0]["original_values"]["energy_pj_bit"], 0.0071)
        self.assertEqual(rows, original)

    def test_reference_context_and_crosscheck_are_high_priority(self):
        rows = [measurement(1), measurement(2, energy_pj_bit=0.033)]
        evidences = [
            evidence(1, "A previous receiver in [4] consumed 75pJ/b."),
            evidence(
                2,
                "Energy = 7.5 mW / 15 Gb/s = 0.5 pJ/bit; reported=0.033; crosscheck=conflict",
                field_name="energy_pj_bit_crosscheck",
                source_kind="derived_fom",
                extraction_method="dimensional_identity_crosscheck",
                review_status="needs_review",
            ),
        ]

        package = build_review_package(rows, evidences, generated_at="test")
        codes = {item["flag_code"] for item in package["measurement_flags"]}

        self.assertIn("REFERENCE_CONTEXT_VALUE", codes)
        self.assertIn("FOM_CROSSCHECK_CONFLICT", codes)
        for item in package["measurement_flags"]:
            if item["flag_code"] in codes:
                self.assertEqual(item["priority"], "P0")

    def test_deterministic_rate_and_power_consistency_flags_have_expected_values(self):
        row = measurement(
            1,
            lane_rate_gbps=50.0,
            lane_count=4,
            aggregate_rate_gbps=150.0,
            reported_rate_min_gbps=40.0,
            reported_rate_max_gbps=60.0,
            reported_rate_gbps=70.0,
            power_mw=100.0,
            power_scope="lane",
            energy_pj_bit=1.0,
        )
        package = build_review_package([row], [evidence(1)], generated_at="test")
        flags = {item["flag_code"]: item for item in package["measurement_flags"]}

        self.assertEqual(flags["AGGREGATE_LANE_MISMATCH"]["expected_value"], 200.0)
        self.assertEqual(flags["AGGREGATE_LANE_MISMATCH"]["delta"], -50.0)
        self.assertEqual(flags["RATE_RANGE_CONFLICT"]["expected_value"], 60.0)
        self.assertAlmostEqual(flags["ENERGY_POWER_RATE_CONFLICT"]["expected_value"], 2.0)
        self.assertAlmostEqual(flags["ENERGY_POWER_RATE_CONFLICT"]["delta"], -1.0)

    def test_robust_outlier_requires_eight_points_and_reports_median_and_z(self):
        energies = [1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 100.0]
        rows = [measurement(index, energy_pj_bit=value) for index, value in enumerate(energies, 1)]
        evidences = [evidence(index, f"The receiver achieves {value} pJ/bit.") for index, value in enumerate(energies, 1)]

        package = build_review_package(rows, evidences, generated_at="test")
        robust = [
            item for item in package["measurement_flags"]
            if item["flag_code"] == "ROBUST_ENERGY_OUTLIER"
        ]

        self.assertEqual(len(robust), 1)
        self.assertEqual(robust[0]["measurement_id"], 8)
        self.assertGreater(robust[0]["group_median"], 1.2)
        self.assertGreater(abs(robust[0]["robust_z"]), 3.5)

        smaller = build_review_package(rows[:7], evidences[:7], generated_at="test")
        self.assertFalse(any(
            item["flag_code"] == "ROBUST_ENERGY_OUTLIER"
            for item in smaller["measurement_flags"]
        ))

    def test_queue_groups_one_paper_and_family_and_assigns_batches(self):
        rows = [
            measurement(1, paper_id=10, energy_pj_bit=1050.0),
            measurement(2, paper_id=10, energy_pj_bit=200.0),
            measurement(3, paper_id=20, title="A Wideband Circuit", component_scope="unknown"),
        ]
        evidences = [evidence(1), evidence(2), evidence(3)]

        package = build_review_package(rows, evidences, batch_size=1, generated_at="test")
        energy_rows = [
            item for item in package["review_queue"]
            if item["paper_id"] == 10 and item["flag_family"] == "energy_extreme"
        ]

        self.assertEqual(len(energy_rows), 1)
        self.assertEqual(energy_rows[0]["measurement_count"], 2)
        self.assertIn("ENERGY_OUTSIDE_HARD_GUARD", energy_rows[0]["flag_codes"])
        self.assertEqual(
            [item["batch_no"] for item in package["review_queue"]],
            list(range(1, len(package["review_queue"]) + 1)),
        )
        self.assertTrue(all(item["batch_item"] == 1 for item in package["review_queue"]))

    def test_json_and_csv_artifacts_are_written_without_database(self):
        package = build_review_package(
            [measurement(1, energy_pj_bit=1050.0)], [evidence(1)], generated_at="test",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = write_package(package, Path(temp_dir), "queue")

            self.assertTrue(all(path.exists() for path in paths.values()))
            self.assertIn("queue_id", paths["review_queue_csv"].read_text(encoding="utf-8-sig"))
            self.assertIn("original_values", paths["measurement_flags_csv"].read_text(encoding="utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
