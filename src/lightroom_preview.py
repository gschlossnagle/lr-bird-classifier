"""
Resolve and load Lightroom standard previews from a catalog.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import sqlite3

from PIL import Image


def _preview_bundle_root(catalog_path: str | Path) -> Path:
    catalog = Path(catalog_path)
    return catalog.with_name(f"{catalog.stem} Previews.lrdata")


def _previews_db_path(catalog_path: str | Path) -> Path:
    return _preview_bundle_root(catalog_path) / "previews.db"


@lru_cache(maxsize=2048)
def lookup_file_uuid(catalog_path: str, image_path: str) -> str | None:
    resolved = str(Path(image_path).resolve())
    image_no_ext = str(Path(resolved).with_suffix(""))
    try:
        conn = sqlite3.connect(f"file:{Path(catalog_path).resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT f.id_global
            FROM Adobe_images i
            JOIN AgLibraryFile f ON f.id_local = i.rootFile
            JOIN AgLibraryFolder fo ON fo.id_local = f.folder
            JOIN AgLibraryRootFolder rf ON rf.id_local = fo.rootFolder
            WHERE rf.absolutePath || fo.pathFromRoot || f.baseName = ?
            """,
            (image_no_ext,),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    return None if row is None else str(row["id_global"])


@lru_cache(maxsize=2048)
def resolve_standard_preview_path(catalog_path: str, image_path: str) -> Path | None:
    file_uuid = lookup_file_uuid(catalog_path, image_path)
    if file_uuid is None:
        return None

    try:
        conn = sqlite3.connect(f"file:{_previews_db_path(catalog_path).resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT Pyramid.digest, PyramidLevel.longDimension
            FROM PyramidLevel
            JOIN Pyramid USING (uuid, digest)
            WHERE PyramidLevel.uuid = ? AND Pyramid.quality = 'standard'
            ORDER BY PyramidLevel.longDimension DESC
            LIMIT 1
            """,
            (file_uuid,),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return None

    if row is None:
        return None

    preview_path = (
        _preview_bundle_root(catalog_path)
        / file_uuid[0]
        / file_uuid[:4]
        / f"{file_uuid}-{row['digest']}_{int(row['longDimension'])}"
    )
    return preview_path if preview_path.exists() else None


def load_standard_preview(catalog_path: str | Path, image_path: str | Path) -> Image.Image | None:
    preview_path = resolve_standard_preview_path(str(Path(catalog_path).resolve()), str(Path(image_path).resolve()))
    if preview_path is None:
        return None
    image = Image.open(preview_path)
    image.load()
    return image.convert("RGB")
