import inspect
import unittest

import scripts.serdes_data_pipeline as pipeline_module

from serdes_enrichment import (
    ENERGY_COMPONENT_SCOPE_VERSION,
    LINK_SUBTYPE_VERSION,
    classify_energy_component_scope,
    classify_link_subtype,
    evidence_tier,
    pareto_frontier_ids,
    performance_completeness,
)


class SerdesEnrichmentTests(unittest.TestCase):
    def test_classifier_versions_are_bumped_for_full_reclassification(self):
        self.assertEqual(ENERGY_COMPONENT_SCOPE_VERSION, "serdes-energy-scope-1.1")
        self.assertEqual(LINK_SUBTYPE_VERSION, "serdes-link-subtype-1.1")

    def test_screened_pipeline_uses_latest_complete_all_run_per_venue(self):
        latest_sql = pipeline_module.SERDES_LATEST_COMPLETE_RUNS_SQL
        self.assertIn("MAX(latest_run.id)", latest_sql)
        self.assertIn("latest_run.status='complete'", latest_sql)
        self.assertIn("latest_run.scope_name LIKE 'all%'", latest_sql % ())
        self.assertIn("GROUP BY latest_run.venue", latest_sql)

        functions = (
            pipeline_module.classify_link_media,
            pipeline_module.classify_link_subtypes,
            pipeline_module.fetch_serdes_papers,
            pipeline_module.fetch_abstracts,
            pipeline_module.extract_abstracts,
        )
        for function in functions:
            with self.subTest(function=function.__name__):
                self.assertIn(
                    "SERDES_LATEST_COMPLETE_RUNS_SQL",
                    inspect.getsource(function),
                )
        self.assertGreaterEqual(
            inspect.getsource(pipeline_module.classify_link_media).count(
                "SERDES_LATEST_COMPLETE_RUNS_SQL"
            ),
            2,
        )

    def test_screened_title_fetch_renders_latest_complete_run_clause(self):
        class CaptureCursor:
            def __init__(self):
                self.query = ""

            def execute(self, query, _params):
                self.query = query

            def fetchall(self):
                return []

        cursor = CaptureCursor()
        self.assertEqual(
            pipeline_module.fetch_serdes_papers(cursor, screened_only=True),
            [],
        )
        self.assertIn(
            pipeline_module.SERDES_LATEST_COMPLETE_RUNS_SQL,
            cursor.query,
        )

    def test_energy_scope_prefers_metric_local_evidence(self):
        result = classify_energy_component_scope(
            "A 112-Gb/s Transceiver",
            ["The receiver consumes 40 mW and achieves 0.36 pJ/bit."],
            "tx_rx",
        )

        self.assertEqual(result["energy_component_scope"], "rx")
        self.assertEqual(result["source"], "evidence")

    def test_energy_scope_distinguishes_driver_trx_and_full_link(self):
        cases = {
            "A 56-Gb/s VCSEL Driver in 28-nm CMOS": "driver_only",
            "A 112-Gb/s PAM-4 Transceiver in 7-nm CMOS": "trx",
            "A 1.2-pJ/bit End-to-End Full-Link SerDes": "full_link",
            "A 64-Gb/s Serializer With 4-Tap FFE": "tx",
            "A 56-Gb/s CDR With DFE": "unknown",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    classify_energy_component_scope(title)["energy_component_scope"],
                    expected,
                )

    def test_energy_scope_system_head_is_not_overridden_by_internal_subblocks(self):
        cases = {
            (
                "A 3.8-mW/Gb/s Quad-Channel Serial Link With a 5-Tap DFE "
                "and a 4-Tap Transmit FFE"
            ): "full_link",
            (
                "A 28-Gb/s Multi-Standard SerDes With a 14-Tap DFE"
            ): "trx",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = classify_energy_component_scope(title)
                self.assertEqual(result["energy_component_scope"], expected)

    def test_generic_analog_front_end_is_not_a_partial_scope_claim(self):
        result = classify_energy_component_scope(
            "A 112-Gb/s Transceiver With an Analog Front-End",
        )

        self.assertEqual(result["energy_component_scope"], "trx")
        self.assertNotIn("partial_scope_exclusion", result["reason_codes"])

    def test_transceiver_scope_takes_priority_over_internal_driver(self):
        cases = {
            "An Optical Transceiver With a VCSEL Driver": "trx",
            "A Parallel Transceiver With an Output Driver": "trx",
            "A 56-Gb/s VCSEL Driver in 28-nm CMOS": "driver_only",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                result = classify_energy_component_scope(title)
                self.assertEqual(result["energy_component_scope"], expected)

    def test_metric_attributed_transceiver_takes_priority_over_driver(self):
        result = classify_energy_component_scope(
            "An Optical Transceiver With a VCSEL Driver",
            ["The VCSEL driver in the transceiver consumes 40 mW."],
        )

        self.assertEqual(result["energy_component_scope"], "trx")
        self.assertEqual(result["source"], "evidence")

    def test_clipped_rx_subblock_evidence_does_not_override_transceiver_scope(self):
        result = classify_energy_component_scope(
            "A 2.29-pJ/b 112-Gb/s Wireline Transceiver With RX 4-Tap FFE",
            [
                "The RX analog FFE compensates 20.8-dB loss at a power "
                "efficiency of 2.29 pJ/b."
            ],
            "tx_rx",
        )

        self.assertEqual(result["energy_component_scope"], "trx")
        self.assertEqual(result["source"], "title")

    def test_partial_analog_energy_scope_remains_unknown(self):
        result = classify_energy_component_scope(
            "A 224-Gb/s PAM-4 Transceiver",
            ["The optimized analog power efficiency is 2.2 pJ/bit."],
            "tx_rx",
        )

        self.assertEqual(result["energy_component_scope"], "unknown")
        self.assertEqual(result["source"], "evidence")
        self.assertIn("partial_scope_exclusion", result["reason_codes"])

    def test_energy_scope_does_not_relabel_unknown_from_energy_provenance(self):
        result = classify_energy_component_scope(
            "A Wideband Amplifier", "Energy efficiency is 0.8 pJ/bit.", "unknown",
        )

        self.assertEqual(result["energy_component_scope"], "unknown")

    def test_optical_link_subtypes(self):
        cases = {
            "A 56-Gb/s VCSEL Optical Transmitter": "vcsel",
            "A Silicon-Photonic Optical I/O Chiplet": "silicon_photonic",
            "A Driver for a Directly-Modulated Laser": "eml_dml",
            "A Burst-Mode Receiver for XGS-PON": "pon",
            "A Coherent Optical Receiver": "optical_other",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    classify_link_subtype(title, None, "optical")["link_subtype"],
                    expected,
                )

    def test_electrical_link_subtypes(self):
        cases = {
            "A UCIe Die-to-Die Interface": "die_to_die",
            "A 4-nm HBM3 Memory Interface": "memory_io",
            "A 112-Gb/s Backplane Receiver": "backplane",
            "A 224-Gb/s Active Electrical Cable Transceiver": "cable",
            "A Chip-to-Chip Serial Link": "chip_to_chip",
            "A 112-Gb/s PAM-4 SerDes": "electrical_other",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    classify_link_subtype(title, None, "electrical")["link_subtype"],
                    expected,
                )

    def test_hbm_esd_is_not_high_bandwidth_memory(self):
        result = classify_link_subtype(
            "A 45-Gb/s Transmitter With 2-kV HBM ESD Diodes",
            None,
            "electrical",
        )

        self.assertEqual(result["link_subtype"], "electrical_other")

    def test_prior_work_medium_evidence_does_not_define_link_subtype(self):
        result = classify_link_subtype(
            "A 100-Gb/s PAM-4 Optical Receiver",
            None,
            "optical",
            medium_evidence=(
                "Low-cost silicon-photonic transmitters have been "
                "demonstrated recently."
            ),
        )

        self.assertEqual(result["link_subtype"], "optical_other")
        self.assertEqual(result["source"], "medium")

    def test_prior_work_sentence_variants_do_not_define_link_subtype(self):
        evidence_values = (
            "Silicon-photonic transmitters were proposed previously.",
            "Prior work presented a silicon-photonic link.",
            "Silicon photonics is used in previous generations.",
            "Existing designs target silicon-photonic links.",
        )
        for evidence in evidence_values:
            with self.subTest(evidence=evidence):
                result = classify_link_subtype(
                    "A 100-Gb/s PAM-4 Optical Receiver",
                    None,
                    "optical",
                    medium_evidence=evidence,
                )
                self.assertEqual(result["link_subtype"], "optical_other")
                self.assertEqual(result["source"], "medium")

    def test_implementation_medium_evidence_defines_link_subtype(self):
        result = classify_link_subtype(
            "A 100-Gb/s PAM-4 Optical Receiver",
            None,
            "optical",
            medium_evidence=(
                "This work demonstrates a silicon-photonic link with an "
                "integrated receiver."
            ),
        )

        self.assertEqual(result["link_subtype"], "silicon_photonic")
        self.assertEqual(result["source"], "medium_evidence")

    def test_unspecified_medium_has_zero_subtype_confidence(self):
        result = classify_link_subtype(
            "A 112-Gb/s Interface", None, "unspecified",
        )

        self.assertEqual(result["link_subtype"], "mixed_or_unknown")
        self.assertEqual(result["confidence"], 0.0)

    def test_multiple_subtypes_are_not_arbitrarily_flattened(self):
        result = classify_link_subtype(
            "A VCSEL and Silicon-Photonic Optical Interface", None, "optical",
        )

        self.assertEqual(result["link_subtype"], "optical_other")
        self.assertIn("multiple_subtype_title", result["reason_codes"])

    def test_evidence_tiers_are_provenance_not_quality(self):
        self.assertEqual(evidence_tier("manual", "verified"), "verified")
        self.assertEqual(evidence_tier("reference_xlsx", "curated_reference"), "verified")
        self.assertEqual(evidence_tier("user_sheet", "extracted"), "user_sheet")
        self.assertEqual(evidence_tier("abstract", "extracted"), "abstract")
        self.assertEqual(evidence_tier("title", "extracted"), "title")
        self.assertEqual(evidence_tier(None, None), "screening_inference")

    def test_completeness_keeps_optional_fields_explicit(self):
        result = performance_completeness(
            {
                "reported_rate_gbps": 112.0,
                "rate_scope": "lane",
                "energy_pj_bit": 1.2,
                "process_nm": 7.0,
                "channel_loss_db": None,
            },
            has_abstract=True,
            has_pdf=False,
            energy_component_scope="rx",
        )

        self.assertTrue(result["fom_ready"])
        self.assertTrue(result["comparable_rate"])
        self.assertFalse(result["loss"])
        self.assertFalse(result["pdf"])
        self.assertEqual(result["available"], 5)

    def test_fom_ready_rejects_rate_without_comparison_scope(self):
        base = {
            "reported_rate_gbps": 112.0,
            "energy_pj_bit": 1.2,
            "process_nm": 7.0,
        }
        unknown_scope = performance_completeness(
            {**base, "rate_scope": "unknown"},
            energy_component_scope="rx",
        )
        symbol_only = performance_completeness(
            {
                "symbol_rate_gbaud": 56.0,
                "energy_pj_bit": 1.2,
                "process_nm": 7.0,
            },
            energy_component_scope="rx",
        )
        lane_rate = performance_completeness(
            {
                "lane_rate_gbps": 112.0,
                "energy_pj_bit": 1.2,
                "process_nm": 7.0,
            },
            energy_component_scope="rx",
        )

        self.assertTrue(unknown_scope["rate"])
        self.assertFalse(unknown_scope["comparable_rate"])
        self.assertFalse(unknown_scope["fom_ready"])
        self.assertTrue(symbol_only["rate"])
        self.assertFalse(symbol_only["fom_ready"])
        self.assertTrue(lane_rate["comparable_rate"])
        self.assertTrue(lane_rate["fom_ready"])

    def test_pareto_frontier_is_group_scoped(self):
        points = [
            {"id": 1, "x": 100, "y": 2.0, "group": "electrical:rx"},
            {"id": 2, "x": 100, "y": 1.0, "group": "electrical:rx"},
            {"id": 3, "x": 200, "y": 1.5, "group": "electrical:rx"},
            {"id": 4, "x": 80, "y": 3.0, "group": "optical:rx"},
            {"id": 5, "x": 120, "y": 2.0, "group": None},
        ]

        frontier = pareto_frontier_ids(
            points,
            id_getter=lambda p: p["id"],
            x_getter=lambda p: p["x"],
            y_getter=lambda p: p["y"],
            group_getter=lambda p: p["group"],
        )

        self.assertEqual(frontier, {2, 3, 4})

    def test_pareto_excludes_nonfinite_and_nonpositive_values(self):
        points = [
            {"id": 1, "x": float("nan"), "y": 1, "group": "g"},
            {"id": 2, "x": 10, "y": float("inf"), "group": "g"},
            {"id": 3, "x": 0, "y": 1, "group": "g"},
            {"id": 4, "x": 10, "y": 1, "group": "g"},
        ]
        frontier = pareto_frontier_ids(
            points,
            id_getter=lambda point: point["id"],
            x_getter=lambda point: point["x"],
            y_getter=lambda point: point["y"],
            group_getter=lambda point: point["group"],
        )
        self.assertEqual(frontier, {4})


if __name__ == "__main__":
    unittest.main()
