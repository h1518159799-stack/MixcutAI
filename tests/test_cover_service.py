import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from cover_service import normalize_cover_path


class CoverServiceTests(unittest.TestCase):
    def test_keeps_supported_cover(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "中文封面.jpg"
            Image.new("RGB", (20, 30), "red").save(path)
            self.assertEqual(Path(normalize_cover_path(str(path))), path.resolve())

    def test_converts_bmp_to_png(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "封面.bmp"
            Image.new("RGB", (20, 30), "blue").save(source)
            with patch("cover_service.DATA_DIR", root / "data"):
                result = Path(normalize_cover_path(str(source)))
            self.assertTrue(result.is_file())
            self.assertEqual(result.suffix, ".png")

    def test_rejects_non_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fake.txt"
            path.write_text("not an image", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "封面格式无法识别"):
                normalize_cover_path(str(path))


if __name__ == "__main__":
    unittest.main()
