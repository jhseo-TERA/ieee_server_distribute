import unittest
from datetime import date

from web.app import (
    RECO_FIXED_PER_SOURCE,
    RECO_MAX_PER_SOURCE,
    RECO_PER_SOURCE,
    RECO_ROTATION_POOL_SIZE,
    _assemble_source_recommendations,
    _build_profile,
    _local_profile_weight,
    _rank_and_filter_candidates,
    _select_daily_recommendations,
    _title_key,
    _title_terms,
)


class RecommendationLogicTests(unittest.TestCase):
    def test_recommendation_default_and_maximum_are_twenty(self):
        self.assertEqual(RECO_PER_SOURCE, 20)
        self.assertEqual(RECO_MAX_PER_SOURCE, 20)
        self.assertEqual(RECO_FIXED_PER_SOURCE, 3)
        self.assertEqual(RECO_ROTATION_POOL_SIZE, 60)

    def test_daily_rotation_keeps_three_anchors_and_changes_twelve_items(self):
        ranked = [
            {"article_number": f"paper-{index}", "title": f"Paper {index}"}
            for index in range(80)
        ]
        first = _select_daily_recommendations(
            ranked, "JSSC", 15, date(2026, 8, 17)
        )
        repeated = _select_daily_recommendations(
            ranked, "JSSC", 15, date(2026, 8, 17)
        )
        following = _select_daily_recommendations(
            ranked, "JSSC", 15, date(2026, 8, 18)
        )

        self.assertEqual(first, repeated)
        self.assertEqual(
            [item["article_number"] for item in first[:3]],
            ["paper-0", "paper-1", "paper-2"],
        )
        self.assertEqual(first[:3], following[:3])
        self.assertEqual(len(first), 15)
        self.assertEqual(len(following), 15)
        self.assertTrue(
            {item["article_number"] for item in first[3:]}.isdisjoint(
                {item["article_number"] for item in following[3:]}
            )
        )
        self.assertTrue(
            all(int(item["article_number"].split("-")[1]) < 60 for item in first)
        )

    def test_daily_rotation_uses_every_available_candidate_without_duplicates(self):
        ranked = [
            {"article_number": f"paper-{index}", "title": f"Paper {index}"}
            for index in range(9)
        ]

        result = _select_daily_recommendations(
            ranked, "SMALL", 15, date(2026, 8, 17)
        )

        self.assertEqual(result, ranked)
        self.assertEqual(len({item["article_number"] for item in result}), 9)

    def test_source_groups_keep_order_and_cap_each_source_at_fifteen(self):
        sources = [
            {"name": "JSSC", "type": "journal"},
            {"name": "ISSCC", "type": "conference"},
            {"name": "EMPTY", "type": "journal"},
        ]
        items_by_source = {
            "JSSC": [
                {"title": f"Journal paper {index}", "source_system": "ieee"}
                for index in range(17)
            ],
            "ISSCC": [
                {"title": f"Conference paper {index}", "source_system": "ieee"}
                for index in range(3)
            ],
            "EMPTY": [],
        }

        items, groups = _assemble_source_recommendations(
            sources,
            items_by_source,
            {"JSSC": ["silicon"], "ISSCC": ["circuit"]},
            {"JSSC": 10, "ISSCC": 2, "EMPTY": 0},
            per_source=15,
        )

        self.assertEqual([group["name"] for group in groups], ["JSSC", "ISSCC"])
        self.assertEqual([group["count"] for group in groups], [15, 3])
        self.assertEqual(len(items), 18)
        self.assertTrue(all(item["source_group"] == "ieee" for item in items))

    def test_profile_is_deterministic_and_can_drop_singletons(self):
        titles = [
            "Silicon photonic PAM-4 transmitter",
            "Silicon photonic receiver",
            "One-off avalanche detector",
        ]

        profile = _build_profile(titles, topn=10, min_freq=2)

        self.assertEqual(profile, ["photonic", "silicon"])

    def test_domain_token_normalization_does_not_emit_broken_pam_token(self):
        terms = _title_terms("A 224-Gb/s PAM-4 silicon-photonic transmitter")

        self.assertIn("pam", terms)
        self.assertNotIn("pam-", terms)

    def test_local_profile_weight_shrinks_with_small_sample(self):
        self.assertEqual(_local_profile_weight(0), 0.0)
        self.assertLess(_local_profile_weight(12), _local_profile_weight(208))
        self.assertLessEqual(_local_profile_weight(10_000), 0.65)

    def test_correction_title_matches_original_title_key(self):
        original = (
            "An integrated CMOS-silicon photonics transmitter with a 112 gigabaud "
            "transmission"
        )
        correction = "Publisher Correction: " + original

        self.assertEqual(_title_key(correction), _title_key(original))

    def test_reranking_filters_corrections_and_duplicates(self):
        original = "Integrated silicon photonic transmitter"
        rows = [
            {
                "title": "Publisher Correction: " + original,
                "year": "2026", "source_name": "NCOMMS", "score": 100.0,
            },
        ]
        for index in range(6):
            rows.append({
                "title": f"Silicon photonic transmitter architecture {index}",
                "year": "2026", "source_name": "NCOMMS", "score": 20.0 - index,
            })
        for index in range(2):
            rows.append({
                "title": f"Optical receiver and silicon modulator {index}",
                "year": "2025", "source_name": "NPHOTON", "score": 10.0 - index,
            })

        result = _rank_and_filter_candidates(
            rows,
            ["silicon", "photonic", "transmitter", "optical", "receiver", "modulator"],
            {_title_key(original)},
            limit=5,
            current_year=2026,
        )

        self.assertEqual(len(result), 5)
        self.assertFalse(any(item["title"].lower().startswith("publisher correction") for item in result))
        self.assertTrue(all("text_score" in item and "keyword_overlap" in item for item in result))


if __name__ == "__main__":
    unittest.main()
