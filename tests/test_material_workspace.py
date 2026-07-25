import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_paths


class MaterialWorkspaceTests(unittest.TestCase):
    def test_default_category_folders_are_created_and_repaired(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "混剪素材"
            with patch.object(runtime_paths, "MATERIAL_WORKSPACE_DIR", root):
                result = runtime_paths.ensure_material_workspace()
                self.assertEqual(result, root)
                self.assertEqual(
                    {path.name for path in root.iterdir() if path.is_dir()},
                    set(runtime_paths.DEFAULT_MATERIAL_CATEGORIES),
                )
                (root / "空镜").rmdir()
                runtime_paths.ensure_material_workspace()
                self.assertTrue((root / "空镜").is_dir())


if __name__ == "__main__":
    unittest.main()

