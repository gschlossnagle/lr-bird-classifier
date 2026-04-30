from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.review_apply_state import ReviewApplyState


class ReviewApplyStateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)

    def test_run_and_image_state_round_trip(self) -> None:
        state_path = self.tmpdir / "apply.sqlite"
        with ReviewApplyState(state_path) as state:
            run_id = state.start_run(
                started_at="2026-04-29T10:00:00Z",
                review_db_path="/tmp/review.sqlite",
                catalog_path="/tmp/catalog.lrcat",
                scope_key="scope_a",
                policy_version="v1",
                dry_run=True,
            )
            state.upsert_image_state(
                "image-key",
                catalog_path="/tmp/catalog.lrcat",
                source_image_id=123,
                source_image_path="/tmp/source.ARW",
                scope_key="scope_a",
                last_review_fingerprint="abc",
                last_catalog_fingerprint="def",
                last_applied_outcome={"status": "apply"},
                last_applied_at="2026-04-29T10:01:00Z",
                last_run_id=run_id,
                status="verified_skip",
                message="ok",
            )
            row = state.get_image_state("image-key")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["last_applied_outcome"], {"status": "apply"})
            self.assertEqual(row["status"], "verified_skip")
            state.record_event(
                run_id=run_id,
                image_key="image-key",
                event_type="verified_skip",
                created_at="2026-04-29T10:01:00Z",
                details={"status": "ok"},
            )
            state.finish_run(run_id, ended_at="2026-04-29T10:02:00Z", summary={"considered": 1})

