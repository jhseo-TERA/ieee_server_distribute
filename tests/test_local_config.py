from pathlib import Path
import tempfile
import unittest

from dotenv import dotenv_values
from werkzeug.security import check_password_hash
from scripts.create_local_config import create_config


class LocalConfigTests(unittest.TestCase):
    def create(self, path, **overrides):
        values = dict(db_user='ieee_app', db_password='db-"quote\\value',
                      admin_password='safe-first-password-42!', viewer_password='safe-second-password-42!')
        values.update(overrides)
        create_config(path, **values)

    def test_creates_quoted_settings_and_hashes_without_plaintext_login_passwords(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            self.create(path)
            values = dotenv_values(path)
            self.assertEqual(values['DB_PASSWORD'], 'db-"quote\\value')
            self.assertTrue(check_password_hash(values['ADMIN_PASSWORD_HASH'], 'safe-first-password-42!'))
            self.assertTrue(check_password_hash(values['AUTH_PASSWORD_HASH'], 'safe-second-password-42!'))
            self.assertGreaterEqual(len(values['APP_SECRET_KEY']), 32)
            self.assertEqual(values['ACCOUNT_AUTH_ENABLED'], '1')
            self.assertNotIn('safe-first-password', path.read_text())

    def test_refuses_existing_config_without_changing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text('existing')
            with self.assertRaises(FileExistsError):
                self.create(path)
            self.assertEqual(path.read_text(), 'existing')

    def test_invalid_password_creates_no_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            with self.assertRaises(ValueError):
                self.create(path, admin_password='short')
            self.assertFalse(path.exists())
