from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.eval_manifest import load_manifest
from src.export_manifest import ManifestExporter
from src.review_store import ReviewStore


class ExportManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self.db_path = self.tmpdir / "review.sqlite"
        self.preview_path = self.tmpdir / "cand_000001.jpg"
        self.preview_path.write_bytes(b"preview")
        self.store = ReviewStore(self.db_path)
        self.addCleanup(self.store.close)
        self.exporter = ManifestExporter(self.store)

        self.image_id = self.store.insert_image(
            {
                "source_image_id": 100,
                "source_image_path": str((self.tmpdir / "source.ARW").resolve()),
                "capture_datetime": "2026-04-22T17:00:00Z",
                "folder": str(self.tmpdir.resolve()),
                "region_hint": "us_southeast",
                "created_at": "2026-04-22T17:00:00Z",
            }
        )

    def _insert_candidate(self, candidate_id: str) -> None:
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

    def _insert_labeled_annotation(self, candidate_id: str, *, stress: bool) -> None:
        self.store.upsert_annotation(
            {
                "candidate_id": candidate_id,
                "annotation_status": "labeled",
                "truth_common_name": "Roseate Spoonbill",
                "truth_sci_name": "Platalea ajaja",
                "truth_label": "04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja",
                "taxon_class": "Aves",
                "resolved_from_input": "roseate spoonbill",
                "stress": stress,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-22T17:05:00Z",
            }
        )

    def test_export_rows_separates_real_world_and_stress(self) -> None:
        self._insert_candidate("cand_normal")
        self._insert_candidate("cand_stress")
        self._insert_labeled_annotation("cand_normal", stress=False)
        self._insert_labeled_annotation("cand_stress", stress=True)

        real_world = self.exporter.export_rows("catalog_real_world")
        stress = self.exporter.export_rows("catalog_stress")

        self.assertEqual(len(real_world), 1)
        self.assertEqual(real_world[0]["candidate_id"], "cand_normal")
        self.assertEqual(len(stress), 1)
        self.assertEqual(stress[0]["candidate_id"], "cand_stress")

    def test_export_materialized_dataset_writes_manifest_and_images(self) -> None:
        self._insert_candidate("cand_normal")
        self._insert_labeled_annotation("cand_normal", stress=False)

        outdir = self.tmpdir / "exported"
        count = self.exporter.export_materialized_dataset(
            "catalog_real_world",
            outdir,
            export_max_size_bytes=1_048_576,
            export_quality_floor=75,
        )

        self.assertEqual(count, 1)
        manifest_path = outdir / "catalog_real_world.jsonl"
        rows = load_manifest(manifest_path)
        self.assertEqual(len(rows), 1)
        exported_path = Path(rows[0]["exported_image_path"])
        self.assertTrue(exported_path.exists())
        self.assertEqual(rows[0]["export_max_size_bytes"], 1_048_576)
        self.assertEqual(rows[0]["export_quality_floor"], 75)


if __name__ == "__main__":
    unittest.main()
