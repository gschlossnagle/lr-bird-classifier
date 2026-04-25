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

SCHEMA_VERSION = 2


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
            CREATE TABLE images (
                id                   INTEGER PRIMARY KEY,
                source_image_id      INTEGER,
                source_image_path    TEXT NOT NULL UNIQUE,
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
                created_at           TEXT NOT NULL
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

    def insert_image(self, row: dict[str, Any]) -> int:
        payload = dict(row)
        existing_keywords = payload.get("existing_keywords")
        if existing_keywords is not None and not isinstance(existing_keywords, str):
            payload["existing_keywords"] = json.dumps(existing_keywords)

        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM images WHERE source_image_path = ?",
                (payload["source_image_path"],),
            ).fetchone()
            if existing is not None:
                return int(existing["id"])

            cur = self._conn.execute(
                """
                INSERT INTO images (
                    source_image_id, source_image_path, capture_datetime, folder,
                    rating, gps_lat, gps_lon, region_hint, lens_model,
                    focal_length, existing_keywords, burst_group_id,
                    near_duplicate_group, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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

    def delete_images_by_source_paths(self, source_paths: list[str]) -> int:
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
                image_rows = self._conn.execute(
                    f"SELECT id FROM images WHERE source_image_path IN ({placeholders})",
                    chunk,
                ).fetchall()
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
        review_status: str | None = None,
    ) -> int:
        sql = """
            SELECT COUNT(DISTINCT c.image_id) AS n
            FROM candidates c
            WHERE 1 = 1
        """
        params: list[Any] = []
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
        review_status: str = "unreviewed",
    ) -> tuple[int, int] | None:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT c.id
                FROM candidates c
                JOIN images i ON i.id = c.image_id
                WHERE c.review_status = ?
                ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC
                """,
                (review_status,),
            ).fetchall()
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

    def previous_reviewed_candidate(self, current_candidate_id: str) -> dict[str, Any] | None:
        with self._lock:
            current = self._conn.execute(
                """
                SELECT i.capture_datetime AS capture_datetime, c.id AS id
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
                (current["capture_datetime"], current["capture_datetime"], current["id"]),
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
                SELECT c.id AS id, i.burst_group_id AS burst_group_id, i.capture_datetime AS capture_datetime
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
                  AND c.id != ?
                  AND c.safe_single_subject_burst = 1
            """
            params: list[Any] = [current["burst_group_id"], candidate_id]
            if not include_reviewed:
                sql += " AND c.review_status != 'reviewed'"
            sql += " ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC"
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def burst_position(self, candidate_id: str) -> tuple[int, int] | None:
        with self._lock:
            current = self._conn.execute(
                """
                SELECT i.burst_group_id AS burst_group_id, i.capture_datetime AS capture_datetime, c.id AS id
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
                ORDER BY julianday(i.capture_datetime) ASC, i.capture_datetime ASC, c.id ASC
                """,
                (current["burst_group_id"],),
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

    def summary_counts(self) -> dict[str, Any]:
        with self._lock:
            overview_rows = self._conn.execute(
                """
                SELECT review_status, COUNT(*) AS n
                FROM candidates
                GROUP BY review_status
                """
            ).fetchall()
            overview = {row["review_status"]: int(row["n"]) for row in overview_rows}

            outcome_rows = self._conn.execute(
                """
                SELECT annotation_status, COUNT(*) AS n
                FROM annotations
                GROUP BY annotation_status
                """
            ).fetchall()
            outcomes = {row["annotation_status"]: int(row["n"]) for row in outcome_rows}

            species_rows = self._conn.execute(
                """
                SELECT
                    truth_common_name,
                    truth_sci_name,
                    truth_label,
                    SUM(CASE WHEN stress = 0 THEN 1 ELSE 0 END) AS normal_count,
                    SUM(CASE WHEN stress = 1 THEN 1 ELSE 0 END) AS stress_count,
                    COUNT(*) AS total_count
                FROM annotations
                WHERE annotation_status = 'labeled'
                GROUP BY truth_label, truth_common_name, truth_sci_name
                ORDER BY total_count DESC, truth_common_name ASC
                """
            ).fetchall()
            species = [dict(row) for row in species_rows]
            return {
                "overview": overview,
                "outcomes": outcomes,
                "species": species,
            }
