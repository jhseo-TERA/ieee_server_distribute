import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values
from werkzeug.security import check_password_hash

from scripts import setup_admin_auth
from scripts import sync_user_serdes_survey as survey


class PrivateSourceSettingsTests(unittest.TestCase):
    def test_admin_password_change_preserves_configured_account_and_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("ADMIN_USERNAME=custom-admin\nADMIN_PASSWORD_HASH=old\n"
                            "AUTH_USERNAME=custom-viewer\nAUTH_PASSWORD_HASH=keep\n"
                            "APP_SECRET_KEY=keep-session\n", encoding="utf-8")
            with patch.object(setup_admin_auth, "ENV_PATH", path), patch.object(
                    setup_admin_auth.getpass, "getpass", side_effect=["new-strong-password"] * 2):
                setup_admin_auth.main()
            values = dotenv_values(path)
        self.assertEqual(values["ADMIN_USERNAME"], "custom-admin")
        self.assertEqual(values["AUTH_USERNAME"], "custom-viewer")
        self.assertEqual(values["AUTH_PASSWORD_HASH"], "keep")
        self.assertEqual(values["APP_SECRET_KEY"], "keep-session")
        self.assertTrue(check_password_hash(values["ADMIN_PASSWORD_HASH"], "new-strong-password"))

    def test_new_admin_uses_generic_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("AUTH_USERNAME=viewer\n", encoding="utf-8")
            with patch.object(setup_admin_auth, "ENV_PATH", path), patch.object(
                    setup_admin_auth.getpass, "getpass", side_effect=["new-strong-password"] * 2):
                setup_admin_auth.main()
            self.assertEqual(dotenv_values(path)["ADMIN_USERNAME"], "admin")

    def test_admin_id_in_password_is_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            original = "ADMIN_USERNAME=custom-admin\nADMIN_PASSWORD_HASH=keep\n"
            path.write_text(original, encoding="utf-8")
            with patch.object(setup_admin_auth, "ENV_PATH", path), patch.object(
                    setup_admin_auth.getpass, "getpass", side_effect=["prefix-custom-admin-password"] * 2):
                with self.assertRaises(SystemExit):
                    setup_admin_auth.main()
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_survey_key_prefix_preserves_configured_namespace_and_stable_hash(self):
        record = {"publication": "JSSC", "sheet": "SurveyData - JSSC", "year": 2025,
                  "normalized_title": "example survey paper"}
        digest = survey.sha256_text("SurveyData - JSSC|2025|example survey paper")[:16]
        with patch.object(survey, "SURVEY_KEY_PREFIX", "legacy-user"):
            self.assertEqual(survey.operating_point_key(record), f"legacy-user-jssc-{digest}")
        with patch.object(survey, "SURVEY_KEY_PREFIX", "survey"):
            self.assertEqual(survey.operating_point_key(record, "-review"), f"survey-jssc-{digest}-review")

    def test_survey_report_name_uses_private_configuration(self):
        with patch.object(survey, "SURVEY_REPORT_NAME", "legacy-report.json"):
            self.assertEqual(survey.build_parser().parse_args(["example.xlsx"]).report.name, "legacy-report.json")


if __name__ == "__main__":
    unittest.main()
