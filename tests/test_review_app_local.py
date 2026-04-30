from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.review_app import QueueTopUpRunner, ReviewAppHandler, load_label_inventory, parse_args
from src.review_suggester import SuggestedLabel


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

    def test_run_hybrid_review_allows_only_save_and_skip(self) -> None:
        scope = {"workflow_type": "run_hybrid_review"}
        self.assertEqual(ReviewAppHandler._allowed_actions(scope), {"save", "skip"})

    def test_render_candidate_hides_expert_actions_for_run_hybrid_review(self) -> None:
        handler = object.__new__(ReviewAppHandler)
        scope = {
            "scope_key": "scope_hybrid",
            "scope_name": "Catalog / Hybrid",
            "catalog_name": "Catalog",
            "trip_folder": "/photos/Hybrid",
            "workflow_type": "run_hybrid_review",
        }
        candidate = {
            "id": "runimg_123",
            "detector_name": "seeded",
            "detector_confidence": 0.61,
        }
        image = {
            "source_image_path": "/photos/Hybrid/IMG_0001.ARW",
            "capture_datetime": "2026-04-29T10:00:00Z",
            "region_hint": "north_america",
            "burst_group_id": "",
        }
        html = handler._render_candidate(
            scope,
            candidate,
            image,
            annotation=None,
            recent=[],
            error="",
            prev_candidate=None,
            burst_target_count=2,
            burst_position=None,
            queue_position=(1, 4),
            unreviewed_images=4,
            unreviewed_candidates=4,
            suggestion=SuggestedLabel(
                truth_common_name="Bald Eagle",
                truth_sci_name="Haliaeetus leucocephalus",
                truth_label="00419_label",
                confidence=0.61,
            ),
            estimated_subject_box_size=None,
            suggestion_status=None,
            prefill_selected_truth_label="",
            prefill_label_input="",
            prefill_stress=False,
            prefill_notes="",
            stress_reason="",
        )
        self.assertIn("review_run", html)
        self.assertIn("Photo Stage", html)
        self.assertIn("Accept Suggestion", html)
        self.assertIn("Skip", html)
        self.assertIn("Type species common name (T)", html)
        self.assertIn("Backlog", html)
        self.assertIn("images / <strong>4</strong> candidates", html)
        self.assertNotIn("Stress", html)
        self.assertNotIn("Reject", html)
        self.assertNotIn("Not a Bird", html)
        self.assertNotIn("Not A Bird", html)
        self.assertNotIn("Accept + Burst", html)
        self.assertIn("data-copy-path=", html)

    def test_render_candidate_shows_expert_actions_and_updated_shortcuts(self) -> None:
        handler = object.__new__(ReviewAppHandler)
        scope = {
            "scope_key": "scope_detector",
            "scope_name": "Catalog / Detector",
            "catalog_name": "Catalog",
            "trip_folder": "/photos/Detector",
            "workflow_type": "detector_review",
        }
        candidate = {
            "id": "cand_123",
            "detector_name": "owlvit",
            "detector_confidence": 0.42,
        }
        image = {
            "source_image_path": "/photos/Detector/IMG_0002.ARW",
            "capture_datetime": "2026-04-29T11:00:00Z",
            "region_hint": "north_america",
            "burst_group_id": "burst-22",
            "lens_model": "FE 200-600mm",
            "focal_length": 600.0,
            "rating": 5,
        }
        html = handler._render_candidate(
            scope,
            candidate,
            image,
            annotation=None,
            recent=[],
            error="",
            prev_candidate=None,
            burst_target_count=0,
            burst_position=(1, 3),
            queue_position=(2, 5),
            unreviewed_images=5,
            unreviewed_candidates=6,
            suggestion=SuggestedLabel(
                truth_common_name="Great Blue Heron",
                truth_sci_name="Ardea herodias",
                truth_label="00999_label",
                confidence=0.42,
            ),
            estimated_subject_box_size=(41.2, 28.8),
            suggestion_status=None,
            prefill_selected_truth_label="",
            prefill_label_input="",
            prefill_stress=False,
            prefill_notes="",
            stress_reason="Classifier confidence is low (42.0%).",
        )
        self.assertIn("review_app", html)
        self.assertIn("Reject", html)
        self.assertIn("Unsure", html)
        self.assertIn("Not A Bird", html)
        self.assertIn(">S<", html)
        self.assertIn("Type species common name (T)", html)
        self.assertIn("Stress suggested", html)
        self.assertIn("Box Size", html)

    def test_render_candidate_shows_unavailable_suggestion_and_partial_details(self) -> None:
        handler = object.__new__(ReviewAppHandler)
        scope = {
            "scope_key": "scope_detector",
            "scope_name": "Catalog / Detector",
            "catalog_name": "Catalog",
            "trip_folder": "/photos/Detector",
            "workflow_type": "detector_review",
        }
        candidate = {
            "id": "cand_999",
            "detector_name": "owlvit",
            "detector_confidence": 0.17,
        }
        image = {
            "source_image_path": "/photos/Detector/IMG_9999.ARW",
            "capture_datetime": "2026-04-29T11:00:00Z",
            "region_hint": "",
        }
        with patch("src.review_app.load_subject_size_metadata", return_value={"focus_distance_m": 12.34}):
            html = handler._render_candidate(
                scope,
                candidate,
                image,
                annotation=None,
                recent=[],
                error="",
                prev_candidate=None,
                burst_target_count=0,
                burst_position=None,
                queue_position=(1, 1),
                unreviewed_images=1,
                unreviewed_candidates=1,
                suggestion=None,
                estimated_subject_box_size=None,
                suggestion_status="classifier unavailable",
                prefill_selected_truth_label="",
                prefill_label_input="Killdeer",
                prefill_stress=False,
                prefill_notes="watch later",
                stress_reason="",
            )
        self.assertIn("Suggestion unavailable", html)
        self.assertIn("classifier unavailable", html)
        self.assertIn("Focus Dist.", html)
        self.assertNotIn("35mm Eq.", html)
        self.assertIn("watch later", html)

    def test_render_summary_simplifies_run_hybrid_review_columns(self) -> None:
        scope = {"scope_key": "scope_hybrid", "scope_name": "Catalog / Hybrid", "workflow_type": "run_hybrid_review"}
        summary = {
            "overview": {"unreviewed": 1, "reviewed": 2, "skipped": 1},
            "outcomes": {"labeled": 2, "reject": 1},
            "species": [
                {
                    "truth_common_name": "Bald Eagle",
                    "truth_sci_name": "Haliaeetus leucocephalus",
                    "normal_count": 1,
                    "stress_count": 1,
                    "total_count": 2,
                }
            ],
        }
        html = ReviewAppHandler._render_summary(scope, summary, "runimg_123", {"running": False})
        self.assertIn("<th>Total</th>", html)
        self.assertNotIn("<th>Stress</th>", html)
        self.assertNotIn("Reject", html)
        self.assertIn("Bald Eagle", html)


if __name__ == "__main__":
    unittest.main()
