from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.raw_utils import load_image


class RawUtilsTest(unittest.TestCase):
    def test_raw_load_prefers_embedded_preview(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        raw_path = tmpdir / "source.ARW"
        raw_path.write_bytes(b"not-a-real-raw")
        preview_path = tmpdir / "preview.jpg"
        Image.new("RGB", (64, 32), color=(1, 2, 3)).save(preview_path, format="JPEG")

        class _PreviewContext:
            def __enter__(self):
                return preview_path

            def __exit__(self, *args):
                return None

        with patch("src.preview.jpeg_preview", return_value=_PreviewContext()):
            image = load_image(raw_path)

        self.assertEqual(image.size, (64, 32))


if __name__ == "__main__":
    unittest.main()
