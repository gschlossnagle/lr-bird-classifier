from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.catalog_extract import Detection
from src.review_assets import BoxedPreviewProvider


class ReviewAssetsTest(unittest.TestCase):
    def test_boxed_preview_provider_writes_jpeg(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        source = tmpdir / "source.jpg"
        Image.new("RGB", (200, 100), color=(20, 30, 40)).save(source, format="JPEG")

        provider = BoxedPreviewProvider(tmpdir / "previews", max_dimension=128, jpeg_quality=80)
        out = provider.build_preview(
            source,
            Detection("bird", 0.9, 10, 10, 80, 60, 0.2),
            "cand_000001",
        )

        self.assertTrue(out.exists())
        self.assertEqual(out.suffix.lower(), ".jpg")
        with Image.open(out) as rendered:
            self.assertLessEqual(max(rendered.size), 128)

    def test_box_coordinates_scale_with_thumbnail(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        source = tmpdir / "source.jpg"
        Image.new("RGB", (400, 200), color=(20, 30, 40)).save(source, format="JPEG")

        provider = BoxedPreviewProvider(tmpdir / "previews", max_dimension=100, jpeg_quality=95)
        out = provider.build_preview(
            source,
            Detection("bird", 0.9, 200, 50, 300, 150, 0.125),
            "cand_scaled",
        )

        with Image.open(out) as rendered:
            # Original 400x200 scales to 100x50. The bbox should therefore
            # move from x=[200,300], y=[50,150] to x=[50,75], y=[12,38].
            self.assertEqual(rendered.size, (100, 50))
            # Check that a border pixel near the scaled top-left corner is red-ish.
            px = rendered.getpixel((50, 12))
            self.assertGreater(px[0], 150)
