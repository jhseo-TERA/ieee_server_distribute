import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = (
    "sync_zotero_favorites",
    "sync_zotero_pdf_attachments",
)


class ZoteroBatchBootstrapTests(unittest.TestCase):
    def test_wrappers_use_only_crlf_line_endings(self):
        for wrapper in WRAPPERS:
            with self.subTest(wrapper=wrapper):
                raw = (ROOT / "scripts" / f"{wrapper}.bat").read_bytes()
                self.assertIn(b"\r\n", raw)
                self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_mysql_bootstrap_is_isolated_and_failure_gates_sync(self):
        bootstrap = '"%ComSpec%" /d /c ""%ROOT%\\scripts\\start_mysql.bat""'
        for wrapper in WRAPPERS:
            with self.subTest(wrapper=wrapper):
                text = (ROOT / "scripts" / f"{wrapper}.bat").read_text(
                    encoding="utf-8"
                )
                self.assertIn(bootstrap, text)
                self.assertNotIn('call "%ROOT%\\scripts\\start_mysql.bat"', text)
                self.assertLess(
                    text.index(bootstrap), text.index("if errorlevel 1 goto :failed")
                )
                self.assertLess(
                    text.index("if errorlevel 1 goto :failed"),
                    text.index(f'"%PYTHON%" -u "%ROOT%\\scripts\\{wrapper}.py"'),
                )
                self.assertIn(
                    ':failed\nset "RESULT=%ERRORLEVEL%"', text
                )
                self.assertIn("endlocal & exit /b %RESULT%", text)


if __name__ == "__main__":
    unittest.main()
