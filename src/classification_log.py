"""
Persistent classification log for lr-bird-classifier.

Stores every auto-tagging result in a small SQLite database co-located with
the Lightroom catalog:

    /path/to/Catalog.lrcat
    /path/to/Catalog_lr_classifier.sqlite   ← this file

This lets the tool:
  - Know the confidence score that was recorded for any image
  - Support --retag-below-confidence: re-run only on images whose previously
    recorded best confidence falls below a given threshold
  - Provide a confidence summary after a run

Schema
------
classifications
    image_id       INTEGER   — AgLibraryImage.id_local
    classified_at  TEXT      — ISO-8601 UTC timestamp
    model          TEXT      — e.g. "rope_vit_reg4_b14/capi-inat21"
    label          TEXT      — raw iNat21 class label
    common_name    TEXT
    sci_name       TEXT
    confidence     REAL      — 0.0–1.0
    PRIMARY KEY (image_id, label)

One row per (image, species) — supports top-k > 1.  Re-running an image
replaces all its rows (DELETE + INSERT).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .classifier import Prediction

log = logging.getLogger(__name__)


class ClassificationLog:
    """
    Read/write interface to the per-catalog classification log.

    Usage::

        log = ClassificationLog(catalog_path)
        log.record(image_id, predictions, model="rope_vit_reg4_b14/capi-inat21")
        low = log.get_images_below_confidence(0.4)
        log.close()
    """

    def __init__(self, catalog_path: Path) -> None:
        self.path = catalog_path.with_name(
            catalog_path.stem + "_lr_classifier.sqlite"
        )
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        log.debug(f"Classification log: {self.path.name}")

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS classifications (
                image_id      INTEGER NOT NULL,
                classified_at TEXT    NOT NULL,
                model         TEXT    NOT NULL,
                label         TEXT    NOT NULL,
                common_name   TEXT,
                sci_name      TEXT,
                confidence    REAL    NOT NULL,
                PRIMARY KEY (image_id, label)
            );
            CREATE INDEX IF NOT EXISTS idx_image_id
                ON classifications (image_id);
            CREATE INDEX IF NOT EXISTS idx_confidence
                ON classifications (confidence);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def record(
        self,
        image_id: int,
        predictions: list["Prediction"],
        model: str,
    ) -> None:
        """
        Record (or replace) classification results for *image_id*.

        All existing rows for the image are removed first so a retag
        produces a clean slate rather than accumulating stale entries.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "DELETE FROM classifications WHERE image_id = ?", (image_id,)
        )
        self._conn.executemany(
            """
            INSERT INTO classifications
                (image_id, classified_at, model, label, common_name, sci_name, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (image_id, now, model, p.label, p.common_name, p.sci_name, p.confidence)
                for p in predictions
            ],
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get_images_below_confidence(self, threshold: float) -> set[int]:
        """
        Return image_ids whose *best* recorded confidence is below *threshold*.

        These are candidates for --retag-below-confidence.
        """
        rows = self._conn.execute(
            """
            SELECT image_id
            FROM   classifications
            GROUP  BY image_id
            HAVING MAX(confidence) < ?
            """,
            (threshold,),
        ).fetchall()
        return {r["image_id"] for r in rows}

    def get_confidence(self, image_id: int) -> float | None:
        """Return the best recorded confidence for *image_id*, or None."""
        row = self._conn.execute(
            "SELECT MAX(confidence) AS c FROM classifications WHERE image_id = ?",
            (image_id,),
        ).fetchone()
        return row["c"] if row and row["c"] is not None else None

    def confidence_summary(self) -> dict:
        """
        Return a summary dict useful for post-run reporting.

        Keys: total, mean_confidence, pct_above_90, pct_above_75,
              pct_above_50, pct_below_50
        """
        rows = self._conn.execute(
            """
            SELECT MAX(confidence) AS best
            FROM   classifications
            GROUP  BY image_id
            """
        ).fetchall()

        if not rows:
            return {"total": 0}

        scores = [r["best"] for r in rows]
        n = len(scores)
        return {
            "total":        n,
            "mean":         sum(scores) / n,
            "pct_above_90": sum(1 for s in scores if s >= 0.90) / n,
            "pct_above_75": sum(1 for s in scores if s >= 0.75) / n,
            "pct_above_50": sum(1 for s in scores if s >= 0.50) / n,
            "pct_below_50": sum(1 for s in scores if s <  0.50) / n,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ClassificationLog":
        return self

    def __exit__(self, *_) -> None:
        self.close()
