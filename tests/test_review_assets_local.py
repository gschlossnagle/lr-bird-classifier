from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    @patch("src.review_assets.load_subject_size_metadata")
    def test_dimension_labels_are_drawn_near_box_edges(self, mocked_metadata) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        source = tmpdir / "source.jpg"
        Image.new("RGB", (300, 200), color=(20, 30, 40)).save(source, format="JPEG")
        mocked_metadata.return_value = {
            "focus_distance_m": 20.0,
            "focal_length_35mm_mm": 400.0,
            "image_width": 300,
            "image_height": 200,
        }

        provider = BoxedPreviewProvider(tmpdir / "previews", max_dimension=300, jpeg_quality=95)
        out = provider.build_preview(
            source,
            Detection("bird", 0.9, 60, 40, 180, 140, 0.2),
            "cand_dims",
        )

        with Image.open(out) as rendered:
            # Width label should be centered below the box, so the region just
            # below the bottom edge should contain strong red-ish label pixels.
            width_region_has_label = False
            for y in range(144, 158):
                for x in range(105, 135):
                    px = rendered.getpixel((x, y))
                    if px[0] > 120 and px[0] > px[1] + 20 and px[0] > px[2] + 20:
                        width_region_has_label = True
                        break
                if width_region_has_label:
                    break
            self.assertTrue(width_region_has_label)

            # Height label should be centered to the right of the box.
            height_region_has_label = False
            for y in range(80, 110):
                for x in range(182, 210):
                    px = rendered.getpixel((x, y))
                    if px[0] > 120 and px[0] > px[1] + 20 and px[0] > px[2] + 20:
                        height_region_has_label = True
                        break
                if height_region_has_label:
                    break
            self.assertTrue(height_region_has_label)
