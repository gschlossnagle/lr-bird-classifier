from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.review_apply import ApplyEngine, ApplyPolicy, consolidate_image_outcome, make_image_key
from src.review_apply_state import ReviewApplyState
from src.review_store import ReviewStore


class ReviewApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.review_db = self.tmpdir / "review.sqlite"
        self.apply_db = self.tmpdir / "apply.sqlite"
        self.catalog_path = self.tmpdir / "catalog.lrcat"
        self.catalog_path.write_text("", encoding="utf-8")
        self.preview_path = self.tmpdir / "cand.jpg"
        self.preview_path.write_bytes(b"preview")
        self.source_path = self.tmpdir / "source.ARW"
        self.source_path.write_text("", encoding="utf-8")
        self.store = ReviewStore(self.review_db)
        self.addCleanup(self.store.close)

    def _seed_labeled_scope(self, *, scope_key: str = "scope_apply", truth_label: str = "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus", common_name: str = "Bald Eagle") -> None:
        self.store.ensure_scope(
            scope_key=scope_key,
            scope_name="Catalog / Apply",
            catalog_name="Catalog",
            catalog_path=str(self.catalog_path),
            trip_folder="Apply",
            workflow_type="run_hybrid_review",
            created_at="2026-04-29T10:00:00Z",
        )
        image_id = self.store.insert_image(
            {
                "scope_key": scope_key,
                "source_image_id": 123,
                "source_image_path": str(self.source_path.resolve()),
                "capture_datetime": "2026-04-29T10:00:00Z",
                "folder": str(self.tmpdir.resolve()),
                "created_at": "2026-04-29T10:00:00Z",
            }
        )
        self.store.insert_candidate(
            {
                "candidate_id": "runimg_123",
                "image_id": image_id,
                "detector_name": "seeded",
                "detected_class": "bird",
                "detector_confidence": 0.61,
                "bbox_x1": 0,
                "bbox_y1": 0,
                "bbox_x2": 10,
                "bbox_y2": 10,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-29T10:00:00Z",
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "runimg_123",
                "annotation_status": "labeled",
                "truth_common_name": common_name,
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": truth_label,
                "taxon_class": "Aves",
                "resolved_from_input": common_name,
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-29T10:01:00Z",
            }
        )

    def test_consolidate_image_outcome_conflict(self) -> None:
        rows = [
            {
                "catalog_path": str(self.catalog_path),
                "scope_key": "scope_a",
                "source_image_id": 1,
                "source_image_path": "/tmp/a.ARW",
                "workflow_type": "run_hybrid_review",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "stress": False,
            },
            {
                "catalog_path": str(self.catalog_path),
                "scope_key": "scope_a",
                "source_image_id": 1,
                "source_image_path": "/tmp/a.ARW",
                "workflow_type": "run_hybrid_review",
                "annotation_status": "labeled",
                "truth_common_name": "Osprey",
                "truth_sci_name": "Pandion haliaetus",
                "truth_label": "99999_Animalia_Chordata_Aves_Accipitriformes_Pandionidae_Pandion_haliaetus",
                "stress": False,
            },
        ]
        outcome = consolidate_image_outcome(rows, ApplyPolicy())
        self.assertEqual(outcome.status, "conflict")

    def test_consolidate_image_outcome_excluding_stress_can_yield_no_label(self) -> None:
        rows = [
            {
                "catalog_path": str(self.catalog_path),
                "scope_key": "scope_a",
                "source_image_id": 1,
                "source_image_path": "/tmp/a.ARW",
                "workflow_type": "run_hybrid_review",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "stress": True,
            }
        ]
        outcome = consolidate_image_outcome(rows, ApplyPolicy(include_stress=False))
        self.assertEqual(outcome.status, "no_label")

    @patch("src.review_apply.ClassificationLog")
    @patch("src.review_apply.LightroomCatalog.open")
    def test_engine_dry_run_reports_apply(self, mocked_open, mocked_clf_log) -> None:
        self._seed_labeled_scope()
        fake_cat = Mock()
        fake_cat.__enter__ = Mock(return_value=fake_cat)
        fake_cat.__exit__ = Mock(return_value=None)
        fake_cat.get_managed_keyword_names_for_image.return_value = set()
        mocked_open.return_value = fake_cat
        fake_log = Mock()
        fake_log.get_all_rows.return_value = []
        mocked_clf_log.return_value = fake_log

        with ReviewApplyState(self.apply_db) as apply_state:
            engine = ApplyEngine(
                review_store=self.store,
                apply_state=apply_state,
                catalog_path=self.catalog_path,
                review_db_path=self.review_db,
                policy=ApplyPolicy(dry_run=True),
            )
            summary = engine.run()

        self.assertEqual(summary["considered"], 1)
        self.assertEqual(summary["would_apply"], 1)
        self.assertEqual(summary["applied"], 0)

    @patch("src.review_apply.ClassificationLog")
    @patch("src.review_apply.LightroomCatalog.open")
    def test_engine_writes_jsonl_report(self, mocked_open, mocked_clf_log) -> None:
        self._seed_labeled_scope()
        report_path = self.tmpdir / "apply-report.jsonl"
        fake_cat = Mock()
        fake_cat.__enter__ = Mock(return_value=fake_cat)
        fake_cat.__exit__ = Mock(return_value=None)
        fake_cat.get_managed_keyword_names_for_image.return_value = set()
        mocked_open.return_value = fake_cat
        fake_log = Mock()
        fake_log.get_all_rows.return_value = []
        mocked_clf_log.return_value = fake_log

        with ReviewApplyState(self.apply_db) as apply_state:
            engine = ApplyEngine(
                review_store=self.store,
                apply_state=apply_state,
                catalog_path=self.catalog_path,
                review_db_path=self.review_db,
                policy=ApplyPolicy(dry_run=True, report_path=str(report_path)),
            )
            summary = engine.run()

        self.assertEqual(summary["would_apply"], 1)
        rows = [json.loads(line) for line in report_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["result"], "would_apply")
        self.assertEqual(rows[0]["status"], "apply")
        self.assertEqual(rows[0]["source_image_path"], str(self.source_path.resolve()))

    @patch("src.review_apply.ClassificationLog")
    @patch("src.review_apply.LightroomCatalog.open")
    def test_engine_verified_skip_when_catalog_matches(self, mocked_open, mocked_clf_log) -> None:
        self._seed_labeled_scope()
        fake_cat = Mock()
        fake_cat.__enter__ = Mock(return_value=fake_cat)
        fake_cat.__exit__ = Mock(return_value=None)
        fake_cat.get_managed_keyword_names_for_image.return_value = {
            "Bald Eagle",
            "Hawks-Eagles-Kites-Allies",
            "Accipitriformes",
            "Accipitridae",
            "Haliaeetus leucocephalus",
            "Very High",
            "manually classed",
        }
        fake_cat.ensure_bird_species_keyword.return_value = 11
        fake_cat.ensure_species_keyword.return_value = (21, 22)
        fake_cat.ensure_scientific_keywords.return_value = (31, 32, 33)
        fake_cat.ensure_confidence_keyword.return_value = 41
        fake_cat.ensure_manually_classed_keyword.return_value = 51
        mocked_open.return_value = fake_cat
        fake_log = Mock()
        fake_log.get_all_rows.return_value = []
        mocked_clf_log.return_value = fake_log

        with ReviewApplyState(self.apply_db) as apply_state:
            engine = ApplyEngine(
                review_store=self.store,
                apply_state=apply_state,
                catalog_path=self.catalog_path,
                review_db_path=self.review_db,
                policy=ApplyPolicy(),
            )
            first = engine.run()
            second = engine.run()

        self.assertEqual(first["verified_skipped"], 1)
        self.assertEqual(second["verified_skipped"], 1)

    @patch("src.review_apply.write_sidecar_species_labels")
    @patch("src.review_apply.replace_catalog_species_labels")
    @patch("src.review_apply.ClassificationLog")
    @patch("src.review_apply.LightroomCatalog.open")
    def test_engine_repairs_when_catalog_differs(
        self,
        mocked_open,
        mocked_clf_log,
        mocked_replace_catalog_labels,
        mocked_write_sidecar,
    ) -> None:
        self._seed_labeled_scope()
        desired_names = {
            "Bald Eagle",
            "Hawks-Eagles-Kites-Allies",
            "Accipitriformes",
            "Accipitridae",
            "Haliaeetus leucocephalus",
            "Very High",
            "manually classed",
        }
        fake_cat = Mock()
        fake_cat.__enter__ = Mock(return_value=fake_cat)
        fake_cat.__exit__ = Mock(return_value=None)
        fake_cat.get_managed_keyword_names_for_image.side_effect = [
            {"Old Label"},
            desired_names,
        ]
        mocked_open.return_value = fake_cat
        fake_log = Mock()
        fake_log.get_all_rows.return_value = []
        mocked_clf_log.return_value = fake_log

        with ReviewApplyState(self.apply_db) as apply_state:
            engine = ApplyEngine(
                review_store=self.store,
                apply_state=apply_state,
                catalog_path=self.catalog_path,
                review_db_path=self.review_db,
                policy=ApplyPolicy(),
            )
            summary = engine.run()
            state = apply_state.get_image_state(
                make_image_key(
                    catalog_path=str(self.catalog_path),
                    source_image_id=123,
                    source_image_path=str(self.source_path.resolve()),
                )
            )

        self.assertEqual(summary["repaired"], 1)
        mocked_replace_catalog_labels.assert_called_once()
        mocked_write_sidecar.assert_called_once()
        assert state is not None
        self.assertEqual(state["status"], "repair")

    @patch("src.review_apply.ClassificationLog")
    @patch("src.review_apply.LightroomCatalog.open")
    def test_engine_errors_when_source_image_id_is_missing(self, mocked_open, mocked_clf_log) -> None:
        self.store.ensure_scope(
            scope_key="scope_missing_id",
            scope_name="Catalog / Missing",
            catalog_name="Catalog",
            catalog_path=str(self.catalog_path),
            trip_folder="Apply",
            workflow_type="run_hybrid_review",
            created_at="2026-04-29T10:00:00Z",
        )
        image_id = self.store.insert_image(
            {
                "scope_key": "scope_missing_id",
                "source_image_path": str(self.source_path.resolve()),
                "capture_datetime": "2026-04-29T10:00:00Z",
                "folder": str(self.tmpdir.resolve()),
                "created_at": "2026-04-29T10:00:00Z",
            }
        )
        self.store.insert_candidate(
            {
                "candidate_id": "runimg_missing",
                "image_id": image_id,
                "detector_name": "seeded",
                "detected_class": "bird",
                "detector_confidence": 0.61,
                "bbox_x1": 0,
                "bbox_y1": 0,
                "bbox_x2": 10,
                "bbox_y2": 10,
                "preview_image_path": str(self.preview_path.resolve()),
                "review_status": "unreviewed",
                "created_at": "2026-04-29T10:00:00Z",
            }
        )
        self.store.upsert_annotation(
            {
                "candidate_id": "runimg_missing",
                "annotation_status": "labeled",
                "truth_common_name": "Bald Eagle",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "taxon_class": "Aves",
                "resolved_from_input": "Bald Eagle",
                "stress": False,
                "reject_sample": False,
                "unsure": False,
                "not_a_bird": False,
                "bad_crop": False,
                "duplicate_sample": False,
                "annotated_at": "2026-04-29T10:01:00Z",
            }
        )
        fake_cat = Mock()
        fake_cat.__enter__ = Mock(return_value=fake_cat)
        fake_cat.__exit__ = Mock(return_value=None)
        mocked_open.return_value = fake_cat
        fake_log = Mock()
        fake_log.get_all_rows.return_value = []
        mocked_clf_log.return_value = fake_log

        with ReviewApplyState(self.apply_db) as apply_state:
            engine = ApplyEngine(
                review_store=self.store,
                apply_state=apply_state,
                catalog_path=self.catalog_path,
                review_db_path=self.review_db,
                policy=ApplyPolicy(scope_key="scope_missing_id"),
            )
            summary = engine.run()

        self.assertEqual(summary["errors"], 1)
