"""Offline backfill state-machine tests; no database or publisher requests."""
from contextlib import nullcontext, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from scripts import backfill_metadata as backfill
from scripts import metadata_enrichment as enrichment
from scripts.metadata_enrichment import sha256, write_json
from scripts.metadata_sources import source
from scripts.run_metadata_update import fingerprint


class MetadataBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        self.directory = self.output / "TCAS-I_2011"
        self.data = self.directory / "validated_candidates.json"
        self.unit_path = self.directory / "unit.json"
        self.records = [{
            "article_number": str(100 + index), "doi": f"10.1109/fixture.{100 + index}",
            "title": f"Test paper {index}", "source_name": "TCAS-I",
        } for index in range(6)]
        write_json(self.data, self.records)
        write_json(self.unit_path, {
            "source": "TCAS-I", "year": 2011, "status": "validated",
            "config_hash": fingerprint(source("TCAS-I")),
            "candidate_sha256": sha256(self.data),
            "missing_existing_anchors": [],
        })
        self.connection = MagicMock()
        self.connect = self.enterContext(patch("pymysql.connect"))
        self.connect.return_value.__enter__.return_value = self.connection
        self.enterContext(patch("scripts.fetch_ieee_crossref.CrossrefClient"))
        self.collect = self.enterContext(patch.object(backfill, "collect_source"))
        self.apply = self.enterContext(patch.object(backfill, "import_new_records"))

    def run_backfill(self, *extra):
        with redirect_stdout(io.StringIO()):
            return backfill.main([
                "--output", str(self.output), "--sources", "TCAS-I",
                "--years", "2011", "--apply", *extra,
            ])

    def unit(self):
        return json.loads(self.unit_path.read_text(encoding="utf-8"))

    def summary(self):
        return json.loads((self.output / "summary.json").read_text(encoding="utf-8"))

    def test_conflict_is_unresolved_and_does_not_disappear_on_plain_rerun(self):
        self.apply.return_value = {"inserted": 0, "existing": 5, "held": 1}
        self.assertEqual(self.run_backfill(), 1)
        self.assertEqual(self.unit()["status"], "completed_with_conflicts")
        self.assertEqual(self.summary()["unresolved"], 1)
        self.assertEqual(self.run_backfill(), 1)
        self.assertEqual(self.apply.call_count, 1)
        self.collect.assert_not_called()

    def test_conflict_retry_reuses_hashed_candidates_and_preserves_insert_history(self):
        self.apply.side_effect = [
            {"inserted": 3, "existing": 2, "held": 1},
            {"inserted": 1, "existing": 5, "held": 0},
        ]
        self.assertEqual(self.run_backfill(), 1)
        self.assertEqual(self.run_backfill("--retry-unresolved"), 0)
        self.collect.assert_not_called()
        self.assertEqual([call.args[1] for call in self.apply.call_args_list], [self.records, self.records])
        unit = self.unit()
        self.assertEqual(unit["status"], "completed")
        self.assertEqual(unit["inserted"], 4)
        self.assertEqual([run["inserted"] for run in unit["applications"]], [3, 1])
        self.assertEqual(self.summary()["inserted"], 4)
        self.assertEqual(self.run_backfill(), 0)
        self.assertEqual(self.apply.call_count, 2)
        self.assertEqual(self.summary()["inserted"], 4)

    def test_changed_cached_candidates_are_never_applied(self):
        write_json(self.data, [{**self.records[0], "doi": "10.1109/wrong.123"}])
        self.assertEqual(self.run_backfill(), 1)
        self.assertEqual(self.unit()["status"], "unresolved")
        self.assertIn("hash changed", self.unit()["error"])
        self.apply.assert_not_called()
        self.collect.assert_not_called()

    def test_changed_source_config_requires_revalidation(self):
        unit = self.unit()
        unit["config_hash"] = "old-source-config"
        write_json(self.unit_path, unit)
        self.assertEqual(self.run_backfill(), 1)
        self.assertIn("hash changed", self.unit()["error"])
        self.apply.assert_not_called()

    def test_gap_status_stays_unresolved_without_explicit_retry(self):
        unit = self.unit()
        unit.update(status="completed_with_gaps", inserted=3,
                    missing_existing_anchors=[{"article_number": "42", "doi": None}])
        write_json(self.unit_path, unit)
        self.assertEqual(self.run_backfill(), 1)
        self.assertEqual(self.summary()["inserted"], 3)
        self.assertEqual(self.summary()["unresolved"], 1)
        self.apply.assert_not_called()
        self.collect.assert_not_called()

    def test_explicit_revalidation_recollects_and_preserves_completed_history(self):
        previous_application = {"path": "prior-reviewed-run", "inserted": 3,
                                "existing": 3, "held": 0}
        previous = self.unit()
        previous.update(status="completed", inserted=3, existing=3, held=0,
                        config_hash="previous-reviewed-rules",
                        applications=[previous_application])
        write_json(self.unit_path, previous)

        # A normal rerun remains a no-op even when collection rules have evolved.
        self.assertEqual(self.run_backfill(), 0)
        self.collect.assert_not_called()
        self.apply.assert_not_called()
        self.assertEqual(self.unit(), previous)

        refreshed = [*self.records, {
            "article_number": "999", "doi": "10.1109/fixture.999",
            "title": "Newly discovered paper", "source_name": "TCAS-I",
        }]
        self.collect.return_value = ([{
            "Journal": "TCAS-I", "Year": "2011", "Title": row["title"],
            "Authors": "A. Author", "Issue": "Vol. 58",
            "URL": f"https://ieeexplore.ieee.org/document/{row['article_number']}/",
            "DOI": row["doi"],
        } for row in refreshed], [])
        self.connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            (row["article_number"], row["doi"]) for row in self.records
        ]
        self.apply.return_value = {"inserted": 1, "existing": 6, "held": 0}

        self.assertEqual(self.run_backfill("--revalidate"), 0)
        self.assertEqual(self.collect.call_count, 1)
        self.assertEqual(self.collect.call_args.args[0]["name"], "TCAS-I")
        self.assertEqual(self.collect.call_args.args[1:3], (2011, 2011))
        self.assertEqual({row["article_number"] for row in self.apply.call_args.args[1]},
                         {row["article_number"] for row in refreshed})
        history = list((self.directory / "history").glob("*.json"))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text(encoding="utf-8")), previous)
        current = self.unit()
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["config_hash"], fingerprint(source("TCAS-I")))
        self.assertEqual(current["inserted"], 4)
        self.assertEqual(current["applications"][0], previous_application)
        self.assertEqual([run["inserted"] for run in current["applications"]], [3, 1])
        self.assertEqual(self.summary()["inserted"], 4)

        self.assertEqual(self.run_backfill(), 0)
        self.assertEqual(self.collect.call_count, 1)
        self.assertEqual(self.apply.call_count, 1)
        self.assertEqual(self.unit(), current)
        self.assertEqual(len(list((self.directory / "history").glob("*.json"))), 1)

    def test_successful_retry_archives_failure_and_clears_current_diagnostics(self):
        for outcome in ('completed', 'confirmed_absence'):
            with self.subTest(outcome=outcome):
                previous = self.unit()
                previous.update(status='unresolved', error='UnresolvedEditionError: old discovery failure',
                                failure_evidence={'status': 'discovery_unresolved', 'old': True})
                write_json(self.unit_path, previous)
                old_history = set((self.directory / 'history').glob('*.json'))
                self.collect.reset_mock()
                self.apply.reset_mock()
                if outcome == 'completed':
                    self.collect.return_value = ([{
                        'Journal': 'TCAS-I', 'Year': '2011', 'Title': 'Recovered paper',
                        'Authors': 'A. Author', 'URL': 'https://ieeexplore.ieee.org/document/123/',
                        'DOI': '10.1109/fixture.123',
                    }], [])
                    self.connection.cursor.return_value.__enter__.return_value.fetchall.return_value = []
                    self.apply.return_value = {'inserted': 1, 'existing': 0, 'held': 0}
                else:
                    self.collect.return_value = ([], [{'status': 'confirmed_absence',
                                                      'reason': 'Reviewed nonpublication exception'}])
                self.assertEqual(self.run_backfill('--retry-unresolved'), 0)
                current = self.unit()
                self.assertEqual(current['status'], outcome)
                self.assertNotIn('error', current)
                self.assertNotIn('failure_evidence', current)
                added_history = set((self.directory / 'history').glob('*.json')) - old_history
                self.assertEqual(len(added_history), 1)
                archived = json.loads(added_history.pop().read_text(encoding='utf-8'))
                self.assertEqual(archived, previous)
                self.assertEqual(self.collect.call_count, 1)
                if outcome == 'confirmed_absence':
                    self.apply.assert_not_called()

    def test_failed_retry_preserves_old_evidence_in_history_and_reports_only_new_failure(self):
        previous = self.unit()
        previous.update(status='unresolved', error='Old discovery failure',
                        failure_evidence={'old': True})
        write_json(self.unit_path, previous)
        self.collect.side_effect = RuntimeError('Current collection failed')
        self.assertEqual(self.run_backfill('--retry-unresolved'), 1)
        current = self.unit()
        self.assertEqual(current['status'], 'unresolved')
        self.assertEqual(current['error'], 'RuntimeError: Current collection failed')
        self.assertNotIn('failure_evidence', current)
        history = list((self.directory / 'history').glob('*.json'))
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0].read_text(encoding='utf-8')), previous)
        self.apply.assert_not_called()


