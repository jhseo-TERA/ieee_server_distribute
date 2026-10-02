from __future__ import annotations

import unittest

from scripts.export_serdes_review_snapshot import first_author, weighted_interleave


class ReviewSnapshotTests(unittest.TestCase):
    def test_first_author_from_ieee_comma_list_with_final_and(self) -> None:
        authors = "Liheng Lou, Xihan Zhu, Wenjuan Lv, and Yizhe Hu"
        self.assertEqual(first_author(authors), "Liheng Lou")

    def test_first_author_from_semicolon_list(self) -> None:
        self.assertEqual(first_author("Jaeduk Han; Yue Lu; Kwangn Han"), "Jaeduk Han")

    def test_first_author_from_and_list(self) -> None:
        self.assertEqual(first_author("Liping Zhong and Quan Pan"), "Liping Zhong")

    def test_weighted_interleave_assigns_50_row_batches(self) -> None:
        rows = [
            {
                "venue": "PTL" if index % 2 else "JSSC",
                "relevance_score": 30,
                "year": 2020,
                "paper_id": index,
            }
            for index in range(1, 1026)
        ]
        ordered = weighted_interleave(rows)
        self.assertEqual(len(ordered), 1025)
        self.assertEqual(max(row["batch_no"] for row in ordered), 21)
        self.assertEqual(sum(row["batch_no"] == 21 for row in ordered), 25)


if __name__ == "__main__":
    unittest.main()
