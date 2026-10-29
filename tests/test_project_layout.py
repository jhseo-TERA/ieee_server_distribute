"""Package relocation must preserve production data and worker paths."""
from pathlib import Path
import unittest

from paper_server.paths import ROOT
from paper_server.ai.repository import LocalAIRepository
from paper_server.serdes import mineru


class ProjectLayoutTests(unittest.TestCase):
    def test_components_resolve_the_project_root_not_the_package(self):
        expected = Path(__file__).resolve().parents[1]
        self.assertEqual(ROOT, expected)
        self.assertEqual(LocalAIRepository(None).root, expected)
        self.assertEqual(mineru.ROOT, expected)
        self.assertEqual(mineru.DEFAULT_CACHE_ROOT, expected / 'outputs' / 'serdes_mineru')

    def test_migration_and_worker_entry_points_are_still_available(self):
        self.assertTrue((ROOT / 'scripts/migrations/008_local_ai.sql').is_file())
        self.assertTrue((mineru.ROOT / 'scripts/mineru_worker.py').is_file())

    def test_schema_uses_project_migration_after_relocation(self):
        from unittest.mock import MagicMock
        engine = MagicMock()
        LocalAIRepository(engine).ensure_schema()
        self.assertGreater(engine.begin.return_value.__enter__.return_value.execute.call_count, 0)


if __name__ == '__main__':
    unittest.main()
