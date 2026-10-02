"""Safety checks for the finite approved SerDes metadata update (no DB access)."""
from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_apply", ROOT / "scripts/apply_serdes_source_review_210.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewApplySafetyTests(unittest.TestCase):
    def setUp(self):
        self.before = {"measurements": [], "evidence": [], "metric_scopes_all": [], "scope_overrides_all": []}
        self.original = {"measurements": []}
        self.entries = {}
        self.links = []
        for mid in sorted({m for m, _ in review.APPROVED}):
            article = next(v[0] for (m, _), v in review.APPROVED.items() if m == mid)
            row = {"id": mid, "component_scope": "full_link", "process_nm": 28,
                   "process_text": "28.0", "energy_pj_bit": None, "power_mw": None,
                   "updated_at": "2026-09-07T08:30:46"}
            for (m, f), (_, value, _) in review.APPROVED.items():
                if m == mid:
                    row[f] = value
            self.before["measurements"].append(copy.deepcopy(row))
            self.original["measurements"].append(copy.deepcopy(row))
            self.entries[mid] = {"article": article, "reason": "Source-reviewed field correction.",
                "pdf_evidence": [{"page": 1, "excerpt": "Original measurement scope.", "sha256": "a" * 64}]}
            self.links.append({"measurement_id": mid, "paper_id": mid + 10000, "article_number": article})
        self.before["measurements"].append({"id": 2131, "component_scope": "full_link", "energy_pj_bit": 4.18})
        self.before["evidence"] = [{"id": 1, "evidence_key": "old", "evidence_text": "Never replace me."}]

    def validate(self, state=None):
        return review.validate_targets(state or self.before, self.original, self.entries, self.links)

    def fake_applied(self):
        pending, _, additions = self.validate()
        after = copy.deepcopy(self.before)
        for row in after["measurements"]:
            for (mid, field), (_, _, value) in review.APPROVED.items():
                if row["id"] == mid:
                    row[field] = value
                    row["updated_at"] = "2026-09-29T11:40:00"
        after["evidence"] += [dict(record, id=100 + i) for i, record in enumerate(additions)]
        return pending, additions, after

    def test_exact_population_and_second_run_idempotence(self):
        pending, additions, after = self.fake_applied()
        self.assertEqual(len(pending), 10)
        self.assertEqual(len(additions), 11)
        review.verify_after(self.before, after, pending, additions)
        again, completed, new = self.validate(after)
        self.assertEqual(again, [])
        self.assertEqual(len(completed), 10)
        self.assertEqual(new, [])

    def test_rejects_unrelated_energy_drift(self):
        self.before["measurements"][0]["energy_pj_bit"] = 2.59
        with self.assertRaisesRegex(review.ReviewGuardError, "unrelated field changed"):
            self.validate()

    def test_rejects_timestamp_drift_even_if_values_match(self):
        self.before["measurements"][0]["updated_at"] = "2026-09-29T11:20:00"
        with self.assertRaisesRegex(review.ReviewGuardError, "updated_at precondition"):
            self.validate()

    def test_rejects_partial_process_update(self):
        next(r for r in self.before["measurements"] if r["id"] == 2143)["process_nm"] = 16
        with self.assertRaisesRegex(review.ReviewGuardError, "neither approved before-state"):
            self.validate()

    def test_rejects_after_values_without_matching_pdf_evidence(self):
        _, _, after = self.fake_applied()
        after["evidence"] = after["evidence"][:1]
        with self.assertRaisesRegex(review.ReviewGuardError, "without this review's evidence"):
            self.validate(after)

    def test_rejects_hold_mutation(self):
        pending, additions, after = self.fake_applied()
        next(r for r in after["measurements"] if r["id"] == 2131)["energy_pj_bit"] = 4.107142857
        with self.assertRaisesRegex(review.ReviewGuardError, "Protected measurement"):
            review.verify_after(self.before, after, pending, additions)

    def test_rejects_energy_scope_and_old_evidence_mutation(self):
        pending, additions, after = self.fake_applied()
        after["metric_scopes_all"].append({"measurement_id": 1374, "energy_component_scope": "trx"})
        with self.assertRaisesRegex(review.ReviewGuardError, "Protected metric_scopes_all"):
            review.verify_after(self.before, after, pending, additions)
        after["metric_scopes_all"] = []
        after["evidence"][0]["evidence_text"] = "Unexpected replacement"
        with self.assertRaisesRegex(review.ReviewGuardError, "Existing evidence was modified"):
            review.verify_after(self.before, after, pending, additions)


if __name__ == "__main__":
    unittest.main()
