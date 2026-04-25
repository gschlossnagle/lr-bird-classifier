from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sqlite3

from src.export_manifest import ManifestExporter
from src.label_resolver import AmbiguousLabelError, LabelResolver, UnknownLabelError
from src.review_queue import QueueFilters, ReviewQueue
from src.review_store import ReviewStore
from src.review_validation import ValidationError, validate_annotation_row, validate_candidate_row


class ReviewStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.db_path = self.tmpdir / "review.sqlite"
        self.preview_path = self.tmpdir / "cand_000001.jpg"
        self.preview_path.write_bytes(b"preview")
        self.store = ReviewStore(self.db_path)
        self.addCleanup(self.store.close)

        self.image_id = self.store.insert_image(
            {
                "source_image_id": 123456,
                "source_image_path": str((self.tmpdir / "source.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:00:00Z",
                "folder": str(self.tmpdir.resolve()),
                "rating": 3.0,
                "region_hint": "us_northeast",
                "existing_keywords": ["Birds|Order|Hawks-Eagles-Kites-Allies"],
                "burst_group_id": "burst_001",
                "created_at": "2026-04-22T17:00:00Z",
            }
        )

    def test_insert_and_fetch_candidate(self) -> None:
        inserted = self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "bbox_area_fraction": 0.12,
                "preview_image_path": str(self.preview_path.resolve()),
                "safe_single_subject_burst": True,
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        self.assertTrue(inserted)

        candidate = self.store.get_candidate("cand_000001")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["review_status"], "unreviewed")
        self.assertEqual(candidate["preview_image_path"], str(self.preview_path.resolve()))
        self.assertEqual(self.store.count_candidates(review_status="unreviewed"), 1)
        self.assertEqual(self.store.count_candidate_images(review_status="unreviewed"), 1)
        self.assertEqual(self.store.queue_position("cand_000001", review_status="unreviewed"), (1, 1))

    def test_insert_candidate_returns_false_for_existing_id(self) -> None:
        row = {
            "candidate_id": "cand_000001",
            "image_id": self.image_id,
            "detector_name": "yolo-bird-v1",
            "detected_class": "bird",
            "detector_confidence": 0.93,
            "bbox_x1": 10,
            "bbox_y1": 20,
            "bbox_x2": 110,
            "bbox_y2": 220,
            "bbox_area_fraction": 0.12,
            "preview_image_path": str(self.preview_path.resolve()),
            "safe_single_subject_burst": True,
            "review_status": "unreviewed",
            "created_at": "2026-04-22T17:01:00Z",
        }
        self.assertTrue(self.store.insert_candidate(row))
        self.assertFalse(self.store.insert_candidate(row))

    def test_insert_image_reuses_existing_row(self) -> None:
        same_id = self.store.insert_image(
            {
                "source_image_id": 123456,
                "source_image_path": str((self.tmpdir / "source.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:00:00Z",
                "folder": str(self.tmpdir.resolve()),
                "rating": 3.0,
                "region_hint": "us_northeast",
                "created_at": "2026-04-22T17:00:00Z",
            }
        )
        self.assertEqual(same_id, self.image_id)

    def test_delete_images_by_source_paths_removes_related_rows(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_delete_1",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_delete_1",
                "annotation_status": "labeled",
                "truth_common_name": "Roseate Spoonbill",
                "truth_sci_name": "Platalea ajaja",
                "truth_label": "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
                "taxon_class": "Aves",
                "resolved_from_input": "roseate spoonbill",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )

        deleted = self.store.delete_images_by_source_paths([str((self.tmpdir / "source.ARW").resolve())])
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.store.get_candidate("cand_delete_1"))
        self.assertIsNone(self.store.get_annotation("cand_delete_1"))
        self.assertIsNone(self.store.get_image(self.image_id))

    def test_recent_labels_dedupes_by_truth_label(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000002",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.94,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:01Z",
            }
        )
        common = {
            "annotation_status": "labeled",
            "truth_common_name": "Roseate Spoonbill",
            "truth_sci_name": "Platalea ajaja",
            "truth_label": "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
            "taxon_class": "Aves",
            "stress": False,
            "reject_sample": False,
            "unsure": False,
            "not_a_bird": False,
            "bad_crop": False,
            "duplicate_sample": False,
        }
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000001",
                "resolved_from_input": "roseate spoonbill",
                "annotated_at": "2026-04-22T17:05:00Z",
                **common,
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000002",
                "resolved_from_input": "Roseate Spoonbill",
                "annotated_at": "2026-04-22T17:06:00Z",
                **common,
            }
        )
        recent = self.store.recent_labels()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["truth_common_name"], "Roseate Spoonbill")

    def test_recent_labels_looks_past_long_run_of_duplicates(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_recent_a",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        self.store.insert_candidate(
            {
                "candidate_id": "cand_recent_b",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.94,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:01Z",
            }
        )
        for idx in range(30):
            self.store._append_label_history(  # test helper path
                {
                    "truth_common_name": "Boat-Tailed Grackle",
                    "truth_sci_name": "Quiscalus major",
                    "truth_label": "03898_Animalia_Chordata_Aves_Passeriformes_Icteridae_Quiscalus_major",
                    "taxon_class": "Aves",
                    "annotated_at": f"2026-04-22T17:05:{idx:02d}Z",
                }
            )
        self.store._append_label_history(
            {
                "truth_common_name": "Roseate Spoonbill",
                "truth_sci_name": "Platalea ajaja",
                "truth_label": "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
                "taxon_class": "Aves",
                "annotated_at": "2026-04-22T17:04:00Z",
            }
        )
        recent = self.store.recent_labels(limit=2)
        self.assertEqual([row["truth_common_name"] for row in recent], ["Boat-Tailed Grackle", "Roseate Spoonbill"])

    def test_previous_reviewed_candidate_returns_prior_review(self) -> None:
        for idx, ts in enumerate(["2026-04-22T17:01:00Z", "2026-04-22T17:02:00Z"], start=1):
            image_id = self.store.insert_image(
                {
                    "source_image_id": 200000 + idx,
                    "source_image_path": str((self.tmpdir / f"source_{idx}.ARW").resolve()),
                    "capture_datetime": ts,
                    "folder": str(self.tmpdir.resolve()),
                    "created_at": ts,
                }
            )
            self.store.insert_candidate(
                {
                    "candidate_id": f"cand_prev_{idx}",
                    "image_id": image_id,
                    "detector_name": "yolo-bird-v1",
                    "detected_class": "bird",
                    "detector_confidence": 0.93,
                    "bbox_x1": 10,
                    "bbox_y1": 20,
                    "bbox_x2": 110,
                    "bbox_y2": 220,
                    "preview_image_path": str(self.preview_path.resolve()),
                    "review_status": "unreviewed",
                    "created_at": ts,
                }
            )
            self.store.upsert_annotation(
                {
                    "candidate_id": f"cand_prev_{idx}",
                    "annotation_status": "labeled",
                    "truth_common_name": "Roseate Spoonbill",
                    "truth_sci_name": "Platalea ajaja",
                    "truth_label": "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
                    "taxon_class": "Aves",
                    "resolved_from_input": "roseate spoonbill",
                    "stress": False,
                    "reject_sample": False,
                    "unsure": False,
                    "not_a_bird": False,
                    "bad_crop": False,
                    "duplicate_sample": False,
                    "annotated_at": ts,
                }
            )
        prev = self.store.previous_reviewed_candidate("cand_prev_2")
        self.assertIsNotNone(prev)
        assert prev is not None
        self.assertEqual(prev["id"], "cand_prev_1")

    def test_apply_annotation_to_burst_updates_other_targets(self) -> None:
        first_id = self.store.insert_image(
            {
                "source_image_id": 300001,
                "source_image_path": str((self.tmpdir / "burst1.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:01:00Z",
                "folder": str(self.tmpdir.resolve()),
                "burst_group_id": "burst_1",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        second_id = self.store.insert_image(
            {
                "source_image_id": 300002,
                "source_image_path": str((self.tmpdir / "burst2.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:01:00.500000Z",
                "folder": str(self.tmpdir.resolve()),
                "burst_group_id": "burst_1",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        for candidate_id, image_id in [("cand_burst_1", first_id), ("cand_burst_2", second_id)]:
            self.store.insert_candidate(
                {
                    "candidate_id": candidate_id,
                    "image_id": image_id,
                    "detector_name": "yolo-bird-v1",
                    "detected_class": "bird",
                    "detector_confidence": 0.93,
                    "bbox_x1": 10,
                    "bbox_y1": 20,
                    "bbox_x2": 110,
                    "bbox_y2": 220,
                    "preview_image_path": str(self.preview_path.resolve()),
                    "review_status": "unreviewed",
                    "safe_single_subject_burst": True,
                    "created_at": "2026-04-22T17:01:00Z",
                }
            )
        row = {
            "candidate_id": "cand_burst_1",
            "annotation_status": "labeled",
            "truth_common_name": "Roseate Spoonbill",
            "truth_sci_name": "Platalea ajaja",
            "truth_label": "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
            "taxon_class": "Aves",
            "resolved_from_input": "roseate spoonbill",
            "stress": False,
            "reject_sample": False,
            "unsure": False,
            "not_a_bird": False,
            "bad_crop": False,
            "duplicate_sample": False,
            "annotated_at": "2026-04-22T17:05:00Z",
        }
        count = self.store.apply_annotation_to_burst("cand_burst_1", row)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.get_annotation("cand_burst_2")["truth_common_name"], "Roseate Spoonbill")

    def test_burst_position_returns_order_and_size(self) -> None:
        first_id = self.store.insert_image(
            {
                "source_image_id": 300011,
                "source_image_path": str((self.tmpdir / "burst_pos_1.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:01:00Z",
                "folder": str(self.tmpdir.resolve()),
                "burst_group_id": "burst_pos",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        second_id = self.store.insert_image(
            {
                "source_image_id": 300012,
                "source_image_path": str((self.tmpdir / "burst_pos_2.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:01:00.500000Z",
                "folder": str(self.tmpdir.resolve()),
                "burst_group_id": "burst_pos",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        third_id = self.store.insert_image(
            {
                "source_image_id": 300013,
                "source_image_path": str((self.tmpdir / "burst_pos_3.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:01:00.700000Z",
                "folder": str(self.tmpdir.resolve()),
                "burst_group_id": "burst_pos",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        for candidate_id, image_id in [
            ("cand_burst_pos_1", first_id),
            ("cand_burst_pos_2", second_id),
            ("cand_burst_pos_3", third_id),
        ]:
            self.store.insert_candidate(
                {
                    "candidate_id": candidate_id,
                    "image_id": image_id,
                    "detector_name": "yolo-bird-v1",
                    "detected_class": "bird",
                    "detector_confidence": 0.93,
                    "bbox_x1": 10,
                    "bbox_y1": 20,
                    "bbox_x2": 110,
                    "bbox_y2": 220,
                    "preview_image_path": str(self.preview_path.resolve()),
                    "review_status": "unreviewed",
                    "safe_single_subject_burst": True,
                    "created_at": "2026-04-22T17:01:00Z",
                }
            )

        self.assertEqual(self.store.burst_position("cand_burst_pos_2"), (2, 3))

    def test_candidate_validation_rejects_invalid_bbox(self) -> None:
        with self.assertRaises(ValidationError):
            validate_candidate_row(
                {
                    "candidate_id": "cand_000001",
                    "image_id": self.image_id,
                    "detector_name": "yolo-bird-v1",
                    "detected_class": "bird",
                    "detector_confidence": 0.93,
                    "bbox_x1": 20,
                    "bbox_y1": 20,
                    "bbox_x2": 20,
                    "bbox_y2": 220,
                    "preview_image_path": str(self.preview_path.resolve()),
                    "review_status": "unreviewed",
                    "created_at": "2026-04-22T17:01:00Z",
                }
            )

    def test_upsert_annotation_replaces_prior_outcome(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )

        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000001",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "bald eagle",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )
        first = self.store.get_annotation("cand_000001")
        self.assertEqual(first["annotation_status"], "labeled")
        self.assertEqual(self.store.get_candidate("cand_000001")["review_status"], "reviewed")
        self.assertEqual(len(self.store.recent_labels()), 1)

        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000001",
                "annotation_status": "duplicate",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": True,
                "annotated_at": "2026-04-22T17:06:00Z",
            }
        )
        second = self.store.get_annotation("cand_000001")
        self.assertEqual(second["annotation_status"], "duplicate")
        self.assertTrue(second["duplicate_sample"])
        self.assertEqual(len(self.store.recent_labels()), 1)

    def test_skip_candidate_does_not_create_annotation(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        self.store.mark_candidate_skipped("cand_000001")
        self.assertEqual(self.store.get_candidate("cand_000001")["review_status"], "skipped")
        self.assertIsNone(self.store.get_annotation("cand_000001"))

    def test_reset_review_state_clears_annotations_and_status(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000001",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "bald eagle",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )
        self.assertEqual(self.store.get_candidate("cand_000001")["review_status"], "reviewed")
        self.assertEqual(len(self.store.recent_labels()), 1)

        self.store.reset_review_state()

        self.assertIsNone(self.store.get_annotation("cand_000001"))
        self.assertEqual(self.store.get_candidate("cand_000001")["review_status"], "unreviewed")
        self.assertEqual(self.store.recent_labels(), [])

    def test_summary_counts_reports_species_and_outcomes(self) -> None:
        for candidate_id in ["cand_sum_1", "cand_sum_2", "cand_sum_3"]:
            self.store.insert_candidate(
                {
                    "candidate_id": candidate_id,
                    "image_id": self.image_id,
                    "detector_name": "yolo-bird-v1",
                    "detected_class": "bird",
                    "detector_confidence": 0.93,
                    "bbox_x1": 10,
                    "bbox_y1": 20,
                    "bbox_x2": 110,
                    "bbox_y2": 220,
                    "preview_image_path": str(self.preview_path.resolve()),
                    "review_status": "unreviewed",
                    "created_at": "2026-04-22T17:01:00Z",
                }
            )

        self.store.upsert_annotation(
            {
                "candidate_id": "cand_sum_1",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "bald eagle",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_sum_2",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "bald eagle",
                "stress": True,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:06:00Z",
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_sum_3",
                "annotation_status": "reject",
                "stress": False,
                "reject_sample": True,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:07:00Z",
            }
        )

        summary = self.store.summary_counts()
        self.assertEqual(summary["overview"]["reviewed"], 3)
        self.assertEqual(summary["outcomes"]["labeled"], 2)
        self.assertEqual(summary["outcomes"]["reject"], 1)
        self.assertEqual(len(summary["species"]), 1)
        self.assertEqual(summary["species"][0]["truth_common_name"], "Bald Eagle")
        self.assertEqual(summary["species"][0]["normal_count"], 1)
        self.assertEqual(summary["species"][0]["stress_count"], 1)

    def test_extraction_cursor_round_trip(self) -> None:
        self.assertIsNone(self.store.get_extraction_cursor("scope-a"))
        self.store.set_extraction_cursor("scope-a", "123", "2026-04-22T17:05:00Z")
        self.assertEqual(self.store.get_extraction_cursor("scope-a"), "123")
        self.store.clear_extraction_cursor("scope-a")
        self.assertIsNone(self.store.get_extraction_cursor("scope-a"))

    def test_migrates_v1_schema_to_v2(self) -> None:
        legacy_db = self.tmpdir / "legacy.sqlite"
        conn = sqlite3.connect(legacy_db)
        conn.executescript(
            """
            CREATE TABLE images (
                id INTEGER PRIMARY KEY,
                source_image_id INTEGER,
                source_image_path TEXT NOT NULL UNIQUE,
                capture_datetime TEXT,
                folder TEXT,
                rating REAL,
                gps_lat REAL,
                gps_lon REAL,
                region_hint TEXT,
                lens_model TEXT,
                focal_length REAL,
                existing_keywords TEXT,
                burst_group_id TEXT,
                near_duplicate_group TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE candidates (
                id TEXT PRIMARY KEY,
                image_id INTEGER NOT NULL,
                detector_name TEXT NOT NULL,
                detected_class TEXT NOT NULL,
                detector_confidence REAL NOT NULL,
                bbox_x1 INTEGER NOT NULL,
                bbox_y1 INTEGER NOT NULL,
                bbox_x2 INTEGER NOT NULL,
                bbox_y2 INTEGER NOT NULL,
                bbox_area_fraction REAL,
                preview_image_path TEXT NOT NULL,
                safe_single_subject_burst INTEGER NOT NULL DEFAULT 0,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                reviewed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE annotations (
                candidate_id TEXT PRIMARY KEY,
                annotation_status TEXT NOT NULL,
                truth_common_name TEXT,
                truth_sci_name TEXT,
                truth_label TEXT,
                taxon_class TEXT,
                resolved_from_input TEXT,
                stress INTEGER NOT NULL DEFAULT 0,
                reject_sample INTEGER NOT NULL DEFAULT 0,
                unsure INTEGER NOT NULL DEFAULT 0,
                not_a_bird INTEGER NOT NULL DEFAULT 0,
                bad_crop INTEGER NOT NULL DEFAULT 0,
                duplicate_sample INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                annotated_by TEXT,
                annotated_at TEXT NOT NULL
            );
            CREATE TABLE label_history (
                id INTEGER PRIMARY KEY,
                truth_common_name TEXT NOT NULL,
                truth_sci_name TEXT NOT NULL,
                truth_label TEXT NOT NULL,
                taxon_class TEXT,
                used_at TEXT NOT NULL
            );
            CREATE TABLE review_sessions (
                id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                user_id TEXT,
                queue_filter TEXT,
                notes TEXT
            );
            PRAGMA user_version = 1;
            """
        )
        conn.commit()
        conn.close()

        with ReviewStore(legacy_db) as migrated:
            migrated.set_extraction_cursor("scope-a", "123", "2026-04-22T17:05:00Z")
            self.assertEqual(migrated.get_extraction_cursor("scope-a"), "123")

    def test_queue_opens_unreviewed_candidate(self) -> None:
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )
        queue = ReviewQueue(self.store, QueueFilters(review_status="unreviewed"))
        candidate = queue.next_candidate()
        self.assertEqual(candidate["id"], "cand_000001")
        opened = queue.open_candidate("cand_000001")
        self.assertEqual(opened["review_status"], "in_review")


class AnnotationValidationTest(unittest.TestCase):
    def test_reject_style_flags_must_match_status(self) -> None:
        with self.assertRaises(ValidationError):
            validate_annotation_row(
                {
                    "candidate_id": "cand_000001",
                    "annotation_status": "reject",
                    "stress": False,
                    "reject_sample": True,
                    "unsure": True,
                    "not_a_bird": False,
                    "bad_crop": False,
                    "duplicate_sample": False,
                    "annotated_at": "2026-04-22T17:06:00Z",
                }
            )


class LabelResolverTest(unittest.TestCase):
    def test_resolve_exact_common_name(self) -> None:
        resolver = LabelResolver(
            [
                "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "02286_Animalia_Chordata_Aves_Passeriformes_Hirundinidae_Hirundo_rustica",
            ]
        )
        resolved = resolver.resolve_common_name("Bald Eagle")
        self.assertEqual(resolved.truth_sci_name, "Haliaeetus leucocephalus")

    def test_unknown_name_raises(self) -> None:
        resolver = LabelResolver(
            ["00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus"]
        )
        with self.assertRaisesRegex(UnknownLabelError, "Fix the name or enter a scientific name"):
            resolver.resolve_common_name("Imaginary Bird")

    def test_ambiguous_name_raises(self) -> None:
        resolver = LabelResolver(
            [
                "00001_Animalia_Chordata_Aves_TestOrder_TestFamily_Genus_species",
                "00002_Animalia_Chordata_Aves_TestOrder_TestFamily_Othergenus_species",
            ]
        )
        resolver._labels_by_normalized_name["shared name"] = [  # type: ignore[attr-defined]
            resolver.resolve_recent_label(
                "00001_Animalia_Chordata_Aves_TestOrder_TestFamily_Genus_species"
            ),
            resolver.resolve_recent_label(
                "00002_Animalia_Chordata_Aves_TestOrder_TestFamily_Othergenus_species"
            ),
        ]
        with self.assertRaises(AmbiguousLabelError):
            resolver.resolve_common_name("shared name")

    def test_canonicalizes_common_name_case(self) -> None:
        resolver = LabelResolver(
            ["04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja"]
        )
        lower = resolver.resolve_common_name("roseate spoonbill")
        title = resolver.resolve_common_name("Roseate Spoonbill")
        self.assertEqual(lower.truth_label, title.truth_label)
        self.assertEqual(lower.truth_common_name, "Roseate Spoonbill")

    def test_accepts_compact_common_name_variants(self) -> None:
        resolver = LabelResolver(
            [
                "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
                "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
            ]
        )
        spoonbill = resolver.resolve_common_name("roseatespoonbill")
        eagle = resolver.resolve_common_name("baldeagle")
        self.assertEqual(spoonbill.truth_common_name, "Roseate Spoonbill")
        self.assertEqual(eagle.truth_common_name, "Bald Eagle")


class ManifestExporterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.db_path = self.tmpdir / "review.sqlite"
        self.preview_path = self.tmpdir / "cand_000001.jpg"
        self.preview_path.write_bytes(b"preview")
        self.store = ReviewStore(self.db_path)
        self.addCleanup(self.store.close)
        self.image_id = self.store.insert_image(
            {
                "source_image_id": 123456,
                "source_image_path": str((self.tmpdir / "source.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:00:00Z",
                "region_hint": "us_northeast",
                "created_at": "2026-04-22T17:00:00Z",
            }
        )
        self.store.insert_candidate(
            {
                "candidate_id": "cand_000001",
                "image_id": self.image_id,
                "detector_name": "yolo-bird-v1",
                "detected_class": "bird",
                "detector_confidence": 0.93,
                "bbox_x1": 10,
                "bbox_y1": 20,
                "bbox_x2": 110,
                "bbox_y2": 220,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )

    def test_exports_real_world_row(self) -> None:
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000001",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "bald eagle",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )
        exporter = ManifestExporter(self.store)
        rows = exporter.export_rows("catalog_real_world")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["truth_common_name"], "Bald Eagle")

    def test_materialized_export_writes_manifest_and_image(self) -> None:
        self.store.upsert_annotation(
            {
                "candidate_id": "cand_000001",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "bald eagle",
                "stress": True,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )
        exporter = ManifestExporter(self.store)
        outdir = self.tmpdir / "dataset"
        count = exporter.export_materialized_dataset(
            "catalog_stress",
            outdir,
            export_max_size_bytes=1024,
            export_quality_floor=75,
        )
        self.assertEqual(count, 1)
        self.assertTrue((outdir / "catalog_stress.jsonl").exists())
        self.assertTrue((outdir / "images" / "catalog-stress-000001.jpg").exists())


if __name__ == "__main__":
    unittest.main()
