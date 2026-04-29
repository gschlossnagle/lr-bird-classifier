from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.review_run import _auto_apply_labels, main, parse_args


class ReviewRunParseArgsTest(unittest.TestCase):
    def test_required_review_run_args_parse(self) -> None:
        argv = [
            "review_run",
            "--catalog",
            "/tmp/catalog.lrcat",
            "--review-db",
            "/tmp/review.sqlite",
            "--preview-dir",
            "/tmp/previews",
            "--labels-file",
            "/tmp/labels.txt",
            "--apply-state-db",
            "/tmp/apply.sqlite",
            "--auto-apply-threshold",
            "0.9",
        ]
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.catalog, "/tmp/catalog.lrcat")
        self.assertEqual(args.auto_apply_threshold, 0.9)
        self.assertEqual(args.apply_state_db, "/tmp/apply.sqlite")

    def test_main_rejects_threshold_below_min_confidence(self) -> None:
        argv = [
            "review_run",
            "--catalog",
            "/tmp/catalog.lrcat",
            "--review-db",
            "/tmp/review.sqlite",
            "--preview-dir",
            "/tmp/previews",
            "--labels-file",
            "/tmp/labels.txt",
            "--apply-state-db",
            "/tmp/apply.sqlite",
            "--min-confidence",
            "0.5",
            "--auto-apply-threshold",
            "0.4",
        ]
        with patch("sys.argv", argv):
            with self.assertRaises(SystemExit):
                main()

    def test_auto_apply_uses_only_top_prediction(self) -> None:
        predictions = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.97,
            ),
            SimpleNamespace(
                label="99999_Animalia_Chordata_Aves_Accipitriformes_Pandionidae_Pandion_haliaetus",
                common_name="Osprey",
                sci_name="Pandion haliaetus",
                confidence=0.82,
            ),
        ]
        labels = _auto_apply_labels(predictions)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].common_name, "Bald Eagle")


if __name__ == "__main__":
    unittest.main()
