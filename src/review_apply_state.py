"""
Persistent apply-state ledger for review-derived Lightroom writeback.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class ReviewApplyState:
    """Small SQLite helper for apply-run bookkeeping."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        current_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version == 0:
            self._conn.executescript(
                """
                CREATE TABLE apply_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    review_db_path TEXT NOT NULL,
                    catalog_path TEXT NOT NULL,
                    scope_key TEXT,
                    policy_version TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    summary_json TEXT
                );

                CREATE TABLE image_apply_state (
                    image_key TEXT PRIMARY KEY,
                    catalog_path TEXT NOT NULL,
                    source_image_id INTEGER,
                    source_image_path TEXT NOT NULL,
                    scope_key TEXT,
                    last_review_fingerprint TEXT NOT NULL,
                    last_catalog_fingerprint TEXT,
                    last_applied_outcome_json TEXT NOT NULL,
                    last_applied_at TEXT NOT NULL,
                    last_run_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT
                );

                CREATE TABLE image_apply_events (
                    id INTEGER PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    image_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    review_fingerprint TEXT,
                    catalog_fingerprint_before TEXT,
                    catalog_fingerprint_after TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            return
        if current_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported review apply state schema version {current_version}; "
                f"expected {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ReviewApplyState":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def start_run(
        self,
        *,
        started_at: str,
        review_db_path: str,
        catalog_path: str,
        scope_key: str | None,
        policy_version: str,
        dry_run: bool,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO apply_runs (
                started_at, review_db_path, catalog_path, scope_key, policy_version, dry_run
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (started_at, review_db_path, catalog_path, scope_key, policy_version, int(dry_run)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, ended_at: str, summary: dict[str, Any]) -> None:
        self._conn.execute(
            """
            UPDATE apply_runs
            SET ended_at = ?, summary_json = ?
            WHERE id = ?
            """,
            (ended_at, json.dumps(summary, separators=(",", ":")), run_id),
        )
        self._conn.commit()

    def get_image_state(self, image_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM image_apply_state WHERE image_key = ?",
            (image_key,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["last_applied_outcome"] = json.loads(result["last_applied_outcome_json"])
        result.pop("last_applied_outcome_json", None)
        return result

    def upsert_image_state(
        self,
        image_key: str,
        *,
        catalog_path: str,
        source_image_id: int | None,
        source_image_path: str,
        scope_key: str | None,
        last_review_fingerprint: str,
        last_catalog_fingerprint: str | None,
        last_applied_outcome: dict[str, Any],
        last_applied_at: str,
        last_run_id: int,
        status: str,
        message: str = "",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO image_apply_state (
                image_key, catalog_path, source_image_id, source_image_path, scope_key,
                last_review_fingerprint, last_catalog_fingerprint, last_applied_outcome_json,
                last_applied_at, last_run_id, status, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(image_key) DO UPDATE SET
                catalog_path = excluded.catalog_path,
                source_image_id = excluded.source_image_id,
                source_image_path = excluded.source_image_path,
                scope_key = excluded.scope_key,
                last_review_fingerprint = excluded.last_review_fingerprint,
                last_catalog_fingerprint = excluded.last_catalog_fingerprint,
                last_applied_outcome_json = excluded.last_applied_outcome_json,
                last_applied_at = excluded.last_applied_at,
                last_run_id = excluded.last_run_id,
                status = excluded.status,
                message = excluded.message
            """,
            (
                image_key,
                catalog_path,
                source_image_id,
                source_image_path,
                scope_key,
                last_review_fingerprint,
                last_catalog_fingerprint,
                json.dumps(last_applied_outcome, separators=(",", ":")),
                last_applied_at,
                last_run_id,
                status,
                message,
            ),
        )
        self._conn.commit()

    def record_event(
        self,
        *,
        run_id: int,
        image_key: str,
        event_type: str,
        created_at: str,
        review_fingerprint: str | None = None,
        catalog_fingerprint_before: str | None = None,
        catalog_fingerprint_after: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO image_apply_events (
                run_id, image_key, event_type, review_fingerprint, catalog_fingerprint_before,
                catalog_fingerprint_after, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                image_key,
                event_type,
                review_fingerprint,
                catalog_fingerprint_before,
                catalog_fingerprint_after,
                json.dumps(details, separators=(",", ":")) if details is not None else None,
                created_at,
            ),
        )
        self._conn.commit()
