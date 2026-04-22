from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
                "bbox_area_fraction": 0.12,
                "preview_image_path": str(self.preview_path.resolve()),
                "safe_single_subject_burst": True,
                "review_status": "unreviewed",
                "created_at": "2026-04-22T17:01:00Z",
            }
        )

        candidate = self.store.get_candidate("cand_000001")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["review_status"], "unreviewed")
        self.assertEqual(candidate["preview_image_path"], str(self.preview_path.resolve()))

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