class BackfillConversionTests(unittest.TestCase):
    def test_conflicting_document_dois_are_both_held_while_other_rows_continue(self):
        first = {
            "Journal": "JSSC", "Year": "2008", "Issue": "Vol. 43",
            "Title": "Conflicting DOI registration", "Authors": "A. Author",
            "URL": "https://ieeexplore.ieee.org/document/4625986/",
            "DOI": "10.1109/JSSC.2008.123456",
        }
        second = {**first, "DOI": "10.1109/JSSC.2008.2006513"}
        good = {**first, "URL": "https://ieeexplore.ieee.org/document/12345/",
                "Title": "Unambiguous paper", "DOI": "10.1109/fixture.12345"}
        for conflicting in ([first, second, first], [second, first, second]):
            with self.subTest(first_doi=conflicting[0]["DOI"]):
                held = []
                rows = backfill.convert_rows([good, *conflicting, good], source("JSSC"), held)
                self.assertEqual([row["article_number"] for row in rows], ["12345"])
                candidates = [candidate for conflict in held
                              for candidate in conflict.get("candidates", [conflict.get("candidate")])]
                self.assertEqual({candidate["doi"] for candidate in candidates},
                                 {first["DOI"].lower(), second["DOI"].lower()})
                self.assertEqual({conflict["article_number"] for conflict in held}, {"4625986"})
                self.assertTrue(all(conflict["reason"] == "conflicting_document_metadata" for conflict in held))


