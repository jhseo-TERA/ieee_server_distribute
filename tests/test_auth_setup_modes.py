"""Password tools must import as modules and honor the selected storage mode."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dotenv import dotenv_values
from scripts import setup_admin_auth, setup_viewer_auth, setup_account_auth


class PasswordStorageTests(unittest.TestCase):
    def test_database_mode_updates_only_the_selected_account_and_closes_engine(self):
        for module, key, username in (
            (setup_admin_auth, 'ADMIN_USERNAME', 'admin'),
            (setup_viewer_auth, 'AUTH_USERNAME', 'viewer'),
        ):
            with self.subTest(tool=module.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / '.env'
                original = f'{key}={username}\nACCOUNT_AUTH_ENABLED=1\nAPP_SECRET_KEY=keep\n'
                path.write_text(original, encoding='utf-8')
                engine, store = Mock(), Mock()
                with patch.object(module, 'ENV_PATH', path), \
                     patch.dict(os.environ, {'ACCOUNT_AUTH_ENABLED': '1'}), \
                     patch.object(module, 'connect_engine', return_value=(engine, {})), \
                     patch.object(module, 'AccountStore', return_value=store), \
                     patch.object(module.getpass, 'getpass', return_value='safe-other-password-42!'):
                    module.main()
                store.set_password.assert_called_once_with(username, 'safe-other-password-42!')
                engine.dispose.assert_called_once_with()
                self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_dotenv_can_select_legacy_storage_without_connecting_to_database(self):
        for module, key, hash_key in (
            (setup_admin_auth, 'ADMIN_USERNAME', 'ADMIN_PASSWORD_HASH'),
            (setup_viewer_auth, 'AUTH_USERNAME', 'AUTH_PASSWORD_HASH'),
        ):
            with self.subTest(tool=module.__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / '.env'
                path.write_text(f'{key}=operator\nACCOUNT_AUTH_ENABLED=0\n', encoding='utf-8')
                with patch.object(module, 'ENV_PATH', path), patch.dict(os.environ), \
                     patch.object(module, 'connect_engine') as connect, \
                     patch.object(module.getpass, 'getpass', return_value='safe-other-password-42!'):
                    os.environ.pop('ACCOUNT_AUTH_ENABLED', None)
                    module.main()
                connect.assert_not_called()
                self.assertTrue(dotenv_values(path)[hash_key])

    def test_generic_account_tool_is_importable(self):
        self.assertTrue(callable(setup_account_auth.main))
