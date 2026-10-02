import unittest

from serdes_fom import (
    FOM_ENERGY_BASIS,
    calculate_energy_pj_bit,
    evaluate_measurement,
    fom_consistency,
    power_semantic_guard,
    rate_semantic_guard,
    select_rate_denominator,
)


def measurement(**overrides):
    row = {
        "id": 10,
        "implementation_id": 20,
        "operating_point_key": "abstract-current",
        "paper_id": 30,
        "paper_title": "A 112-Gb/s SerDes",
        "source_kind": "abstract",
        "review_status": "extracted",
        "overall_confidence": 0.8,
        "power_mw": 224.0,
        "power_scope": "unknown",
        "reported_rate_gbps": 112.0,
        "lane_rate_gbps": None,
        "aggregate_rate_gbps": None,
        "lane_count": None,
        "rate_scope": "unknown",
        "energy_pj_bit": None,
        "energy_basis": None,
    }
    row.update(overrides)
    return row


def evidence(
    field_name,
    text="The receiver operates at 112 Gb/s and consumes 224 mW.",
    *,
    source_kind="abstract",
    locator="Abstract",
    abstract_id=40,
    evidence_id=None,
):
    return {
        "id": evidence_id or (1 if field_name == "power_mw" else 2),
        "field_name": field_name,
        "paper_id": 30,
        "abstract_id": abstract_id,
        "source_kind": source_kind,
        "source_url": "https://example.test/paper",
        "source_locator": locator,
        "evidence_text": text,
        "confidence": 0.8,
        "review_status": "extracted",
    }


