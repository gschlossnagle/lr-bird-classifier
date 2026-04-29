from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.review_app import QueueTopUpRunner, load_label_inventory, parse_args


class ReviewAppTest(unittest.TestCase):
    def test_load_label_inventory_from_plain_text(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "labels.txt"
        path.write_text("label_a\nlabel_b\nlabel_a\n", encoding="utf-8")
        self.assertEqual(load_label_inventory(path), ["label_a", "label_b"])

    def test_load_label_inventory_from_jsonl(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "labels.jsonl"
        path.write_text(
            '{"truth_label":"label_a"}\n{"truth_label":"label_b"}\n',
            encoding="utf-8",
        )
        self.assertEqual(load_label_inventory(path), ["label_a", "label_b"])

    def test_formats_flag_accepts_arbitrary_values(self) -> None:
        argv = [
            "review_app",
            "--db",
            "/tmp/review.sqlite",
            "--labels-file",
            "/tmp/labels.txt",
            "--formats",
            "RAW,PSD,VIDEO,PNG",
        ]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.formats, "RAW,PSD,VIDEO,PNG")

    def test_detector_model_flag_parses(self) -> None:
        argv = [
            "review_app",
            "--db",
            "/tmp/review.sqlite",
            "--labels-file",
            "/tmp/labels.txt",
            "--detector-model",
            "yolov8s.pt",
        ]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.detector_model, "yolov8s.pt")

    def test_queue_topup_runner_passes_detector_model(self) -> None:
        class FakeDetector:
            def __init__(self, model: str | None = None) -> None:
                self.model = model

        with patch("src.review_app._load_object", return_value=FakeDetector):
            runner = QueueTopUpRunner(
                catalog="/tmp/catalog.lrcat",
                detector_import_path="package.module.Detector",
                detector_model="yolov8m.pt",
                preview_dir="/tmp/previews",
                formats={"RAW"},
                folder=None,
                scope_folder=None,
                min_stars=None,
                batch_limit=10,
                max_preview_dimension=2048,
                jpeg_quality=85,
            )
        self.assertEqual(runner.extractor.detector.model, "yolov8m.pt")


if __name__ == "__main__":
    unittest.main()
