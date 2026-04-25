from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def test_raw_load_prefers_rawpy_thumbnail_before_preview_extraction(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        raw_path = tmpdir / "source.ARW"
        raw_path.write_bytes(b"not-a-real-raw")

        jpeg_bytes = io.BytesIO()
        Image.new("RGB", (80, 40), color=(4, 5, 6)).save(jpeg_bytes, format="JPEG")

        class _RawContext:
            def __enter__(self):
                return SimpleNamespace(
                    extract_thumb=lambda: SimpleNamespace(
                        format=fake_rawpy.ThumbFormat.JPEG,
                        data=jpeg_bytes.getvalue(),
                    )
                )

            def __exit__(self, *args):
                return None

        fake_rawpy = SimpleNamespace(
            ThumbFormat=SimpleNamespace(JPEG="jpeg", BITMAP="bitmap"),
            LibRawNoThumbnailError=type("LibRawNoThumbnailError", (Exception,), {}),
            LibRawUnsupportedThumbnailError=type("LibRawUnsupportedThumbnailError", (Exception,), {}),
            LibRawIOError=type("LibRawIOError", (Exception,), {}),
            LibRawFileUnsupportedError=type("LibRawFileUnsupportedError", (Exception,), {}),
            LibRawError=Exception,
            imread=lambda path: _RawContext(),
        )

        with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
            with patch("src.preview.jpeg_preview", side_effect=AssertionError("exiftool preview path should not run")):
                image = load_image(raw_path)

        self.assertEqual(image.size, (80, 40))


if __name__ == "__main__":
    unittest.main()
