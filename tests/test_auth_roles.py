import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import web.app as app_module


class AuthRoleTests(unittest.TestCase):
    def setUp(self):
        self.accounts = {
            "viewer": {
                "username": "viewer",
                "password_hash": generate_password_hash("viewer-password"),
                "role": "viewer",
            },
            "admin": {
                "username": "admin",
                "password_hash": generate_password_hash("admin-password"),
                "role": "admin",
            },
        }

    def test_authenticate_assigns_expected_roles(self):
        with patch.object(app_module, "AUTH_ACCOUNTS", self.accounts):
            viewer = app_module._authenticate_account("viewer", "viewer-password")
            admin = app_module._authenticate_account("admin", "admin-password")
            invalid = app_module._authenticate_account("viewer", "wrong-password")

        self.assertEqual(viewer["role"], "viewer")
        self.assertEqual(admin["role"], "admin")
        self.assertIsNone(invalid)

    def test_viewer_cannot_call_favorite_api(self):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "viewer"
            sess["role"] = "viewer"
            sess["csrf_token"] = "test-token"

        response = client.post(
            "/api/favorite",
            json={"article_number": "123"},
            headers={"X-CSRF-Token": "test-token"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "administrator required")

    def test_viewer_page_marks_favorites_read_only(self):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "viewer"
            sess["role"] = "viewer"
            sess["csrf_token"] = "test-token"

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("읽기 전용".encode(), response.data)
        self.assertIn(b"const CAN_EDIT_FAVORITES = false", response.data)
        self.assertIn(b'id="f-size"', response.data)
        self.assertIn(b'<option value="200">200', response.data)


if __name__ == "__main__":
    unittest.main()
