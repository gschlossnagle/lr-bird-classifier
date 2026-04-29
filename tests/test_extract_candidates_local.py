from __future__ import annotations

import unittest
from unittest.mock import patch

from src.extract_candidates import parse_args


class ExtractCandidatesArgsTest(unittest.TestCase):
    def test_formats_flag_accepts_arbitrary_values(self) -> None:
        argv = [
            "extract_candidates",
            "--catalog",
            "/tmp/catalog.lrcat",
            "--db",
            "/tmp/review.sqlite",
            "--preview-dir",
            "/tmp/previews",
            "--detector",
            "package.module.Detector",
            "--formats",
            "RAW,PSD,VIDEO,PNG",
        ]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.formats, "RAW,PSD,VIDEO,PNG")

    def test_detector_model_flag_parses(self) -> None:
        argv = [
            "extract_candidates",
            "--catalog",
            "/tmp/catalog.lrcat",
            "--db",
            "/tmp/review.sqlite",
            "--preview-dir",
            "/tmp/previews",
            "--detector",
            "package.module.Detector",
            "--detector-model",
            "yolov8n.pt",
        ]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.detector_model, "yolov8n.pt")


if __name__ == "__main__":
    unittest.main()
