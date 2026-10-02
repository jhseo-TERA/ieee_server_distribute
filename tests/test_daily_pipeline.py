import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import daily_pipeline_state
from scripts import run_pdf_download_routine
from scripts import run_pdf_postprocess


ROOT = Path(__file__).resolve().parents[1]


class WeeklyBatchRegressionTests(unittest.TestCase):
    def test_ieee_weekly_batch_uses_only_crlf_line_endings(self):
        raw = (ROOT / "scripts" / "update_ieee_weekly.bat").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_ieee_self_test_runs_before_mysql_start(self):
        text = (ROOT / "scripts" / "update_ieee_weekly.bat").read_text(
            encoding="utf-8"
        )
        self_test_guard = 'if /I "%~1"=="--self-test" goto :self_test'
        mysql_start = '"%ComSpec%" /d /c ""%ROOT%\\scripts\\start_mysql.bat""'
        self.assertIn(self_test_guard, text)
        self.assertLess(text.index(self_test_guard), text.index(mysql_start))
        self.assertIn(
            'call :check_file "%ROOT%\\scripts\\import_excel_to_db.py"', text
        )

    def test_pdf_daily_bootstrap_isolated_from_mysql_batch_exit(self):
        for relative_path in (
            "scripts/run_pdf_download_daily.bat",
            "scripts/start_mysql.bat",
        ):
            raw = (ROOT / relative_path).read_bytes()
            self.assertIn(b"\r\n", raw)
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

        daily = (ROOT / "scripts" / "run_pdf_download_daily.bat").read_text(
            encoding="utf-8"
        )
        mysql = (ROOT / "scripts" / "start_mysql.bat").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '"%ComSpec%" /d /c ""%ROOT%\\scripts\\start_mysql.bat""',
            daily,
        )
        self.assertIn("MySQL 준비 완료; 다운로드 루틴을 시작합니다.", daily)
        self.assertNotIn('start "" /B', mysql)
        self.assertIn("Start-Process", mysql)
        self.assertIn("-WindowStyle Hidden", mysql)

    def test_pdf_task_installer_registers_permanent_optica_split(self):
        installer = (ROOT / "scripts" / "install_pdf_download_task.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("IEEE_Paper_Server_OpticaMorningPDFDownload", installer)
        self.assertIn("New-ScheduledTaskTrigger -Daily -At \"09:00\"", installer)
        self.assertIn("--provider optica --limit 13", installer)
        self.assertIn("IEEE_Paper_Server_DailyPDFDownload", installer)
        self.assertIn("New-ScheduledTaskTrigger -Daily -At \"19:00\"", installer)


class DailyPipelineStateTests(unittest.TestCase):
    def test_checkpoint_reaches_completed_only_after_every_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_pipeline.json"
            state = daily_pipeline_state.begin_pipeline(
                "run-1", "2026-08-04", path=path
            )
            self.assertEqual(state["stages"]["download"]["status"], "running")

            daily_pipeline_state.update_stage(
                "download", "completed", path=path, run_id="run-1", return_code=0
            )
            for stage in daily_pipeline_state.POST_STAGES:
                state = daily_pipeline_state.update_stage(
                    stage, "completed", path=path, run_id="run-1", return_code=0
                )
            self.assertEqual(state["status"], "completed")

    def test_failed_post_stage_remains_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily_pipeline.json"
            daily_pipeline_state.begin_pipeline("run-2", "2026-08-04", path=path)
            daily_pipeline_state.update_stage(
                "download", "completed", path=path, run_id="run-2"
            )
            state = daily_pipeline_state.update_stage(
                "zotero_metadata",
                "failed",
                path=path,
                run_id="run-2",
                return_code=1,
            )
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["stages"]["download"]["status"], "completed")


class DailyPostprocessTests(unittest.TestCase):
    def test_resume_skips_completed_stage_and_runs_remaining_stages(self):
        state = {
            "run_id": "run-3",
            "date": "2026-08-04",
            "stages": {
                "download": {"status": "completed", "successful_downloads": 2},
                "zotero_metadata": {"status": "completed"},
                "zotero_pdf_attachments": {"status": "failed"},
                "obsidian": {"status": "pending"},
            },
        }
        invoked = []

        def update(stage, status, **_kwargs):
            state["stages"][stage]["status"] = status
            return state

        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "post.lock"
            with (
                patch.object(run_pdf_postprocess, "load_pipeline", return_value=state),
                patch.object(run_pdf_postprocess, "update_stage", side_effect=update),
                patch.object(run_pdf_postprocess, "acquire_post_lock", return_value=True),
                patch.object(run_pdf_postprocess, "POST_LOCK_PATH", lock),
                patch.object(
                    run_pdf_postprocess,
                    "run_stage",
                    side_effect=lambda command: invoked.append(command.name) or 0,
                ),
            ):
                result = run_pdf_postprocess.run_postprocess(
                    expected_date="2026-08-04", resume=True
                )

        self.assertEqual(result, 0)
        self.assertEqual(
            invoked,
            ["sync_zotero_pdf_attachments.bat", "zotero_tag_ingest.bat"],
        )

    def test_postprocess_does_not_run_before_download_completion(self):
        state = {
            "run_id": "run-4",
            "date": "2026-08-04",
            "stages": {"download": {"status": "failed"}},
        }
        with (
            patch.object(run_pdf_postprocess, "load_pipeline", return_value=state),
            patch.object(run_pdf_postprocess, "run_stage") as run_stage,
        ):
            result = run_pdf_postprocess.run_postprocess(resume=True)
        self.assertEqual(result, 3)
        run_stage.assert_not_called()


