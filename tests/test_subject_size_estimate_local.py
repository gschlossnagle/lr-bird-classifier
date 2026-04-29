from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.subject_size_estimate import (
    _parse_length_meters,
    _parse_length_millimeters,
    estimate_subject_box_size_cm,
    estimated_subject_box_size_for_candidate,
    load_subject_size_metadata,
)


class SubjectSizeEstimateTest(unittest.TestCase):
    def tearDown(self) -> None:
        load_subject_size_metadata.cache_clear()

    def test_parse_length_meters(self) -> None:
        self.assertAlmostEqual(_parse_length_meters("64.38 m"), 64.38)
        self.assertAlmostEqual(_parse_length_meters("200 cm"), 2.0)
        self.assertAlmostEqual(_parse_length_meters("500 mm"), 0.5)

    def test_parse_length_millimeters(self) -> None:
        self.assertAlmostEqual(_parse_length_millimeters("800 mm"), 800.0)
        self.assertAlmostEqual(_parse_length_millimeters("40cm"), 400.0)

    def test_estimate_subject_box_size_cm(self) -> None:
        width_cm, height_cm = estimate_subject_box_size_cm(
            image_width=6000,
            image_height=4000,
            bbox_x1=1500,
            bbox_y1=1000,
            bbox_x2=3000,
            bbox_y2=2000,
            focus_distance_m=50.0,
            focal_length_35mm_mm=400.0,
        )
        self.assertAlmostEqual(width_cm, 112.5, places=1)
        self.assertAlmostEqual(height_cm, 75.0, places=1)

    def test_load_subject_size_metadata_reads_exiftool_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.ARW"
            path.write_bytes(b"raw")
            stdout = json.dumps(
                [
                    {
                        "FocusDistance2": "47.35 m",
                        "FocalLengthIn35mmFormat": "400 mm",
                        "ImageWidth": 6048,
                        "ImageHeight": 4020,
                    }
                ]
            )
            completed = subprocess.CompletedProcess(["exiftool"], 0, stdout=stdout, stderr="")
            with patch("src.subject_size_estimate.subprocess.run", return_value=completed):
                metadata = load_subject_size_metadata(path)
        self.assertEqual(metadata["image_width"], 6048)
        self.assertEqual(metadata["image_height"], 4020)
        self.assertAlmostEqual(metadata["focus_distance_m"], 47.35)
        self.assertAlmostEqual(metadata["focal_length_35mm_mm"], 400.0)

    def test_estimated_subject_box_size_for_candidate_returns_none_when_missing_metadata(self) -> None:
        candidate = {
            "bbox_x1": 1,
            "bbox_y1": 2,
            "bbox_x2": 11,
            "bbox_y2": 22,
        }
        with patch("src.subject_size_estimate.load_subject_size_metadata", return_value={}):
            self.assertIsNone(estimated_subject_box_size_for_candidate("/tmp/a.ARW", candidate))


if __name__ == "__main__":
    unittest.main()
