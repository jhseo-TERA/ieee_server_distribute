from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.security import check_password_hash

from scripts import setup_viewer_auth


class ViewerAuthSetupTests(unittest.TestCase):
    def test_updates_only_viewer_hash_and_preserves_other_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "AUTH_USERNAME=viewer\nAUTH_PASSWORD_HASH=old\n"
                "ADMIN_PASSWORD_HASH=keep-admin\nAPP_SECRET_KEY=keep-session\n",
                encoding="utf-8",
            )
            with patch.object(setup_viewer_auth, "ENV_PATH", env_path), patch.object(
                setup_viewer_auth.getpass,
                "getpass",
                side_effect=["new-secure-password", "new-secure-password"],
            ):
                setup_viewer_auth.main()
            values = env_path.read_text(encoding="utf-8").splitlines()
            stored = next(line.split("=", 1)[1] for line in values if line.startswith("AUTH_PASSWORD_HASH="))
            self.assertTrue(check_password_hash(stored, "new-secure-password"))
            self.assertIn("AUTH_USERNAME=viewer", values)
            self.assertIn("ADMIN_PASSWORD_HASH=keep-admin", values)
            self.assertIn("APP_SECRET_KEY=keep-session", values)

    def test_rejects_mismatch_short_and_username_passwords(self):
        cases = [
            ["first-password", "second-password"],
            ["short", "short"],
            ["prefix-viewer-password", "prefix-viewer-password"],
        ]
        for answers in cases:
            with self.subTest(answers=answers), tempfile.TemporaryDirectory() as directory:
                env_path = Path(directory) / ".env"
                env_path.write_text("AUTH_USERNAME=viewer\nAUTH_PASSWORD_HASH=old\n", encoding="utf-8")
                with patch.object(setup_viewer_auth, "ENV_PATH", env_path), patch.object(
                    setup_viewer_auth.getpass, "getpass", side_effect=answers
                ), self.assertRaises(SystemExit):
                    setup_viewer_auth.main()
                self.assertIn("AUTH_PASSWORD_HASH=old", env_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
