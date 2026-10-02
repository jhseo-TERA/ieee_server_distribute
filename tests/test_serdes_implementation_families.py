import unittest

from serdes_implementation_families import (
    author_surnames,
    build_match_candidates,
    candidate_key,
    compare_technical_fingerprint,
    decision_is_family_eligible,
    family_key,
    normalize_family_title,
    paper_match_features,
    resolve_decision_status,
    select_family_candidates,
    validate_family_snapshot,
)


def paper(
    paper_id,
    venue,
    year,
    title,
    authors="Alice Gray; Bob Smith; Carol Jones",
    medium="electrical",
):
    return {
        "id": paper_id,
        "article_number": str(1_000_000 + paper_id),
        "source_name": venue,
        "source_type": "journal" if venue in {
            "JSSC", "TCAS-I", "TCAS-II", "OJSSC", "MWTL", "MWCL",
        } else "conference",
        "year": year,
        "title": title,
        "authors": authors,
        "link_medium": medium,
        "implementation_id": paper_id + 10_000,
    }


class SerdesImplementationFamilyTests(unittest.TestCase):
    def test_session_prefix_and_publisher_markup_normalize_identically(self):
        conference = normalize_family_title(
            "6.1 A 112Gb/s PAM-4 Transceiver in 7nm FinFET", "ISSCC",
        )
        journal = normalize_family_title(
            "A 112-Gb/s PAM-4 Transceiver in 7-nm FinFET", "JSSC",
        )

        self.assertEqual(conference, journal)

    def test_exact_conference_journal_pair_is_strict(self):
        rows = [
            paper(
                1, "VLSI-Circuits", 2023,
                "A 112-Gb/s PAM-4 Transceiver in 7-nm FinFET",
            ),
            paper(
                2, "JSSC", 2024,
                "A 112 Gb/s PAM-4 Transceiver in 7 nm FinFET",
            ),
        ]

        candidates = build_match_candidates(rows)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_tier"], "strict")
        self.assertEqual(candidates[0]["proposed_decision_status"], "auto_grouped")
        self.assertIn("strict_reciprocal_best", candidates[0]["reason_codes"])

    def test_rate_conflict_is_never_auto_grouped(self):
        rows = [
            paper(
                10, "ISCAS", 2018,
                "A 20-Gb/s Fully Integrated Optical Receiver With Baud-Rate CDR",
                "Anne Doe; Ben Roe", "optical",
            ),
            paper(
                11, "JSSC", 2019,
                "A 25-Gb/s Fully Integrated Optical Receiver With Baud-Rate CDR",
                "Anne Doe; Ben Roe", "optical",
            ),
        ]

        candidates = build_match_candidates(rows)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_tier"], "manual_review")
        self.assertIn("rate", candidates[0]["technical_conflicts"])

    def test_process_and_medium_conflicts_are_explicit(self):
        left = paper_match_features(paper(
            20, "CICC", 2020,
            "A 56-Gb/s PAM-4 Transmitter in 28-nm CMOS",
            medium="electrical",
        ))
        right = paper_match_features(paper(
            21, "JSSC", 2021,
            "A 56-Gb/s PAM-4 Transmitter in 65-nm CMOS",
            medium="optical",
        ))

        comparison = compare_technical_fingerprint(left, right)

        self.assertIn("process", comparison["conflicts"])
        self.assertIn("medium", comparison["conflicts"])
        self.assertIn("rate", comparison["matches"])

    def test_transmitter_receiver_conflict_is_explicit(self):
        left = paper_match_features(paper(
            30, "CICC", 2020, "A 56-Gb/s PAM-4 Transmitter in 28-nm CMOS",
        ))
        right = paper_match_features(paper(
            31, "JSSC", 2021, "A 56-Gb/s PAM-4 Receiver in 28-nm CMOS",
        ))

        comparison = compare_technical_fingerprint(left, right)

        self.assertIn("block", comparison["conflicts"])

    def test_exact_tie_is_demoted_instead_of_arbitrarily_selected(self):
        title = "A 32-Gb/s Serial-Link Receiver in 28-nm CMOS"
        rows = [
            paper(40, "ISSCC", 2020, title),
            paper(41, "JSSC", 2021, title),
            paper(42, "JSSC", 2021, title),
        ]

        candidates = build_match_candidates(rows)

        self.assertEqual(len(candidates), 2)
        self.assertFalse(any(item["candidate_tier"] == "strict" for item in candidates))
        self.assertTrue(any(
            "insufficient_best_match_margin" in item["reason_codes"]
            for item in candidates
        ))

    def test_disallowed_venue_and_reverse_year_are_blocked(self):
        title = "A 56-Gb/s PAM-4 Receiver in 28-nm CMOS"
        authors = "A Gray; B Smith; C Jones; D Brown"
        rows = [
            paper(50, "PTL", 2020, title, authors),
            paper(51, "JSSC", 2021, title, authors),
            paper(52, "ISSCC", 2022, title, authors),
            paper(53, "JSSC", 2021, title, authors),
        ]

        self.assertEqual(build_match_candidates(rows), [])

    def test_manual_review_and_rejection_survive_rebuild(self):
        self.assertEqual(
            resolve_decision_status("manual_review", "auto_grouped", "classifier"),
            "manual_review",
        )
        self.assertEqual(
            resolve_decision_status("rejected", "auto_grouped", "manual"),
            "rejected",
        )
        self.assertEqual(
            resolve_decision_status(None, "auto_grouped", None),
            "auto_grouped",
        )

    def test_classifier_auto_group_can_be_demoted(self):
        self.assertEqual(
            resolve_decision_status(
                "auto_grouped", "manual_review", "classifier",
            ),
            "manual_review",
        )
        self.assertTrue(decision_is_family_eligible("auto_grouped"))
        self.assertTrue(decision_is_family_eligible("approved"))
        self.assertFalse(decision_is_family_eligible("manual_review"))
        self.assertFalse(decision_is_family_eligible("rejected"))

    def test_manual_approval_wins_conference_membership_conflict(self):
        rows = [
            {
                "id": 10, "conference_paper_id": 1, "journal_paper_id": 2,
                "decision_status": "auto_grouped", "match_score": 0.99,
            },
            {
                "id": 11, "conference_paper_id": 1, "journal_paper_id": 3,
                "decision_status": "approved", "match_score": 0.70,
            },
            {
                "id": 12, "conference_paper_id": 4, "journal_paper_id": 3,
                "decision_status": "rejected", "match_score": 1.0,
            },
        ]

        selected, conflicts = select_family_candidates(rows)

        self.assertEqual([row["id"] for row in selected], [11])
        self.assertEqual([row["id"] for row in conflicts], [10])

    def test_rejected_candidate_cannot_retain_family_projection(self):
        families = [{
            "id": 7, "canonical_paper_id": 2, "review_status": "active",
        }]
        members = [
            {
                "family_id": 7, "paper_id": 2, "implementation_id": 102,
                "match_candidate_id": None, "is_canonical": 1,
            },
            {
                "family_id": 7, "paper_id": 1, "implementation_id": 101,
                "match_candidate_id": 10, "is_canonical": 0,
            },
        ]
        candidate = {
            "id": 10, "conference_paper_id": 1, "journal_paper_id": 2,
            "conference_implementation_id": 101,
            "journal_implementation_id": 102,
            "family_id": 7, "decision_status": "rejected",
        }

        violations = validate_family_snapshot(families, members, [candidate])

        self.assertTrue(any(
            "ineligible candidate 10 retains family 7" in item
            for item in violations
        ))
        released_candidate = dict(candidate, family_id=None)
        inactive_family = dict(families[0], review_status="inactive")
        self.assertEqual(
            validate_family_snapshot([inactive_family], [], [released_candidate]),
            [],
        )

    def test_family_snapshot_detects_stale_member_implementation(self):
        families = [{
            "id": 7, "canonical_paper_id": 2, "review_status": "active",
        }]
        members = [
            {
                "family_id": 7, "paper_id": 2, "implementation_id": 102,
                "match_candidate_id": None, "is_canonical": 1,
            },
            {
                "family_id": 7, "paper_id": 1, "implementation_id": 999,
                "match_candidate_id": 10, "is_canonical": 0,
            },
        ]
        candidate = {
            "id": 10, "conference_paper_id": 1, "journal_paper_id": 2,
            "conference_implementation_id": 101,
            "journal_implementation_id": 102,
            "family_id": 7, "decision_status": "auto_grouped",
        }

        violations = validate_family_snapshot(families, members, [candidate])

        self.assertIn(
            "candidate 10 conference implementation is stale", violations,
        )

    def test_author_parser_handles_ieee_delimiters(self):
        first, surnames = author_surnames(
            "Alice Gray;\nBob van Dyke; Carol Jones",
        )

        self.assertEqual(first, "gray")
        self.assertEqual(surnames, {"gray", "dyke", "jones"})

    def test_keys_are_stable_and_directional(self):
        self.assertEqual(candidate_key(1, 2), candidate_key(1, 2))
        self.assertNotEqual(candidate_key(1, 2), candidate_key(2, 1))
        self.assertEqual(family_key(2), "ieee-journal-family:2")


if __name__ == "__main__":
    unittest.main()
