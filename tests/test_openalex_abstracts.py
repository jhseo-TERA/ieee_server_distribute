import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import scripts.fetch_openalex_abstracts as openalex_module

from scripts.fetch_openalex_abstracts import (
    OpenAlexError,
    normalize_doi,
    reconstruct_abstract,
    refresh_serdes_enrichment,
    select_targets,
    work_to_article,
    write_text_cache,
)


class OpenAlexAbstractTests(unittest.TestCase):
    def test_target_selection_uses_latest_complete_screening_runs(self):
        class CaptureCursor:
            def __init__(self):
                self.query = ""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, _params):
                # Mirror PyMySQL interpolation so literal % errors are caught.
                self.query = query % _params

            def fetchall(self):
                return []

        class CaptureConnection:
            def __init__(self):
                self.capture = CaptureCursor()

            def cursor(self):
                return self.capture

        conn = CaptureConnection()
        self.assertEqual(select_targets(conn, limit=10), [])
        self.assertIn("MAX(latest_run.id)", conn.capture.query)
        self.assertIn("latest_run.status='complete'", conn.capture.query)
        self.assertIn("latest_run.scope_name LIKE 'all%'", conn.capture.query)
        self.assertIn("GROUP BY latest_run.venue", conn.capture.query)

    def test_normalizes_doi_urls(self):
        self.assertEqual(
            normalize_doi("https://doi.org/10.1109/JSSC.2016.2519389"),
            "10.1109/jssc.2016.2519389",
        )
        self.assertEqual(normalize_doi("doi: 10.1109/ABC.1"), "10.1109/abc.1")

    def test_reconstructs_inverted_abstract(self):
        index = {"A": [0], "receiver": [1], "works": [2], "well.": [3]}
        self.assertEqual(reconstruct_abstract(index), "A receiver works well.")

    def test_rejects_conflicting_positions(self):
        with self.assertRaises(OpenAlexError):
            reconstruct_abstract({"one": [0], "two": [0]})

    def test_validates_work_identity(self):
        paper = {
            "id": 1,
            "doi": "10.1109/TEST.1",
            "title": "A 60 Gb/s Wireline Receiver",
            "year": "2025",
        }
        work = {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1109/test.1",
            "title": "A 60-Gb/s Wireline Receiver",
            "publication_year": 2025,
            "abstract_inverted_index": {"Measured": [0], "receiver.": [1]},
        }
        article = work_to_article(work, paper)
        self.assertEqual(article["provider_record_id"], "W1")
        self.assertEqual(article["abstract"], "Measured receiver.")

    def test_rejects_title_mismatch(self):
        paper = {
            "id": 1,
            "doi": "10.1109/TEST.1",
            "title": "Wireline Receiver",
            "year": "2025",
        }
        work = {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1109/test.1",
            "title": "Quantum Sensor Array",
            "publication_year": 2025,
            "abstract_inverted_index": {"Text": [0]},
        }
        with self.assertRaises(OpenAlexError):
            work_to_article(work, paper)

    def test_writes_cc0_text_cache(self):
        paper = {
            "article_number": "123",
            "title": "Receiver",
            "source_name": "JSSC",
            "year": "2025",
            "doi": "10.1109/ABC.1",
        }
        article = {
            "title": "Receiver",
            "doi": "10.1109/abc.1",
            "abstract_url": "https://openalex.org/W1",
            "abstract": "A measured receiver.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text_cache(Path(tmp), paper, article)
            content = path.read_text(encoding="utf-8")
        self.assertIn("OpenAlex scholarly metadata (CC0)", content)
        self.assertIn("A measured receiver.", content)

    def test_refresh_extracts_current_abstracts_before_reclassification(self):
        conn = object()
        calls = []

        def record(name, result):
            def invoke(*args, **kwargs):
                calls.append((name, args, kwargs))
                return result
            return invoke

        with (
            patch.object(
                openalex_module,
                "extract_abstracts",
                side_effect=record("extract", {"measurements": 1}),
            ) as extract,
            patch.object(
                openalex_module,
                "classify_link_media",
                side_effect=record("media", {"total": 1}),
            ) as media,
            patch.object(
                openalex_module,
                "classify_measurement_scopes",
                side_effect=record("scope", {"total": 1}),
            ) as scopes,
            patch.object(
                openalex_module,
                "classify_link_subtypes",
                side_effect=record("subtype", {"total": 1}),
            ) as subtypes,
        ):
            result = refresh_serdes_enrichment(conn, venue="JSSC")

        self.assertEqual([item[0] for item in calls], [
            "extract", "media", "scope", "subtype",
        ])
        extract.assert_called_once_with(conn, venue="JSSC", screened_only=True)
        media.assert_called_once_with(conn, included_only=True)
        scopes.assert_called_once_with(conn)
        subtypes.assert_called_once_with(conn, included_only=True)
        self.assertEqual(result["abstract_extraction"], {"measurements": 1})

    def test_main_retries_enrichment_when_fetch_has_no_new_revision(self):
        args = Mock(
            limit=10,
            batch_size=100,
            delay=0.5,
            venue=None,
            retry_terminal=False,
            cache_dir=Path("cache"),
        )
        conn = Mock()
        refreshed = {
            "abstract_extraction": {},
            "link_media": {},
            "energy_scopes": {},
            "link_subtypes": {},
        }
        with (
            patch.object(openalex_module, "build_parser") as parser,
            patch.object(openalex_module, "db_connect", return_value=conn),
            patch.object(openalex_module, "ensure_schema", return_value=1),
            patch.object(
                openalex_module,
                "fetch_openalex",
                return_value={"new_revisions": 0},
            ),
            patch.object(
                openalex_module,
                "refresh_serdes_enrichment",
                return_value=refreshed,
            ) as refresh,
        ):
            parser.return_value.parse_args.return_value = args
            openalex_module.main()

        refresh.assert_called_once_with(conn, venue=None)
        conn.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
