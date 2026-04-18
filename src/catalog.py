"""
Lightroom Classic catalog (.lrcat) integration.

Supports reading image paths and writing bird species keywords back into
the catalog's SQLite database.

IMPORTANT: Never open a catalog that Lightroom currently has open — LR
holds an exclusive lock and writes will be lost or corrupt the catalog.
Always make a backup before writing (use open() with backup=True).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Seconds between macOS Core Data epoch (Jan 1 2001) and Unix epoch (Jan 1 1970)
_CORE_DATA_EPOCH_OFFSET = 978307200

# File formats we can run CV classification on
CLASSIFIABLE_FORMATS = {"RAW", "DNG", "JPEG", "TIFF"}

# Keyword hierarchy root that our species tags live under
KEYWORD_ROOT_NAME = "Birds"
KEYWORD_PARENT_NAME = "Species"
KEYWORD_GROUP_NAME = "Group"


@dataclass
class CatalogImage:
    """A single image record from the catalog."""
    id_local: int
    file_path: str          # absolute path on disk
    file_format: str        # RAW, DNG, JPEG, TIFF, etc.
    base_name: str
    extension: str
    rating: Optional[float]
    color_label: Optional[str]
    capture_time: Optional[str]


class LightroomCatalog:
    """
    Read/write interface to a Lightroom Classic .lrcat catalog.

    Usage::

        with LightroomCatalog.open("/path/to/catalog.lrcat") as cat:
            images = cat.get_images(formats={"RAW", "DNG"})
            for img in images:
                species_keyword_id = cat.ensure_keyword("Roseate Spoonbill")
                cat.tag_image(img.id_local, species_keyword_id)
    """

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        flags = "ro" if readonly else "rwc"
        uri = f"file:{self.path}?mode={flags}"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        log.info(f"Opened catalog: {self.path} ({'read-only' if readonly else 'read-write'})")

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        readonly: bool = False,
        backup: bool = True,
    ) -> "LightroomCatalog":
        """
        Open a catalog, optionally creating a timestamped backup first.

        Args:
            path: path to the .lrcat file.
            readonly: open in read-only mode (no writes allowed).
            backup: if True and not readonly, copy the catalog to
                    <name>_backup_<timestamp>.lrcat before opening.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Catalog not found: {path}")

        if not readonly and backup:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = path.with_stem(f"{path.stem}_backup_{ts}")
            shutil.copy2(path, backup_path)
            log.info(f"Backup created: {backup_path}")

        return cls(path, readonly=readonly)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LightroomCatalog":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Reading images
    # ------------------------------------------------------------------

    def get_images(
        self,
        *,
        formats: Optional[set[str]] = None,
        folder_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[CatalogImage]:
        """
        Return images from the catalog.

        Args:
            formats: set of file format strings to include, e.g. {"RAW", "DNG"}.
                     If None, all formats are returned.
            folder_filter: only include images whose path contains this substring.
            limit: max number of images to return.
        """
        sql = """
            SELECT
                i.id_local,
                i.fileFormat,
                i.rating,
                i.colorLabels,
                i.captureTime,
                fi.baseName,
                fi.extension,
                rf.absolutePath,
                fo.pathFromRoot
            FROM Adobe_images i
            JOIN AgLibraryFile fi ON fi.id_local = i.rootFile
            JOIN AgLibraryFolder fo ON fo.id_local = fi.folder
            JOIN AgLibraryRootFolder rf ON rf.id_local = fo.rootFolder
            WHERE i.masterImage IS NULL
        """
        params: list = []

        if formats:
            placeholders = ",".join("?" * len(formats))
            sql += f" AND i.fileFormat IN ({placeholders})"
            params.extend(formats)

        if folder_filter:
            sql += " AND (rf.absolutePath || fo.pathFromRoot) LIKE ?"
            params.append(f"%{folder_filter}%")

        if limit:
            sql += f" LIMIT {int(limit)}"

        rows = self._conn.execute(sql, params).fetchall()

        images = []
        for r in rows:
            file_path = f"{r['absolutePath']}{r['pathFromRoot']}{r['baseName']}.{r['extension']}"
            images.append(CatalogImage(
                id_local=r["id_local"],
                file_path=file_path,
                file_format=r["fileFormat"] or "",
                base_name=r["baseName"],
                extension=r["extension"],
                rating=r["rating"],
                color_label=r["colorLabels"],
                capture_time=r["captureTime"],
            ))

        return images

    def get_keywords_for_image(self, image_id: int) -> list[str]:
        """Return keyword names currently applied to an image."""
        rows = self._conn.execute("""
            SELECT k.name FROM AgLibraryKeywordImage ki
            JOIN AgLibraryKeyword k ON k.id_local = ki.tag
            WHERE ki.image = ?
        """, (image_id,)).fetchall()
        return [r["name"] for r in rows]

    def get_gps_for_image(self, image_id: int) -> Optional[tuple[float, float]]:
        """Return (latitude, longitude) from EXIF metadata for an image, or None."""
        row = self._conn.execute("""
            SELECT gpsLatitude, gpsLongitude
            FROM AgHarvestedExifMetadata
            WHERE image = ?
              AND gpsLatitude IS NOT NULL
              AND gpsLongitude IS NOT NULL
        """, (image_id,)).fetchone()
        return (row["gpsLatitude"], row["gpsLongitude"]) if row else None

    def get_first_gps(self, image_ids: list[int]) -> Optional[tuple[float, float]]:
        """
        Return (latitude, longitude) from the first image in *image_ids* that
        has GPS EXIF data. Returns None if no image has GPS data.
        """
        if not image_ids:
            return None
        # Query in chunks of 500 to stay within SQLite expression limits
        for i in range(0, len(image_ids), 500):
            chunk = image_ids[i:i + 500]
            placeholders = ",".join("?" * len(chunk))
            row = self._conn.execute(f"""
                SELECT gpsLatitude, gpsLongitude
                FROM AgHarvestedExifMetadata
                WHERE image IN ({placeholders})
                  AND gpsLatitude IS NOT NULL
                  AND gpsLongitude IS NOT NULL
                LIMIT 1
            """, chunk).fetchone()
            if row:
                return (row["gpsLatitude"], row["gpsLongitude"])
        return None

    def get_species_tagged_images(self) -> set[int]:
        """
        Return set of image id_locals that already have any keyword under
        the Birds > Species hierarchy. Used for idempotency — skip these
        images on subsequent runs.
        """
        species_row = self._conn.execute("""
            SELECT k.id_local, k.genealogy
            FROM AgLibraryKeyword k
            JOIN AgLibraryKeyword birds ON birds.id_local = k.parent
                AND birds.lc_name = 'birds'
            WHERE k.lc_name = 'species'
        """).fetchone()

        if not species_row:
            return set()

        genealogy_prefix = species_row["genealogy"]

        rows = self._conn.execute("""
            SELECT DISTINCT ki.image
            FROM AgLibraryKeywordImage ki
            JOIN AgLibraryKeyword k ON k.id_local = ki.tag
            WHERE k.genealogy LIKE ?
        """, (f"{genealogy_prefix}/%",)).fetchall()

        return {r["image"] for r in rows}

    def get_manually_classed_images(self) -> set[int]:
        """
        Return set of image id_locals tagged with the 'manually classed' keyword.
        These images are excluded from auto-classification — any manual correction
        applied in Lightroom becomes sticky and will never be overwritten.
        """
        row = self._conn.execute("""
            SELECT id_local FROM AgLibraryKeyword
            WHERE lc_name = 'manually classed'
            LIMIT 1
        """).fetchone()

        if not row:
            return set()

        rows = self._conn.execute("""
            SELECT image FROM AgLibraryKeywordImage WHERE tag = ?
        """, (row["id_local"],)).fetchall()

        return {r["image"] for r in rows}

    def ensure_manually_classed_keyword(self) -> int:
        """
        Ensure the 'manually classed' keyword exists at the top level and
        return its id_local. Call this when writing a manual correction so
        the keyword is available in Lightroom's keyword panel.
        """
        root_id = self._get_root_keyword_id()
        return self._get_or_create_keyword("manually classed", parent_id=root_id)

    # ------------------------------------------------------------------
    # Writing keywords
    # ------------------------------------------------------------------

    def ensure_keyword(
        self,
        name: str,
        parent_name: Optional[str] = KEYWORD_PARENT_NAME,
    ) -> int:
        """
        Return the id_local of the keyword *name*, creating it if needed.

        Keywords are created under the hierarchy:
            Birds > Species > {name}

        Args:
            name: the keyword name (e.g. "Roseate Spoonbill").
            parent_name: direct parent keyword name. Pass None to create
                         at the top level.
        """
        if self.readonly:
            raise RuntimeError("Catalog opened in read-only mode")

        # Ensure the full parent chain exists first
        parent_id = self._ensure_keyword_chain()

        # Now ensure the leaf keyword
        return self._get_or_create_keyword(name, parent_id=parent_id)

    def ensure_group_keyword(self, name: str) -> int:
        """
        Return the id_local of a group keyword *name* under Birds > Group,
        creating the chain if needed.

        E.g. ensure_group_keyword("Eagle") creates Birds > Group > Eagle.
        """
        if self.readonly:
            raise RuntimeError("Catalog opened in read-only mode")
        root_id = self._get_root_keyword_id()
        birds_id = self._get_or_create_keyword(KEYWORD_ROOT_NAME, parent_id=root_id)
        group_id = self._get_or_create_keyword(KEYWORD_GROUP_NAME, parent_id=birds_id)
        return self._get_or_create_keyword(name, parent_id=group_id)

    def _ensure_keyword_chain(self) -> int:
        """Create Birds > Species hierarchy if missing, return Species id."""
        root_id = self._get_root_keyword_id()
        birds_id = self._get_or_create_keyword(KEYWORD_ROOT_NAME, parent_id=root_id)
        species_id = self._get_or_create_keyword(KEYWORD_PARENT_NAME, parent_id=birds_id)
        return species_id

    def _get_root_keyword_id(self) -> int:
        """Return the id of the root (nameless) keyword, creating it if absent."""
        row = self._conn.execute(
            "SELECT id_local FROM AgLibraryKeyword WHERE parent IS NULL LIMIT 1"
        ).fetchone()
        if row:
            return row["id_local"]
        return self._create_keyword(name=None, parent_id=None)

    def _get_or_create_keyword(self, name: Optional[str], parent_id: Optional[int]) -> int:
        """Return existing keyword id or create a new one."""
        if name is None:
            row = self._conn.execute(
                "SELECT id_local FROM AgLibraryKeyword WHERE name IS NULL AND parent IS NULL"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id_local FROM AgLibraryKeyword WHERE lc_name = ? AND parent = ?",
                (name.lower(), parent_id),
            ).fetchone()

        if row:
            return row["id_local"]
        return self._create_keyword(name=name, parent_id=parent_id)

    def _create_keyword(self, name: Optional[str], parent_id: Optional[int]) -> int:
        """Insert a new keyword and return its id_local."""
        new_id_global = str(uuid.uuid4()).upper()

        # Compute genealogy: /N{id} segments where N = digit count
        if parent_id is None:
            # Will fill in after we get the rowid
            genealogy_prefix = ""
        else:
            parent_row = self._conn.execute(
                "SELECT genealogy FROM AgLibraryKeyword WHERE id_local = ?", (parent_id,)
            ).fetchone()
            genealogy_prefix = parent_row["genealogy"] if parent_row else ""

        now = _core_data_now()

        cur = self._conn.execute("""
            INSERT INTO AgLibraryKeyword
                (id_global, name, lc_name, parent, genealogy, dateCreated,
                 imageCountCache, includeOnExport, includeParents, includeSynonyms,
                 keywordType, lastApplied)
            VALUES (?, ?, ?, ?, ?, ?, -1, 1, 1, 1, 'user_keyword', ?)
        """, (
            new_id_global,
            name,
            name.lower() if name else None,
            parent_id,
            "",     # placeholder, updated below
            now,
            now,
        ))
        new_id = cur.lastrowid

        # Build final genealogy now that we have the id
        id_str = str(new_id)
        if genealogy_prefix:
            genealogy = f"{genealogy_prefix}/{len(id_str)}{id_str}"
        else:
            genealogy = f"/{len(id_str)}{id_str}"

        self._conn.execute(
            "UPDATE AgLibraryKeyword SET genealogy = ? WHERE id_local = ?",
            (genealogy, new_id),
        )
        self._conn.commit()
        log.debug(f"Created keyword '{name}' (id={new_id})")
        return new_id

    def tag_image(self, image_id: int, keyword_id: int) -> bool:
        """
        Apply a keyword to an image.

        Returns True if the tag was newly added, False if it already existed.
        """
        if self.readonly:
            raise RuntimeError("Catalog opened in read-only mode")

        existing = self._conn.execute(
            "SELECT id_local FROM AgLibraryKeywordImage WHERE image = ? AND tag = ?",
            (image_id, keyword_id),
        ).fetchone()

        if existing:
            return False

        self._conn.execute(
            "INSERT INTO AgLibraryKeywordImage (image, tag) VALUES (?, ?)",
            (image_id, keyword_id),
        )
        # Bump touchTime so LR knows this image's metadata changed
        self._conn.execute(
            "UPDATE Adobe_images SET touchTime = ?, touchCount = touchCount + 1 WHERE id_local = ?",
            (_core_data_now(), image_id),
        )
        self._conn.commit()
        log.debug(f"Tagged image {image_id} with keyword {keyword_id}")
        return True

    def untag_image(self, image_id: int, keyword_id: int) -> bool:
        """Remove a keyword from an image. Returns True if removed."""
        if self.readonly:
            raise RuntimeError("Catalog opened in read-only mode")

        cur = self._conn.execute(
            "DELETE FROM AgLibraryKeywordImage WHERE image = ? AND tag = ?",
            (image_id, keyword_id),
        )
        if cur.rowcount:
            self._conn.execute(
                "UPDATE Adobe_images SET touchTime = ?, touchCount = touchCount + 1 WHERE id_local = ?",
                (_core_data_now(), image_id),
            )
            self._conn.commit()
            return True
        return False

    # ------------------------------------------------------------------
    # Convenience stats
    # ------------------------------------------------------------------

    def keyword_summary(self) -> list[dict]:
        """Return all keywords with their image counts."""
        rows = self._conn.execute("""
            SELECT k.name, k.id_local, COUNT(ki.image) as count
            FROM AgLibraryKeyword k
            LEFT JOIN AgLibraryKeywordImage ki ON ki.tag = k.id_local
            WHERE k.name IS NOT NULL
            GROUP BY k.id_local
            ORDER BY count DESC
        """).fetchall()
        return [{"name": r["name"], "id": r["id_local"], "count": r["count"]} for r in rows]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _core_data_now() -> float:
    """Current time as a macOS Core Data timestamp (seconds since Jan 1 2001)."""
    return datetime.now(timezone.utc).timestamp() - _CORE_DATA_EPOCH_OFFSET
