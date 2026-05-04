from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.apply_review_labels import _exit_code_for_summary, main, parse_args


class ApplyReviewLabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.review_db = self.tmpdir / "review.sqlite"
        self.review_db.write_text("", encoding="utf-8")
        self.catalog = self.tmpdir / "catalog.lrcat"
        self.catalog.write_text("", encoding="utf-8")
        self.state_db = self.tmpdir / "apply.sqlite"

    def _argv(self, *extra: str) -> list[str]:
        return [
            "apply_review_labels",
            "--db",
            str(self.review_db),
            "--catalog",
            str(self.catalog),
            "--state-db",
            str(self.state_db),
            *extra,
        ]

    def test_parse_args(self) -> None:
        with patch("sys.argv", self._argv("--scope", "scope_a", "--dry-run", "--report", "report.jsonl")):
            args = parse_args()
        self.assertEqual(args.scope, "scope_a")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.report, "report.jsonl")

    def test_main_returns_error_for_missing_review_db(self) -> None:
        self.review_db.unlink()
        with patch("sys.argv", self._argv()):
            rc = main()
        self.assertEqual(rc, 1)

    def test_exit_code_prefers_errors(self) -> None:
        code = _exit_code_for_summary({"errors": 1, "conflicts": 4}, dry_run=False)
        self.assertEqual(code, 1)

    def test_exit_code_uses_conflict_code_for_real_apply(self) -> None:
        code = _exit_code_for_summary({"errors": 0, "conflicts": 2}, dry_run=False)
        self.assertEqual(code, 2)

    def test_exit_code_ignores_conflicts_in_dry_run(self) -> None:
        code = _exit_code_for_summary({"errors": 0, "conflicts": 2}, dry_run=True)
        self.assertEqual(code, 0)

    @patch("src.apply_review_labels.ApplyEngine")
    @patch("src.apply_review_labels.ReviewApplyState")
    @patch("src.apply_review_labels.ReviewStore")
    def test_main_returns_conflict_exit_code(
        self,
        mocked_store_cls,
        mocked_state_cls,
        mocked_engine_cls,
    ) -> None:
        store = Mock()
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        state = Mock()
        state_ctx = Mock()
        state_ctx.__enter__ = Mock(return_value=state)
        state_ctx.__exit__ = Mock(return_value=None)
        mocked_state_cls.return_value = state_ctx

        engine = Mock()
        engine.run.return_value = {
            "considered": 1,
            "applied": 0,
            "would_apply": 0,
            "repaired": 0,
            "would_repair": 0,
            "verified_skipped": 0,
            "conflicts": 1,
            "no_label": 0,
            "errors": 0,
        }
        mocked_engine_cls.return_value = engine

        with patch("sys.argv", self._argv()):
            rc = main()

        self.assertEqual(rc, 2)

    @patch("src.apply_review_labels.ApplyEngine")
    @patch("src.apply_review_labels.ReviewApplyState")
    @patch("src.apply_review_labels.ReviewStore")
    def test_main_returns_zero_for_dry_run_conflicts(
        self,
        mocked_store_cls,
        mocked_state_cls,
        mocked_engine_cls,
    ) -> None:
        store = Mock()
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        state = Mock()
        state_ctx = Mock()
        state_ctx.__enter__ = Mock(return_value=state)
        state_ctx.__exit__ = Mock(return_value=None)
        mocked_state_cls.return_value = state_ctx

        engine = Mock()
        engine.run.return_value = {
            "considered": 1,
            "applied": 0,
            "would_apply": 1,
            "repaired": 0,
            "would_repair": 0,
            "verified_skipped": 0,
            "conflicts": 1,
            "no_label": 0,
            "errors": 0,
        }
        mocked_engine_cls.return_value = engine

        with patch("sys.argv", self._argv("--dry-run")):
            rc = main()

        self.assertEqual(rc, 0)

    @patch("src.apply_review_labels.ApplyEngine")
    @patch("src.apply_review_labels.ReviewApplyState")
    @patch("src.apply_review_labels.ReviewStore")
    def test_main_returns_error_exit_code(
        self,
        mocked_store_cls,
        mocked_state_cls,
        mocked_engine_cls,
    ) -> None:
        store = Mock()
        store_ctx = Mock()
        store_ctx.__enter__ = Mock(return_value=store)
        store_ctx.__exit__ = Mock(return_value=None)
        mocked_store_cls.return_value = store_ctx

        state = Mock()
        state_ctx = Mock()
        state_ctx.__enter__ = Mock(return_value=state)
        state_ctx.__exit__ = Mock(return_value=None)
        mocked_state_cls.return_value = state_ctx

        engine = Mock()
        engine.run.return_value = {
            "considered": 1,
            "applied": 0,
            "would_apply": 0,
            "repaired": 0,
            "would_repair": 0,
            "verified_skipped": 0,
            "conflicts": 0,
            "no_label": 0,
            "errors": 1,
        }
        mocked_engine_cls.return_value = engine

        with patch("sys.argv", self._argv()):
            rc = main()

        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