class DailyRoutineLockTests(unittest.TestCase):
    def test_duplicate_invocation_has_distinct_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            lock = state_dir / "routine.lock"
            lock.write_text("active", encoding="utf-8")
            with (
                patch.object(run_pdf_download_routine, "STATE_DIR", state_dir),
                patch.object(run_pdf_download_routine, "LOCK_PATH", lock),
                patch("sys.argv", ["run_pdf_download_routine.py"]),
            ):
                result = run_pdf_download_routine.main()
        self.assertEqual(result, run_pdf_download_routine.EXIT_SKIPPED_LOCKED)


class DailyRoutineBudgetTests(unittest.TestCase):
    def test_optica_provider_attempt_cap_is_morning_only(self):
        self.assertEqual(
            run_pdf_download_routine.provider_allowance("optica", 30, 635), 13
        )
        self.assertEqual(
            run_pdf_download_routine.provider_allowance(
                "optica", 30, 635, provider_used=13
            ),
            0,
        )
        self.assertEqual(
            run_pdf_download_routine.provider_allowance(
                "optica",
                30,
                635,
                provider_used=7,
                invocation_cap=0,
            ),
            0,
        )
        self.assertEqual(
            run_pdf_download_routine.provider_allowance(
                "optica", 30, 635, provider_used=13
            ),
            0,
        )
        self.assertEqual(
            run_pdf_download_routine.provider_allowance("nature", 12, 2), 2
        )

    def test_split_policy_disables_later_optica_run(self):
        state = {"optica_split_morning_date": "2026-08-16"}
        self.assertEqual(
            run_pdf_download_routine.split_policy_invocation_cap(
                "optica",
                "2026-08-16",
                state,
                split_morning_invocation=False,
            ),
            0,
        )
        self.assertIsNone(
            run_pdf_download_routine.split_policy_invocation_cap(
                "optica",
                "2026-08-16",
                state,
                split_morning_invocation=True,
            )
        )
        self.assertIsNone(
            run_pdf_download_routine.split_policy_invocation_cap(
                "nature",
                "2026-08-16",
                state,
                split_morning_invocation=False,
            )
        )

    def test_permanent_policy_disables_afternoon_even_if_morning_bootstrap_failed(self):
        self.assertEqual(
            run_pdf_download_routine.split_policy_invocation_cap(
                "optica",
                "2026-08-16",
                {},
                split_morning_invocation=False,
            ),
            0,
        )
        self.assertEqual(
            run_pdf_download_routine.split_policy_invocation_cap(
                "optica",
                "2026-08-19",
                {},
                split_morning_invocation=False,
            ),
            0,
        )

    def test_child_output_is_safe_for_legacy_windows_console(self):
        rendered = run_pdf_download_routine.console_safe_text(
            "전부 실패 — 권한 확인", encoding="cp949"
        )
        self.assertEqual(rendered, "전부 실패 ? 권한 확인")

    def test_date_scoped_daily_limit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "daily_limit_override.json").write_text(
                '{"date":"2026-08-11","limit":40,"reason":"approved test"}',
                encoding="utf-8",
            )
            with patch.object(run_pdf_download_routine, "STATE_DIR", state_dir):
                limit, override = run_pdf_download_routine.effective_daily_limit(
                    "2026-08-11"
                )

        self.assertEqual(limit, 40)
        self.assertIsNotNone(override)

    def test_daily_limit_override_expires_on_next_date(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "daily_limit_override.json").write_text(
                '{"date":"2026-08-11","limit":40}', encoding="utf-8"
            )
            with patch.object(run_pdf_download_routine, "STATE_DIR", state_dir):
                limit, override = run_pdf_download_routine.effective_daily_limit(
                    "2026-08-12"
                )

        self.assertEqual(limit, run_pdf_download_routine.DAILY_LIMIT)
        self.assertIsNone(override)

    def test_daily_limit_override_cannot_exceed_hard_maximum(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            (state_dir / "daily_limit_override.json").write_text(
                '{"date":"2026-08-11","limit":41}', encoding="utf-8"
            )
            with patch.object(run_pdf_download_routine, "STATE_DIR", state_dir):
                limit, override = run_pdf_download_routine.effective_daily_limit(
                    "2026-08-11"
                )

        self.assertEqual(limit, run_pdf_download_routine.DAILY_LIMIT)
        self.assertIsNone(override)

    def test_provider_summary_reports_actual_publisher_requests(self):
        summary = run_pdf_download_routine.parse_provider_summary(
            "[완료] 성공 0 / 건너뜀 0 / 실패 3"
        )
        self.assertEqual(
            summary,
            {"successes": 0, "skipped": 0, "failures": 3},
        )
        self.assertEqual(
            run_pdf_download_routine.actual_attempt_count(summary, 30),
            3,
        )

    def test_missing_provider_summary_keeps_conservative_reservation(self):
        self.assertEqual(
            run_pdf_download_routine.actual_attempt_count(None, 30),
            30,
        )

    def test_partial_success_completes_download_stage_for_zotero_postprocess(self):
        self.assertEqual(
            run_pdf_download_routine.classify_outcome(1, ["optica"], 10, 2),
            "completed",
        )

    def test_failure_without_downloads_still_blocks_postprocess(self):
        self.assertEqual(
            run_pdf_download_routine.classify_outcome(1, ["optica"], 10, 0),
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