class SerdesFomRegenerationTests(unittest.TestCase):
    def test_spreadsheet_dimensional_identity(self):
        self.assertAlmostEqual(calculate_energy_pj_bit(180, 56), 3.2142857142857144)
        self.assertAlmostEqual(calculate_energy_pj_bit(373.6, 64.375), 5.803495145631067)
        self.assertAlmostEqual(calculate_energy_pj_bit(224, 112), 2.0)

    def test_same_abstract_context_is_derived(self):
        decision = evaluate_measurement(
            measurement(), [evidence("power_mw"), evidence("reported_rate_gbps")],
        )

        self.assertEqual(decision["action"], "derive")
        self.assertEqual(decision["selected_rate_field"], "reported_rate_gbps")
        self.assertEqual(decision["calculated_energy_pj_bit"], 2.0)

    def test_abstract_title_fallback_is_not_combined(self):
        decision = evaluate_measurement(
            measurement(),
            [
                evidence("power_mw"),
                evidence(
                    "reported_rate_gbps", source_kind="title",
                    locator="Title", abstract_id=None,
                ),
            ],
        )

        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "missing_rate_evidence")

    def test_distant_abstract_context_is_not_combined(self):
        decision = evaluate_measurement(
            measurement(),
            [
                evidence("power_mw", "The receiver consumes 224 mW at the test supply."),
                evidence("reported_rate_gbps", "A separate experiment reports 112 Gb/s."),
            ],
        )

        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "abstract_context_not_shared")

    def test_curated_sheet_uses_speed_column_even_for_multilane_title(self):
        row = measurement(
            source_kind="user_sheet", review_status="verified",
            reported_rate_gbps=64.375, lane_rate_gbps=64.375,
            aggregate_rate_gbps=257.5, lane_count=4, power_mw=373.6,
        )
        rows = [
            evidence(
                "power_mw", "Power(mW): 373.6; Speed (Gb/s): 64.375",
                source_kind="user_sheet", locator="'SurveyData - ISSCC'!A29:N29",
                abstract_id=None,
            ),
            evidence(
                "reported_rate_gbps", "Speed (Gb/s): 64.375; Power(mW): 373.6",
                source_kind="user_sheet", locator="'SurveyData - ISSCC'!A29:N29",
                abstract_id=None,
            ),
        ]

        decision = evaluate_measurement(row, rows)

        self.assertEqual(decision["action"], "derive")
        self.assertEqual(decision["denominator_reason"], "spreadsheet_speed_column")
        self.assertAlmostEqual(decision["calculated_energy_pj_bit"], 5.803495145631067)

    def test_multilane_unknown_power_scope_is_staged(self):
        choice = select_rate_denominator(
            measurement(
                reported_rate_gbps=28, lane_rate_gbps=28,
                aggregate_rate_gbps=112, lane_count=4, rate_scope="lane",
            ),
            "unknown",
        )

        self.assertFalse(choice.eligible)
        self.assertEqual(choice.reason, "multilane_power_scope_unknown")

    def test_explicit_total_multilane_power_uses_aggregate_rate(self):
        row = measurement(
            reported_rate_gbps=28, lane_rate_gbps=28,
            aggregate_rate_gbps=112, lane_count=4, rate_scope="lane",
            power_mw=224,
        )
        shared = "The four-lane receiver consumes total power of 224 mW at 4x28 Gb/s."
        decision = evaluate_measurement(
            row,
            [evidence("power_mw", shared), evidence("reported_rate_gbps", shared)],
        )

        self.assertEqual(decision["action"], "derive")
        self.assertEqual(decision["selected_rate_field"], "aggregate_rate_gbps")
        self.assertEqual(decision["calculated_energy_pj_bit"], 2.0)

    def test_reported_energy_is_only_audited(self):
        row = measurement(energy_pj_bit=2.0, energy_basis="reported_energy")
        decision = evaluate_measurement(
            row, [evidence("power_mw"), evidence("reported_rate_gbps")],
        )

        self.assertEqual(decision["action"], "audit_match")
        self.assertEqual(decision["existing_energy_pj_bit"], 2.0)
        self.assertEqual(fom_consistency(2.0, 2.0)["status"], "match")

    def test_reported_conflict_is_preserved_for_review(self):
        row = measurement(energy_pj_bit=1.0, energy_basis="reported_energy")
        decision = evaluate_measurement(
            row, [evidence("power_mw"), evidence("reported_rate_gbps")],
        )

        self.assertEqual(decision["action"], "audit_conflict")
        self.assertEqual(decision["existing_energy_pj_bit"], 1.0)

    def test_owned_calculation_is_refreshable(self):
        row = measurement(energy_pj_bit=1.5, energy_basis=FOM_ENERGY_BASIS)
        decision = evaluate_measurement(
            row, [evidence("power_mw"), evidence("reported_rate_gbps")],
        )

        self.assertEqual(decision["action"], "refresh_derived")
        self.assertEqual(decision["calculated_energy_pj_bit"], 2.0)

    def test_power_semantic_guards(self):
        cases = {
            "740 μW off-state power": "inactive_state_power",
            "sub-250 mW power consumption": "bounded_power",
            "optical modulation amplitude of 1.26 mW": "non_consumption_power",
            "1.42 mW/Gb efficiency": "already_normalized_power_per_rate",
            "225 mW/channel": "per_channel_power_scope",
            "wavelength-stabilization power of 0.29 mW": "subblock_power",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                self.assertEqual(power_semantic_guard(text), reason)

    def test_symbol_rate_without_bit_rate_is_not_used(self):
        choice = select_rate_denominator(
            measurement(
                reported_rate_gbps=None, lane_rate_gbps=None,
                aggregate_rate_gbps=None, symbol_rate_gbaud=56,
            ),
        )

        self.assertFalse(choice.eligible)
        self.assertEqual(choice.reason, "missing_fixed_bit_rate")

    def test_rate_range_endpoint_is_not_used_as_an_operating_point(self):
        choice = select_rate_denominator(
            measurement(
                reported_rate_gbps=8.1,
                reported_rate_min_gbps=1.62,
                reported_rate_max_gbps=8.1,
            ),
        )

        self.assertFalse(choice.eligible)
        self.assertEqual(choice.reason, "rate_range_without_operating_point")
        self.assertEqual(
            rate_semantic_guard("A 1.62-to-8.1 Gb/s receiver"),
            "rate_range_without_operating_point",
        )


if __name__ == "__main__":
    unittest.main()
