from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.review_run import _auto_apply_labels, _seed_deferred_candidate, main, parse_args
from src.review_store import ReviewStore


class ReviewRunParseArgsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.catalog_path = self.tmpdir / "catalog.lrcat"
        self.catalog_path.write_text("", encoding="utf-8")
        self.review_db = self.tmpdir / "review.sqlite"
        self.preview_dir = self.tmpdir / "previews"
        self.preview_dir.mkdir()
        self.labels_file = self.tmpdir / "labels.txt"
        self.labels_file.write_text("Bald Eagle\n", encoding="utf-8")
        self.apply_db = self.tmpdir / "apply.sqlite"
        self.image_path = self.tmpdir / "img1.ARW"
        self.image_path.write_text("", encoding="utf-8")

    def _argv(self, *extra: str) -> list[str]:
        return [
            "review_run",
            "--catalog",
            str(self.catalog_path),
            "--review-db",
            str(self.review_db),
            "--preview-dir",
            str(self.preview_dir),
            "--labels-file",
            str(self.labels_file),
            "--apply-state-db",
            str(self.apply_db),
            *extra,
        ]

    def test_required_review_run_args_parse(self) -> None:
        argv = self._argv("--auto-apply-threshold", "0.9")
        with patch("sys.argv", argv):
            args = parse_args()
        self.assertEqual(args.catalog, str(self.catalog_path))
        self.assertEqual(args.auto_apply_threshold, 0.9)
        self.assertEqual(args.apply_state_db, str(self.apply_db))

    def test_main_rejects_threshold_below_min_confidence(self) -> None:
        argv = self._argv("--min-confidence", "0.5", "--auto-apply-threshold", "0.4")
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

    @patch("src.review_run._run_apply_phase")
    @patch("src.review_run._launch_review_ui")
    @patch("src.review_run.write_sidecar_species_labels")
    @patch("src.review_run.replace_catalog_species_labels")
    @patch("src.review_run.ClassificationLog")
    @patch("src.review_run.LightroomCatalog.open")
    @patch("src.review_run.ReviewStore")
    @patch("src.review_run._create_classifier")
    @patch("builtins.print")
    def test_main_auto_applies_without_launching_ui(
        self,
        mocked_print,
        mocked_create_classifier,
        mocked_store_cls,
        mocked_catalog_open,
        mocked_clf_log_cls,
        mocked_replace_catalog_labels,
        mocked_write_sidecar,
        mocked_launch_ui,
        mocked_run_apply,
    ) -> None:
        fake_classifier = Mock()
        fake_classifier.predict.return_value = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.97,
            )
        ]
        mocked_create_classifier.return_value = (fake_classifier, "net/tag")
        store = Mock()
        store.list_reviewed_image_annotations.return_value = []
        store.count_candidate_images.return_value = 0
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        image = SimpleNamespace(
            id_local=101,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:00:00Z",
            rating=4,
        )
        cat = Mock()
        cat.get_images.return_value = [image]
        cat.get_manually_classed_images.return_value = set()
        cat.get_species_tagged_images.return_value = set()
        cat.get_first_gps.return_value = None
        cat_ctx = Mock()
        cat_ctx.__enter__ = Mock(return_value=cat)
        cat_ctx.__exit__ = Mock(return_value=None)
        mocked_catalog_open.return_value = cat_ctx

        clf_log = Mock()
        mocked_clf_log_cls.return_value = clf_log
        mocked_run_apply.return_value = {"considered": 0}

        with patch("sys.argv", self._argv("--auto-apply-threshold", "0.9", "--no-launch-ui", "--no-geo-filter")):
            rc = main()

        self.assertEqual(rc, 0)
        store.ensure_scope.assert_called_once()
        mocked_replace_catalog_labels.assert_called_once()
        mocked_write_sidecar.assert_called_once()
        clf_log.record.assert_called_once()
        mocked_launch_ui.assert_not_called()
        mocked_run_apply.assert_not_called()
        mocked_print.assert_any_call("Auto-applied labels to 1 image(s). No review is needed.")

    @patch("src.review_run._seed_deferred_candidate")
    @patch("src.review_run._run_apply_phase")
    @patch("src.review_run._launch_review_ui")
    @patch("src.review_run.ClassificationLog")
    @patch("src.review_run.LightroomCatalog.open")
    @patch("src.review_run.ReviewStore")
    @patch("src.review_run._create_classifier")
    @patch("builtins.print")
    def test_main_seeds_and_launches_ui_for_deferred_items(
        self,
        mocked_print,
        mocked_create_classifier,
        mocked_store_cls,
        mocked_catalog_open,
        mocked_clf_log_cls,
        mocked_launch_ui,
        mocked_run_apply,
        mocked_seed_deferred,
    ) -> None:
        fake_classifier = Mock()
        fake_classifier.predict.return_value = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.61,
            )
        ]
        mocked_create_classifier.return_value = (fake_classifier, "net/tag")
        store = Mock()
        store.list_reviewed_image_annotations.return_value = []
        store.count_candidate_images.return_value = 1
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        image = SimpleNamespace(
            id_local=202,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:00:00Z",
            rating=4,
        )
        cat = Mock()
        cat.get_images.return_value = [image]
        cat.get_manually_classed_images.return_value = set()
        cat.get_species_tagged_images.return_value = set()
        cat.get_first_gps.return_value = None
        cat_ctx = Mock()
        cat_ctx.__enter__ = Mock(return_value=cat)
        cat_ctx.__exit__ = Mock(return_value=None)
        mocked_catalog_open.return_value = cat_ctx

        clf_log = Mock()
        mocked_clf_log_cls.return_value = clf_log
        mocked_run_apply.return_value = {
            "considered": 1,
            "applied": 1,
            "repaired": 0,
            "verified_skipped": 0,
            "conflicts": 0,
            "no_label": 0,
            "errors": 0,
        }

        with patch("sys.argv", self._argv("--auto-apply-threshold", "0.9", "--no-geo-filter")):
            rc = main()

        self.assertEqual(rc, 0)
        mocked_seed_deferred.assert_called_once()
        mocked_launch_ui.assert_called_once()
        mocked_run_apply.assert_not_called()
        clf_log.record.assert_not_called()
        mocked_print.assert_any_call("Queued 1 image(s) for review.")
        mocked_print.assert_any_call("Review the queued images in the browser. Only reviewed labels will be applied afterward.")
        mocked_print.assert_any_call("Review is still pending for 1 image(s). No reviewed labels were applied yet.")

    @patch("src.review_run._seed_deferred_candidate")
    @patch("src.review_run._run_apply_phase")
    @patch("src.review_run._launch_review_ui")
    @patch("src.review_run.ClassificationLog")
    @patch("src.review_run.LightroomCatalog.open")
    @patch("src.review_run.ReviewStore")
    @patch("src.review_run._create_classifier")
    @patch("builtins.print")
    def test_main_runs_apply_when_reviewed_labels_exist(
        self,
        mocked_print,
        mocked_create_classifier,
        mocked_store_cls,
        mocked_catalog_open,
        mocked_clf_log_cls,
        mocked_launch_ui,
        mocked_run_apply,
        mocked_seed_deferred,
    ) -> None:
        fake_classifier = Mock()
        fake_classifier.predict.return_value = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.61,
            )
        ]
        mocked_create_classifier.return_value = (fake_classifier, "net/tag")
        store = Mock()
        store.list_reviewed_image_annotations.return_value = [{"candidate_id": "runimg_202"}]
        store.count_candidate_images.return_value = 1
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        image = SimpleNamespace(
            id_local=202,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:00:00Z",
            rating=4,
        )
        cat = Mock()
        cat.get_images.return_value = [image]
        cat.get_manually_classed_images.return_value = set()
        cat.get_species_tagged_images.return_value = set()
        cat.get_first_gps.return_value = None
        cat_ctx = Mock()
        cat_ctx.__enter__ = Mock(return_value=cat)
        cat_ctx.__exit__ = Mock(return_value=None)
        mocked_catalog_open.return_value = cat_ctx

        clf_log = Mock()
        mocked_clf_log_cls.return_value = clf_log
        mocked_run_apply.return_value = {
            "considered": 1,
            "applied": 1,
            "repaired": 0,
            "verified_skipped": 0,
            "conflicts": 0,
            "no_label": 0,
            "errors": 0,
        }

        with patch("sys.argv", self._argv("--auto-apply-threshold", "0.9", "--no-launch-ui", "--no-geo-filter")):
            rc = main()

        self.assertEqual(rc, 0)
        mocked_seed_deferred.assert_called_once()
        mocked_launch_ui.assert_not_called()
        mocked_run_apply.assert_called_once()
        mocked_print.assert_any_call("Queued 1 image(s) for review.")
        mocked_print.assert_any_call("Queued review items are stored in " + str(self.review_db) + ". Launch review_app to continue.")
        mocked_print.assert_any_call("Applied reviewed labels for 1 image(s); 0 already matched, 0 conflicted, 0 had no label outcome, 0 errored.")

    @patch("src.review_run._run_apply_phase")
    @patch("src.review_run._launch_review_ui")
    @patch("src.review_run._seed_deferred_candidate")
    @patch("src.review_run.write_sidecar_species_labels")
    @patch("src.review_run.replace_catalog_species_labels")
    @patch("src.review_run.ClassificationLog")
    @patch("src.review_run.LightroomCatalog.open")
    @patch("src.review_run.ReviewStore")
    @patch("src.review_run._create_classifier")
    def test_main_skips_manual_and_already_tagged_images(
        self,
        mocked_create_classifier,
        mocked_store_cls,
        mocked_catalog_open,
        mocked_clf_log_cls,
        mocked_replace_catalog_labels,
        mocked_write_sidecar,
        mocked_seed_deferred,
        mocked_launch_ui,
        mocked_run_apply,
    ) -> None:
        fake_classifier = Mock()
        mocked_create_classifier.return_value = (fake_classifier, "net/tag")
        store = Mock()
        store.list_reviewed_image_annotations.return_value = []
        store.count_candidate_images.return_value = 0
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        image_manual = SimpleNamespace(
            id_local=301,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:00:00Z",
            rating=4,
        )
        image_tagged = SimpleNamespace(
            id_local=302,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:01:00Z",
            rating=4,
        )
        cat = Mock()
        cat.get_images.return_value = [image_manual, image_tagged]
        cat.get_manually_classed_images.return_value = {301}
        cat.get_species_tagged_images.return_value = {302}
        cat.get_first_gps.return_value = None
        cat_ctx = Mock()
        cat_ctx.__enter__ = Mock(return_value=cat)
        cat_ctx.__exit__ = Mock(return_value=None)
        mocked_catalog_open.return_value = cat_ctx

        clf_log = Mock()
        mocked_clf_log_cls.return_value = clf_log

        with patch("sys.argv", self._argv("--auto-apply-threshold", "0.9", "--no-launch-ui", "--no-geo-filter")):
            rc = main()

        self.assertEqual(rc, 0)
        fake_classifier.predict.assert_not_called()
        mocked_replace_catalog_labels.assert_not_called()
        mocked_write_sidecar.assert_not_called()
        mocked_seed_deferred.assert_not_called()
        mocked_launch_ui.assert_not_called()
        mocked_run_apply.assert_not_called()
        clf_log.record.assert_not_called()

    @patch("src.review_run._run_apply_phase")
    @patch("src.review_run._launch_review_ui")
    @patch("src.review_run.write_sidecar_species_labels")
    @patch("src.review_run.replace_catalog_species_labels")
    @patch("src.review_run.ClassificationLog")
    @patch("src.review_run.LightroomCatalog.open")
    @patch("src.review_run.ReviewStore")
    @patch("src.review_run._create_classifier")
    def test_main_retag_below_confidence_reprocesses_tagged_image(
        self,
        mocked_create_classifier,
        mocked_store_cls,
        mocked_catalog_open,
        mocked_clf_log_cls,
        mocked_replace_catalog_labels,
        mocked_write_sidecar,
        mocked_launch_ui,
        mocked_run_apply,
    ) -> None:
        fake_classifier = Mock()
        fake_classifier.predict.return_value = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.97,
            )
        ]
        mocked_create_classifier.return_value = (fake_classifier, "net/tag")
        store = Mock()
        store.list_reviewed_image_annotations.return_value = []
        store.count_candidate_images.return_value = 0
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        image = SimpleNamespace(
            id_local=401,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:00:00Z",
            rating=4,
        )
        cat = Mock()
        cat.get_images.return_value = [image]
        cat.get_manually_classed_images.return_value = set()
        cat.get_species_tagged_images.return_value = {401}
        cat.get_first_gps.return_value = None
        cat_ctx = Mock()
        cat_ctx.__enter__ = Mock(return_value=cat)
        cat_ctx.__exit__ = Mock(return_value=None)
        mocked_catalog_open.return_value = cat_ctx

        clf_log = Mock()
        clf_log.get_images_below_confidence.return_value = {401}
        mocked_clf_log_cls.return_value = clf_log

        with patch(
            "sys.argv",
            self._argv("--auto-apply-threshold", "0.9", "--retag-below-confidence", "0.8", "--no-launch-ui", "--no-geo-filter"),
        ):
            rc = main()

        self.assertEqual(rc, 0)
        fake_classifier.predict.assert_called_once()
        cat.remove_auto_classifications.assert_called_once_with(401)
        mocked_replace_catalog_labels.assert_called_once()
        mocked_write_sidecar.assert_called_once()
        self.assertTrue(mocked_write_sidecar.call_args.kwargs["replace_existing"])
        clf_log.record.assert_called_once()
        mocked_launch_ui.assert_not_called()
        mocked_run_apply.assert_not_called()

    @patch("src.review_run._run_apply_phase")
    @patch("src.review_run._launch_review_ui")
    @patch("src.review_run._seed_deferred_candidate")
    @patch("src.review_run.write_sidecar_species_labels")
    @patch("src.review_run.replace_catalog_species_labels")
    @patch("src.review_run.ClassificationLog")
    @patch("src.review_run.LightroomCatalog.open")
    @patch("src.review_run.ReviewStore")
    @patch("src.review_run._create_classifier")
    def test_main_dry_run_seeds_deferred_without_catalog_writes(
        self,
        mocked_create_classifier,
        mocked_store_cls,
        mocked_catalog_open,
        mocked_clf_log_cls,
        mocked_replace_catalog_labels,
        mocked_write_sidecar,
        mocked_seed_deferred,
        mocked_launch_ui,
        mocked_run_apply,
    ) -> None:
        fake_classifier = Mock()
        fake_classifier.predict.return_value = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.61,
            )
        ]
        mocked_create_classifier.return_value = (fake_classifier, "net/tag")
        store = Mock()
        store.list_reviewed_image_annotations.return_value = []
        store.count_candidate_images.return_value = 1
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        image = SimpleNamespace(
            id_local=501,
            file_path=str(self.image_path),
            capture_time="2026-04-29T10:00:00Z",
            rating=4,
        )
        cat = Mock()
        cat.get_images.return_value = [image]
        cat.get_manually_classed_images.return_value = set()
        cat.get_species_tagged_images.return_value = set()
        cat.get_first_gps.return_value = None
        cat_ctx = Mock()
        cat_ctx.__enter__ = Mock(return_value=cat)
        cat_ctx.__exit__ = Mock(return_value=None)
        mocked_catalog_open.return_value = cat_ctx

        clf_log = Mock()
        mocked_clf_log_cls.return_value = clf_log

        with patch("sys.argv", self._argv("--auto-apply-threshold", "0.9", "--dry-run", "--no-launch-ui", "--no-geo-filter")):
            rc = main()

        self.assertEqual(rc, 0)
        self.assertTrue(mocked_catalog_open.call_args.kwargs["readonly"])
        mocked_seed_deferred.assert_called_once()
        mocked_replace_catalog_labels.assert_not_called()
        mocked_write_sidecar.assert_not_called()
        clf_log.record.assert_not_called()
        mocked_run_apply.assert_not_called()
        mocked_launch_ui.assert_not_called()

    @patch("src.review_run.load_image")
    def test_seed_deferred_candidate_is_idempotent_for_same_image(self, mocked_load_image) -> None:
        mocked_load_image.return_value = SimpleNamespace(size=(400, 300))
        preview_path = self.preview_dir / "runimg_601.jpg"
        preview_path.write_bytes(b"preview")
        provider = Mock()
        provider.build_preview_from_image.return_value = preview_path
        img = SimpleNamespace(
            id_local=601,
            capture_time="2026-04-29T10:00:00Z",
            rating=5,
        )
        confident = [
            SimpleNamespace(
                label="00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                confidence=0.61,
            )
        ]

        with ReviewStore(self.review_db) as store:
            store.ensure_scope(
                scope_key="scope_seed",
                scope_name="Catalog / Seed",
                catalog_name="catalog",
                catalog_path=str(self.catalog_path),
                trip_folder="Seed",
                workflow_type="run_hybrid_review",
                created_at="2026-04-29T10:00:00Z",
            )
            _seed_deferred_candidate(
                store=store,
                provider=provider,
                scope_key="scope_seed",
                image_path=self.image_path,
                img=img,
                confident=confident,
                clf_model_str="net/tag",
                geo_filtered=False,
            )
            _seed_deferred_candidate(
                store=store,
                provider=provider,
                scope_key="scope_seed",
                image_path=self.image_path,
                img=img,
                confident=confident,
                clf_model_str="net/tag",
                geo_filtered=False,
            )

            self.assertEqual(store.count_candidates(scope_key="scope_seed"), 1)
            self.assertEqual(store.count_candidate_images(scope_key="scope_seed"), 1)
            suggestion = store.get_seed_suggestion("runimg_601")
            self.assertIsNotNone(suggestion)
            assert suggestion is not None
            self.assertEqual(suggestion["best_common_name"], "Bald Eagle")


if __name__ == "__main__":
    unittest.main()
