from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BackupPolicyTests(unittest.TestCase):
    def read(self, relative_path: str) -> str:
        return (ROOT / relative_path).read_text(encoding="utf-8")

    def test_source_snapshot_is_seven_day_code_only_backup(self):
        script = self.read("scripts/backup_source.ps1")
        self.assertIn("$retentionDays = 7", script)
        self.assertIn('"scripts", "web", "tests", "py_00_src"', script)
        self.assertIn('".pdf"', script)
        self.assertNotIn('".mcp.json"', script)
        self.assertIn("tar -tzf", script)
        self.assertIn(".partial", script)

    def test_mysql_dump_is_validated_and_does_not_archive_credentials(self):
        script = self.read("scripts/backup_mysql.ps1")
        for option in (
            "--single-transaction",
            "--result-file=",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
            "$retentionDays = 7",
            "Get-FileHash",
            "tar -tzf",
        ):
            self.assertIn(option, script)
        self.assertLess(script.index("Remove-Item -LiteralPath $optionFile"), script.index("& $tar -czf"))

    def test_task_installers_use_cmd_wrapper_and_expected_schedule(self):
        backups = self.read("scripts/install_backup_tasks.ps1")
        weekly = self.read("scripts/install_ieee_weekly_task.ps1")
        self.assertIn('/d /c', backups)
        self.assertIn('At = "02:00"', backups)
        self.assertIn('At = "02:15"', backups)
        self.assertIn('/d /c', weekly)
        self.assertIn('-DaysOfWeek Monday -At "03:00"', weekly)
        self.assertIn('"update_ieee_weekly.bat"', weekly)

    def test_git_backup_excludes_data_and_can_rebuild_venv(self):
        ignore = self.read(".gitignore")
        restore = self.read("scripts/restore_venv.ps1")
        for excluded in (".env", ".venv/", "*.pdf", "_backups/", "logs/"):
            self.assertIn(excluded, ignore)
        self.assertIn("requirements.lock.txt", restore)
        self.assertIn("-m venv", restore)
        self.assertIn("-m unittest discover", restore)


if __name__ == "__main__":
    unittest.main()
