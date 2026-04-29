"""
SQLite persistence layer for the annotation review workflow.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .review_validation import validate_annotation_row, validate_candidate_row

SCHEMA_VERSION = 4


class ReviewStore:
    """Read/write access to the local annotation review database."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            current_version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current_version == 0:
                self._create_schema()
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
            elif current_version < SCHEMA_VERSION:
                self._migrate_schema(current_version)
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                self._conn.commit()
            elif current_version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported review store schema version {current_version}; "
                    f"expected {SCHEMA_VERSION}"
                )

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE review_scopes (
                scope_key    TEXT PRIMARY KEY,
                scope_name   TEXT NOT NULL,
                catalog_name TEXT NOT NULL,
                catalog_path TEXT NOT NULL,
                trip_folder  TEXT NOT NULL,
                workflow_type TEXT NOT NULL DEFAULT 'detector_review',
                created_at   TEXT NOT NULL,
                last_opened_at TEXT,
                status       TEXT NOT NULL DEFAULT 'active',
                notes        TEXT
            );

            CREATE TABLE images (
                id                   INTEGER PRIMARY KEY,
                scope_key            TEXT NOT NULL,
                source_image_id      INTEGER,
                source_image_path    TEXT NOT NULL,
                capture_datetime     TEXT,
                folder               TEXT,
                rating               REAL,
                gps_lat              REAL,
                gps_lon              REAL,
                region_hint          TEXT,
                lens_model           TEXT,
                focal_length         REAL,
                existing_keywords    TEXT,
                burst_group_id       TEXT,
                near_duplicate_group TEXT,
                created_at           TEXT NOT NULL,
                FOREIGN KEY (scope_key) REFERENCES review_scopes(scope_key),
                UNIQUE (scope_key, source_image_path)
            );

            CREATE TABLE candidates (
                id                        TEXT PRIMARY KEY,
                image_id                  INTEGER NOT NULL,
                detector_name             TEXT NOT NULL,
                detected_class            TEXT NOT NULL,
                detector_confidence       REAL NOT NULL,
                bbox_x1                   INTEGER NOT NULL,
                bbox_y1                   INTEGER NOT NULL,
                bbox_x2                   INTEGER NOT NULL,
                bbox_y2                   INTEGER NOT NULL,
                bbox_area_fraction        REAL,
                preview_image_path        TEXT NOT NULL,
                safe_single_subject_burst INTEGER NOT NULL DEFAULT 0,
                review_status             TEXT NOT NULL DEFAULT 'unreviewed',
                reviewed_at               TEXT,
                created_at                TEXT NOT NULL,
                FOREIGN KEY (image_id) REFERENCES images(id),
                CHECK (bbox_x2 > bbox_x1),
                CHECK (bbox_y2 > bbox_y1),
                CHECK (safe_single_subject_burst IN (0, 1)),
                CHECK (review_status IN ('unreviewed', 'in_review', 'reviewed', 'skipped'))
            );

            CREATE TABLE annotations (
                candidate_id      TEXT PRIMARY KEY,
                annotation_status TEXT NOT NULL,
                truth_common_name TEXT,
                truth_sci_name    TEXT,
                truth_label       TEXT,
                taxon_class       TEXT,
                resolved_from_input TEXT,
                stress            INTEGER NOT NULL DEFAULT 0,
                reject_sample     INTEGER NOT NULL DEFAULT 0,
                unsure            INTEGER NOT NULL DEFAULT 0,
                not_a_bird        INTEGER NOT NULL DEFAULT 0,
                bad_crop          INTEGER NOT NULL DEFAULT 0,
                duplicate_sample  INTEGER NOT NULL DEFAULT 0,
                notes             TEXT,
                annotated_by      TEXT,
                annotated_at      TEXT NOT NULL,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id),
                CHECK (annotation_status IN ('labeled', 'reject', 'unsure', 'not_a_bird', 'bad_crop', 'duplicate')),
                CHECK (stress IN (0, 1)),
                CHECK (reject_sample IN (0, 1)),
                CHECK (unsure IN (0, 1)),
                CHECK (not_a_bird IN (0, 1)),
                CHECK (bad_crop IN (0, 1)),
                CHECK (duplicate_sample IN (0, 1))
            );

            CREATE TABLE label_history (
                id                INTEGER PRIMARY KEY,
                truth_common_name TEXT NOT NULL,
                truth_sci_name    TEXT NOT NULL,
                truth_label       TEXT NOT NULL,
                taxon_class       TEXT,
                used_at           TEXT NOT NULL
            );

            CREATE TABLE review_sessions (
                id           INTEGER PRIMARY KEY,
                started_at   TEXT NOT NULL,
                ended_at     TEXT,
                user_id      TEXT,
                queue_filter TEXT,
                notes        TEXT
            );

            CREATE TABLE extraction_cursors (
                scope_key      TEXT PRIMARY KEY,
                cursor_value   TEXT,
                updated_at     TEXT NOT NULL
            );

            CREATE TABLE candidate_seed_suggestions (
                candidate_id        TEXT PRIMARY KEY,
                model               TEXT NOT NULL,
                best_truth_label    TEXT,
                best_common_name    TEXT,
                best_sci_name       TEXT,
                best_confidence     REAL,
                top_predictions_json TEXT,
                geo_filtered        INTEGER NOT NULL DEFAULT 0,
                seeded_at           TEXT NOT NULL,
                FOREIGN KEY (candidate_id) REFERENCES candidates(id)
            );

            CREATE INDEX idx_scopes_last_opened_at ON review_scopes (last_opened_at);

            CREATE INDEX idx_images_scope_key         ON images (scope_key);
            CREATE INDEX idx_images_capture_datetime ON images (capture_datetime);
            CREATE INDEX idx_images_burst_group_id   ON images (burst_group_id);
            CREATE INDEX idx_images_region_hint      ON images (region_hint);

            CREATE INDEX idx_candidates_image_id      ON candidates (image_id);
            CREATE INDEX idx_candidates_review_status ON candidates (review_status);
            CREATE INDEX idx_candidates_detector_conf ON candidates (detector_confidence);

            CREATE INDEX idx_annotations_status      ON annotations (annotation_status);
            CREATE INDEX idx_annotations_truth_label ON annotations (truth_label);
            CREATE INDEX idx_annotations_stress      ON annotations (stress);
            CREATE INDEX idx_annotations_reject      ON annotations (reject_sample);

            CREATE INDEX idx_label_history_used_at ON label_history (used_at DESC);
            CREATE INDEX idx_label_history_label   ON label_history (truth_label);
            """
        )

    def _migrate_schema(self, current_version: int) -> None:
        if current_version == 1:
            self._conn.executescript(
                """
                CREATE TABLE extraction_cursors (
                    scope_key      TEXT PRIMARY KEY,
                    cursor_value   TEXT,
                    updated_at     TEXT NOT NULL
                );
                """
            )
            current_version = 2

        if current_version == 2:
            self._conn.execute("PRAGMA foreign_keys = OFF")
            try:
                self._conn.executescript(
                    """
                    CREATE TABLE review_scopes (
                        scope_key    TEXT PRIMARY KEY,
                        scope_name   TEXT NOT NULL,
                        catalog_name TEXT NOT NULL,
                        catalog_path TEXT NOT NULL,
                        trip_folder  TEXT NOT NULL,
                        created_at   TEXT NOT NULL,
                        last_opened_at TEXT,
                        status       TEXT NOT NULL DEFAULT 'active',
                        notes        TEXT
                    );

                    INSERT INTO review_scopes (
                        scope_key, scope_name, catalog_name, catalog_path, trip_folder, created_at, status
                    ) VALUES (
                        '__legacy__', 'Legacy Imported Scope', 'Legacy', 'Legacy', 'Legacy', '1970-01-01T00:00:00Z', 'active'
                    );

                    CREATE TABLE images_new (
                        id                   INTEGER PRIMARY KEY,
                        scope_key            TEXT NOT NULL,
                        source_image_id      INTEGER,
                        source_image_path    TEXT NOT NULL,
                        capture_datetime     TEXT,
                        folder               TEXT,
                        rating               REAL,
                        gps_lat              REAL,
                        gps_lon              REAL,
                        region_hint          TEXT,
                        lens_model           TEXT,
                        focal_length         REAL,
                        existing_keywords    TEXT,
                        burst_group_id       TEXT,
                        near_duplicate_group TEXT,
                        created_at           TEXT NOT NULL,
                        FOREIGN KEY (scope_key) REFERENCES review_scopes(scope_key),
                        UNIQUE (scope_key, source_image_path)
                    );

                    INSERT INTO images_new (
                        id, scope_key, source_image_id, source_image_path, capture_datetime, folder,
                        rating, gps_lat, gps_lon, region_hint, lens_model, focal_length,
                        existing_keywords, burst_group_id, near_duplicate_group, created_at
                    )
                    SELECT
                        id, '__legacy__', source_image_id, source_image_path, capture_datetime, folder,
                        rating, gps_lat, gps_lon, region_hint, lens_model, focal_length,
                        existing_keywords, burst_group_id, near_duplicate_group, created_at
                    FROM images;

                    DROP TABLE images;
                    ALTER TABLE images_new RENAME TO images;

                    CREATE INDEX idx_scopes_last_opened_at ON review_scopes (last_opened_at);
                    CREATE INDEX idx_images_scope_key         ON images (scope_key);
                    CREATE INDEX idx_images_capture_datetime ON images (capture_datetime);
                    CREATE INDEX idx_images_burst_group_id   ON images (burst_group_id);
                    CREATE INDEX idx_images_region_hint      ON images (region_hint);
                    """
                )
            finally:
                self._conn.execute("PRAGMA foreign_keys = ON")
            current_version = 3

        if current_version == 3:
            self._conn.executescript(
                """
                ALTER TABLE review_scopes
                    ADD COLUMN workflow_type TEXT NOT NULL DEFAULT 'detector_review';

                CREATE TABLE candidate_seed_suggestions (
                    candidate_id        TEXT PRIMARY KEY,
                    model               TEXT NOT NULL,
                    best_truth_label    TEXT,
                    best_common_name    TEXT,
                    best_sci_name       TEXT,
                    best_confidence     REAL,
                    top_predictions_json TEXT,
                    geo_filtered        INTEGER NOT NULL DEFAULT 0,
                    seeded_at           TEXT NOT NULL,
                    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
                );
                """
            )
            current_version = 4

        if current_version != SCHEMA_VERSION:
            raise RuntimeError(
                f"Unsupported review store schema version {current_version}; "
                f"expected {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ReviewStore":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def ensure_scope(
        self,
        *,
        scope_key: str,
        scope_name: str,
        catalog_name: str,
        catalog_path: str,
        trip_folder: str,
        workflow_type: str = "detector_review",
        created_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO review_scopes (
                    scope_key, scope_name, catalog_name, catalog_path, trip_folder, workflow_type, created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(scope_key) DO UPDATE SET
                    scope_name = excluded.scope_name,
                    catalog_name = excluded.catalog_name,
                    catalog_path = excluded.catalog_path,
                    trip_folder = excluded.trip_folder,
                    workflow_type = excluded.workflow_type
                """,
                (scope_key, scope_name, catalog_name, catalog_path, trip_folder, workflow_type, created_at),
            )
            self._conn.commit()

    def touch_scope(self, scope_key: str, opened_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE review_scopes SET last_opened_at = ? WHERE scope_key = ?",
                (opened_at, scope_key),
            )
            self._conn.commit()

    def list_scopes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    s.*,
                    COUNT(DISTINCT i.id) AS image_count,
                    COUNT(DISTINCT c.id) AS candidate_count,
                    COUNT(DISTINCT CASE WHEN c.review_status = 'unreviewed' THEN c.image_id END) AS unreviewed_image_count,
                    SUM(CASE WHEN c.review_status = 'unreviewed' THEN 1 ELSE 0 END) AS unreviewed_count,
                    SUM(CASE WHEN c.review_status = 'reviewed' THEN 1 ELSE 0 END) AS reviewed_count
                FROM review_scopes s
                LEFT JOIN images i ON i.scope_key = s.scope_key
                LEFT JOIN candidates c ON c.image_id = i.id
                GROUP BY s.scope_key, s.scope_name, s.catalog_name, s.catalog_path, s.trip_folder,
                         s.workflow_type,
                         s.created_at, s.last_opened_at, s.status, s.notes
                ORDER BY COALESCE(s.last_opened_at, s.created_at) DESC, s.scope_name ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_scope(self, scope_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM review_scopes WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            return dict(row) if row else None

    def insert_image(self, row: dict[str, Any]) -> int:
        payload = dict(row)
        if not payload.get("scope_key"):
            payload["scope_key"] = "__default__"
            self.ensure_scope(
                scope_key="__default__",
                scope_name="Default Scope",
                catalog_name="Default",
                catalog_path="Default",
                trip_folder="Default",
                created_at=payload.get("created_at", "1970-01-01T00:00:00Z"),
            )
        existing_keywords = payload.get("existing_keywords")
        if existing_keywords is not None and not isinstance(existing_keywords, str):
            payload["existing_keywords"] = json.dumps(existing_keywords)

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM images WHERE scope_key = ? AND source_image_path = ?",
                (payload["scope_key"], payload["source_image_path"]),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

            cur = self._conn.execute(
                """
                INSERT INTO images (
                    scope_key, source_image_id, source_image_path, capture_datetime, folder,
                    rating, gps_lat, gps_lon, region_hint, lens_model,
                    focal_length, existing_keywords, burst_group_id,
                    near_duplicate_group, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["scope_key"],
                    payload.get("source_image_id"),
                    payload["source_image_path"],
                    payload.get("capture_datetime"),
                    payload.get("folder"),
                    payload.get("rating"),
                    payload.get("gps_lat"),
                    payload.get("gps_lon"),
                    payload.get("region_hint"),
                    payload.get("lens_model"),
                    payload.get("focal_length"),
                    payload.get("existing_keywords"),
                    payload.get("burst_group_id"),
                    payload.get("near_duplicate_group"),
                    payload["created_at"],
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def insert_candidate(self, row: dict[str, Any]) -> bool:
        validate_candidate_row(row, require_existing_preview=True)
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM candidates WHERE id = ?",
                (row["candidate_id"],),
            ).fetchone()
            if existing is not None:
                return False

            self._conn.execute(
                """
                INSERT INTO candidates (
                    id, image_id, detector_name, detected_class, detector_confidence,
                    bbox_x1, bbox_y1, bbox_x2, bbox_y2, bbox_area_fraction,
                    preview_image_path, safe_single_subject_burst, review_status,
                    reviewed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["candidate_id"],
                    row["image_id"],
                    row["detector_name"],
                    row["detected_class"],
                    row["detector_confidence"],
                    row["bbox_x1"],
                    row["bbox_y1"],
                    row["bbox_x2"],
                    row["bbox_y2"],
                    row.get("bbox_area_fraction"),
                    row["preview_image_path"],
                    int(row.get("safe_single_subject_burst", False)),
                    row["review_status"],
                    row.get("reviewed_at"),
                    row["created_at"],
                ),
            )
            self._conn.commit()
            return True

    def mark_candidate_in_review(self, candidate_id: str) -> None:
        self._set_review_status(candidate_id, "in_review")

    def mark_candidate_skipped(self, candidate_id: str) -> None:
        self._set_review_status(candidate_id, "skipped")

    def _set_review_status(self, candidate_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE candidates SET review_status = ? WHERE id = ?",
                (status, candidate_id),
            )
            self._conn.commit()

    def reset_review_state(self) -> None:
        """Clear human review state while preserving extracted candidates and images."""
        with self._lock:
            self._conn.execute("DELETE FROM annotations")
            self._conn.execute("DELETE FROM label_history")
            self._conn.execute(
                "UPDATE candidates SET review_status = 'unreviewed', reviewed_at = NULL"
            )
            self._conn.commit()

    def get_extraction_cursor(self, scope_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor_value FROM extraction_cursors WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
            return str(row["cursor_value"]) if row and row["cursor_value"] is not None else None

    def set_extraction_cursor(self, scope_key: str, cursor_value: str, updated_at: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO extraction_cursors (scope_key, cursor_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(scope_key) DO UPDATE SET
                    cursor_value = excluded.cursor_value,
                    updated_at = excluded.updated_at
                """,
                (scope_key, cursor_value, updated_at),
            )
            self._conn.commit()

    def clear_extraction_cursor(self, scope_key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM extraction_cursors WHERE scope_key = ?", (scope_key,))
            self._conn.commit()

    def upsert_seed_suggestion(
        self,
        candidate_id: str,
        *,
        model: str,
        best_truth_label: str | None,
        best_common_name: str | None,
        best_sci_name: str | None,
        best_confidence: float | None,
        top_predictions: list[dict[str, Any]] | None,
        geo_filtered: bool,
        seeded_at: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO candidate_seed_suggestions (
                    candidate_id, model, best_truth_label, best_common_name, best_sci_name,
                    best_confidence, top_predictions_json, geo_filtered, seeded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    model = excluded.model,
                    best_truth_label = excluded.best_truth_label,
                    best_common_name = excluded.best_common_name,
                    best_sci_name = excluded.best_sci_name,
                    best_confidence = excluded.best_confidence,
                    top_predictions_json = excluded.top_predictions_json,
                    geo_filtered = excluded.geo_filtered,
                    seeded_at = excluded.seeded_at
                """,
                (
                    candidate_id,
                    model,
                    best_truth_label,
                    best_common_name,
                    best_sci_name,
                    best_confidence,
                    json.dumps(top_predictions) if top_predictions is not None else None,
                    int(geo_filtered),
                    seeded_at,
                ),
            )
            self._conn.commit()

    def get_seed_suggestion(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidate_seed_suggestions WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            result["geo_filtered"] = bool(result["geo_filtered"])
            if result.get("top_predictions_json"):
                result["top_predictions"] = json.loads(result["top_predictions_json"])
            else:
                result["top_predictions"] = None
            result.pop("top_predictions_json", None)
            return result

    def delete_images_by_source_paths(self, source_paths: list[str], *, scope_key: str | None = None) -> int:
        """
        Delete extracted review rows for the given source image paths.

        This removes annotations and candidates before removing the image rows.
        Returns the number of image rows deleted.
        """
        if not source_paths:
            return 0
        deleted = 0
        with self._lock:
            for i in range(0, len(source_paths), 500):
                chunk = source_paths[i : i + 500]
                placeholders = ",".join("?" * len(chunk))
                sql = f"SELECT id FROM images WHERE source_image_path IN ({placeholders})"
                params: list[Any] = list(chunk)
                if scope_key is not None:
                    sql += " AND scope_key = ?"
                    params.append(scope_key)
                image_rows = self._conn.execute(sql, params).fetchall()
                image_ids = [int(row["id"]) for row in image_rows]
                if not image_ids:
                    continue

                id_placeholders = ",".join("?" * len(image_ids))
                candidate_rows = self._conn.execute(
                    f"SELECT id FROM candidates WHERE image_id IN ({id_placeholders})",
                    image_ids,
                ).fetchall()
                candidate_ids = [str(row["id"]) for row in candidate_rows]
                if candidate_ids:
                    candidate_placeholders = ",".join("?" * len(candidate_ids))
                    self._conn.execute(
                        f"DELETE FROM annotations WHERE candidate_id IN ({candidate_placeholders})",
                        candidate_ids,
                    )
                    self._conn.execute(
                        f"DELETE FROM candidates WHERE id IN ({candidate_placeholders})",
                        candidate_ids,
                    )

                self._conn.execute(
                    f"DELETE FROM images WHERE id IN ({id_placeholders})",
                    image_ids,
                )
                deleted += len(image_ids)

            self._conn.commit()
        return deleted

    def upsert_annotation(self, row: dict[str, Any]) -> None:
        validate_annotation_row(row)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO annotations (
                    candidate_id, annotation_status, truth_common_name, truth_sci_name,
                    truth_label, taxon_class, resolved_from_input, stress,
                    reject_sample, unsure, not_a_bird, bad_crop, duplicate_sample,
                    notes, annotated_by, annotated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    annotation_status = excluded.annotation_status,
                    truth_common_name = excluded.truth_common_name,
                    truth_sci_name = excluded.truth_sci_name,
                    truth_label = excluded.truth_label,
                    taxon_class = excluded.taxon_class,
                    resolved_from_input = excluded.resolved_from_input,
                    stress = excluded.stress,
                    reject_sample = excluded.reject_sample,
                    unsure = excluded.unsure,
                    not_a_bird = excluded.not_a_bird,
                    bad_crop = excluded.bad_crop,
                    duplicate_sample = excluded.duplicate_sample,
                    notes = excluded.notes,
                    annotated_by = excluded.annotated_by,
                    annotated_at = excluded.annotated_at
                """,
                (
                    row["candidate_id"],
                    row["annotation_status"],
                    row.get("truth_common_name"),
                    row.get("truth_sci_name"),
                    row.get("truth_label"),
                    row.get("taxon_class"),
                    row.get("resolved_from_input"),
                    int(row["stress"]),
                    int(row["reject_sample"]),
                    int(row["unsure"]),
                    int(row["not_a_bird"]),
                    int(row["bad_crop"]),
                    int(row["duplicate_sample"]),
                    row.get("notes"),
                    row.get("annotated_by"),
                    row["annotated_at"],
                ),
            )
            self._conn.execute(
                "UPDATE candidates SET review_status = 'reviewed', reviewed_at = ? WHERE id = ?",
                (row["annotated_at"], row["candidate_id"]),
            )
            if row["annotation_status"] == "labeled":
                self._append_label_history(row)
            self._conn.commit()

    def _append_label_history(self, row: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO label_history (
                truth_common_name, truth_sci_name, truth_label, taxon_class, used_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["truth_common_name"],
                row["truth_sci_name"],
                row["truth_label"],
                row.get("taxon_class"),
                row["annotated_at"],
            ),
        )

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_image(self, image_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM images WHERE id = ?",
                (image_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            existing_keywords = result.get("existing_keywords")
            if existing_keywords:
                result["existing_keywords"] = json.loads(existing_keywords)
            return result

    def list_candidates(
        self,
        *,
        scope_key: str | None = None,
        review_status: str | None = None,
        burst_group_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT c.*
            FROM candidates c
            JOIN images i ON i.id = c.image_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if scope_key is not None:
            sql += " AND i.scope_key = ?"
            params.append(scope_key)
        if review_status is not None:
            sql += " AND c.review_status = ?"
            params.append(review_status)
        if burst_group_id is not None:
            sql += " AND i.burst_group_id = ?"
            params.append(burst_group_id)
        sql += " ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def count_candidates(
        self,
        *,
        scope_key: str | None = None,
        review_status: str | None = None,
        burst_group_id: str | None = None,
    ) -> int:
        sql = """
            SELECT COUNT(*) AS n
            FROM candidates c
            JOIN images i ON i.id = c.image_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if scope_key is not None:
            sql += " AND i.scope_key = ?"
            params.append(scope_key)
        if review_status is not None:
            sql += " AND c.review_status = ?"
            params.append(review_status)
        if burst_group_id is not None:
            sql += " AND i.burst_group_id = ?"
            params.append(burst_group_id)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0

    def count_candidate_images(
        self,
        *,
        scope_key: str | None = None,
        review_status: str | None = None,
    ) -> int:
        sql = """
            SELECT COUNT(DISTINCT c.image_id) AS n
            FROM candidates c
            JOIN images i ON i.id = c.image_id
            WHERE 1 = 1
        """
        params: list[Any] = []
        if scope_key is not None:
            sql += " AND i.scope_key = ?"
            params.append(scope_key)
        if review_status is not None:
            sql += " AND c.review_status = ?"
            params.append(review_status)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0

    def queue_position(
        self,
        candidate_id: str,
        *,
        scope_key: str | None = None,
        review_status: str = "unreviewed",
    ) -> tuple[int, int] | None:
        with self._lock:
            sql = """
                SELECT c.id
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE c.review_status = ?
            """
            params: list[Any] = [review_status]
            if scope_key is not None:
                sql += " AND i.scope_key = ?"
                params.append(scope_key)
            sql += " ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC"
            rows = self._conn.execute(sql, params).fetchall()
            ordered_ids = [row["id"] for row in rows]
            if candidate_id not in ordered_ids:
                return None
            return ordered_ids.index(candidate_id) + 1, len(ordered_ids)

    def get_annotation(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM annotations WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            for key in ("stress", "reject_sample", "unsure", "not_a_bird", "bad_crop", "duplicate_sample"):
                result[key] = bool(result[key])
            return result

    def recent_labels(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT truth_common_name, truth_sci_name, truth_label, taxon_class, used_at
                FROM label_history
                ORDER BY used_at DESC, id DESC
                LIMIT ?
                """,
                (max(limit * 50, 250),),
            ).fetchall()
            unique: list[dict[str, Any]] = []
            seen: set[str] = set()
            for row in rows:
                item = dict(row)
                if item["truth_label"] in seen:
                    continue
                seen.add(item["truth_label"])
                unique.append(item)
                if len(unique) >= limit:
                    break
            return unique

    def previous_reviewed_candidate(self, current_candidate_id: str, *, scope_key: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            current = self._conn.execute(
                """
                SELECT i.capture_datetime AS capture_datetime, c.id AS id, i.scope_key AS scope_key
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE c.id = ?
                """,
                (current_candidate_id,),
            ).fetchone()
            if current is None:
                return None
            row = self._conn.execute(
                """
                SELECT c.*
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE c.review_status = 'reviewed'
                  AND i.scope_key = ?
                  AND (
                    julianday(i.capture_datetime) < julianday(?)
                    OR (
                      julianday(i.capture_datetime) = julianday(?)
                      AND c.id < ?
                    )
                  )
                ORDER BY julianday(i.capture_datetime) DESC, i.capture_datetime DESC, c.id DESC
                LIMIT 1
                """,
                (scope_key or current["scope_key"], current["capture_datetime"], current["capture_datetime"], current["id"]),
            ).fetchone()
            return dict(row) if row else None

    def burst_candidates(
        self,
        candidate_id: str,
        *,
        include_reviewed: bool = False,
    ) -> list[dict[str, Any]]:
        with self._lock:
            current = self._conn.execute(
                """
                SELECT c.id AS id, i.burst_group_id AS burst_group_id, i.capture_datetime AS capture_datetime, i.scope_key AS scope_key
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if current is None or not current["burst_group_id"]:
                return []

            sql = """
                SELECT c.*
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE i.burst_group_id = ?
                  AND i.scope_key = ?
                  AND c.id != ?
                  AND c.safe_single_subject_burst = 1
            """
            params: list[Any] = [current["burst_group_id"], current["scope_key"], candidate_id]
            if not include_reviewed:
                sql += " AND c.review_status != 'reviewed'"
            sql += " ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC"
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def burst_position(self, candidate_id: str) -> tuple[int, int] | None:
        with self._lock:
            current = self._conn.execute(
                """
                SELECT i.burst_group_id AS burst_group_id, i.capture_datetime AS capture_datetime, c.id AS id, i.scope_key AS scope_key
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE c.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if current is None or not current["burst_group_id"]:
                return None

            rows = self._conn.execute(
                """
                SELECT c.id
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE i.burst_group_id = ?
                  AND i.scope_key = ?
                ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC
                """,
                (current["burst_group_id"], current["scope_key"]),
            ).fetchall()
            ordered_ids = [row["id"] for row in rows]
            if candidate_id not in ordered_ids:
                return None
            return ordered_ids.index(candidate_id) + 1, len(ordered_ids)

    def apply_annotation_to_burst(
        self,
        candidate_id: str,
        annotation_row: dict[str, Any],
    ) -> int:
        targets = self.burst_candidates(candidate_id, include_reviewed=False)
        count = 0
        for target in targets:
            row = dict(annotation_row)
            row["candidate_id"] = target["id"]
            self.upsert_annotation(row)
            count += 1
        return count

    def summary_counts(self, *, scope_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            overview_sql = """
                SELECT c.review_status, COUNT(*) AS n
                FROM candidates c
                JOIN images i ON i.id = c.image_id
            """
            overview_params: list[Any] = []
            if scope_key is not None:
                overview_sql += " WHERE i.scope_key = ?"
                overview_params.append(scope_key)
            overview_sql += " GROUP BY c.review_status"
            overview_rows = self._conn.execute(
                overview_sql,
                overview_params,
            ).fetchall()
            overview = {row["review_status"]: int(row["n"]) for row in overview_rows}

            outcome_sql = """
                SELECT a.annotation_status, COUNT(*) AS n
                FROM annotations a
                JOIN candidates c ON c.id = a.candidate_id
                JOIN images i ON i.id = c.image_id
            """
            outcome_params: list[Any] = []
            if scope_key is not None:
                outcome_sql += " WHERE i.scope_key = ?"
                outcome_params.append(scope_key)
            outcome_sql += " GROUP BY a.annotation_status"
            outcome_rows = self._conn.execute(outcome_sql, outcome_params).fetchall()
            outcomes = {row["annotation_status"]: int(row["n"]) for row in outcome_rows}

            species_sql = """
                SELECT
                    a.truth_common_name,
                    a.truth_sci_name,
                    a.truth_label,
                    SUM(CASE WHEN a.stress = 0 THEN 1 ELSE 0 END) AS normal_count,
                    SUM(CASE WHEN a.stress = 1 THEN 1 ELSE 0 END) AS stress_count,
                    COUNT(*) AS total_count
                FROM annotations a
                JOIN candidates c ON c.id = a.candidate_id
                JOIN images i ON i.id = c.image_id
                WHERE a.annotation_status = 'labeled'
            """
            species_params: list[Any] = []
            if scope_key is not None:
                species_sql += " AND i.scope_key = ?"
                species_params.append(scope_key)
            species_sql += """
                GROUP BY a.truth_label, a.truth_common_name, a.truth_sci_name
                ORDER BY total_count DESC, truth_common_name ASC
            """
            species_rows = self._conn.execute(species_sql, species_params).fetchall()
            species = [dict(row) for row in species_rows]
            return {
                "overview": overview,
                "outcomes": outcomes,
                "species": species,
            }