class EnrichmentConflictAuditTests(unittest.TestCase):
    def test_shared_doi_holds_both_candidates_even_if_one_key_already_exists(self):
        first = {
            "article_number": "111", "doi": "10.1109/JSSC.shared",
            "title": "First candidate", "authors": "A. Author", "year": "2008",
            "source_name": "JSSC", "source_system": "ieee", "source_type": "journal",
            "url": "https://ieeexplore.ieee.org/document/111/", "issue": None,
        }
        second = {**first, "article_number": "222", "doi": "10.1109/jssc.shared",
                  "url": "https://ieeexplore.ieee.org/document/222/"}
        old = {**first, "id": 7, "pdf_available": 1, "pdf_local_path": "custom/111.pdf",
               "is_favorite": 1}
        for records in ([first, second], [second, first]):
            with self.subTest(first_key=records[0]["article_number"]), TemporaryDirectory() as temporary:
                directory = Path(temporary) / "apply"
                connection = MagicMock()
                current = {"111": dict(old)}
                with patch.object(enrichment, "metadata_lock", return_value=nullcontext()), \
                     patch.object(enrichment, "select_matches", side_effect=[current, {}]):
                    result = enrichment.import_new_records(connection, records, directory)
                self.assertEqual(result, {"inserted": 0, "existing": 0, "held": 2})
                audit = json.loads((directory / "changes.json").read_text(encoding="utf-8"))
                self.assertEqual({row["article_number"] for row in audit["held"]}, {"111", "222"})
                self.assertEqual(current["111"], old)
                connection.cursor.return_value.__enter__.return_value.executemany.assert_not_called()
                connection.cursor.return_value.__enter__.return_value.execute.assert_not_called()

    def test_no_writable_proposals_still_persist_the_audit_summary(self):
        conflicting = [{
            "article_number": key, "new": {"doi": "10.1109/jssc.shared"},
            "evidence": "test fixture",
        } for key in ("111", "222")]
        for proposals in ([], conflicting):
            with self.subTest(proposals=len(proposals)), TemporaryDirectory() as temporary:
                directory = Path(temporary) / "apply"
                connection = MagicMock()
                with patch.object(enrichment, "metadata_lock", return_value=nullcontext()), \
                     patch.object(enrichment, "select_matches") as select:
                    summary = enrichment.apply_proposals(connection, proposals, directory)
                stored = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(stored, summary)
                self.assertEqual(stored["updated"], 0)
                self.assertEqual(len(stored["skipped"]), len(proposals))
                select.assert_not_called()
                connection.cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
