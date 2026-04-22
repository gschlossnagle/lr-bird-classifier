"""
Extract embedded JPEG previews from supported image containers via exiftool.

This module is used both for Photoshop documents and for RAW files where an
embedded JPEG preview is often good enough for review and detection. That is
substantially faster than full RAW decoding and aligns with the review
workflow's throughput goals.

Usage (context-manager form — temp file is always cleaned up)::

    from src.preview import jpeg_preview

    with jpeg_preview(path) as jpg_path:
        img = Image.open(jpg_path).convert("RGB")

Or as a plain function if you manage cleanup yourself::

    from src.preview import extract_jpeg_preview

    tmp = extract_jpeg_preview(path)   # returns Path | None
    if tmp:
        img = Image.open(tmp).convert("RGB")
        tmp.unlink()
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Generator, Optional

log = logging.getLogger(__name__)

# Extensions this module handles
PSD_EXTENSIONS = {".psd", ".psb"}
RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2", ".dng", ".pef", ".srw"}

# exiftool tags tried in order of preference (quality / availability)
_PHOTOSHOP_PREVIEW_TAGS = [
    "-PreviewImage",    # Full-resolution JPEG composite (Photoshop 5+)
    "-JpgFromRaw",      # Sometimes present in PSB files
    "-ThumbnailImage",  # Smaller — last resort
]

_RAW_PREVIEW_TAGS = [
    "-JpgFromRaw",
    "-PreviewImage",
    "-OtherImage",
    "-ThumbnailImage",
]


def extract_jpeg_preview(path: Path) -> Optional[Path]:
    """
    Extract a JPEG preview from a supported image container using exiftool.

    The exact tags tried depend on file type. Returns the first non-empty
    extracted preview as a temporary file.

    The caller is responsible for deleting the returned file when done.
    Returns None if no preview could be extracted or exiftool is absent.
    """
    tags = _preview_tags_for_path(path)
    for tag in tags:
        tmp = _try_extract(path, tag)
        if tmp is not None:
            log.debug(f"Extracted preview from {path.name} via {tag} → {tmp.name}")
            return tmp

    log.warning(f"No JPEG preview found in {path.name} — tried {tags}")
    return None


def _preview_tags_for_path(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in PSD_EXTENSIONS:
        return _PHOTOSHOP_PREVIEW_TAGS
    if suffix in RAW_EXTENSIONS:
        return _RAW_PREVIEW_TAGS
    return _PHOTOSHOP_PREVIEW_TAGS


def _try_extract(path: Path, tag: str) -> Optional[Path]:
    """Run exiftool for a single tag and return a temp Path on success."""
    # Create the temp file first so we always have a path to clean up.
    fd, tmp_name = tempfile.mkstemp(suffix=".jpg", prefix="lr_preview_")
    tmp = Path(tmp_name)
    try:
        import os
        os.close(fd)   # close the fd; exiftool writes via stdout

        result = subprocess.run(
            ["exiftool", "-b", tag, str(path)],
            capture_output=True,
            timeout=30,
        )

        if result.returncode == 0 and result.stdout:
            tmp.write_bytes(result.stdout)
            return tmp

        # Non-zero exit or empty output — clean up and try next tag
        tmp.unlink(missing_ok=True)
        return None

    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        log.warning("exiftool not found — cannot extract PSD/PSB preview")
        return None
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log.debug(f"Preview extraction error ({tag}, {path.name}): {e}")
        return None


@contextlib.contextmanager
def jpeg_preview(path: Path) -> Generator[Path, None, None]:
    """
    Context manager: extract a JPEG preview and guarantee temp-file cleanup.

    Raises ValueError if no preview can be extracted.

    Example::

        with jpeg_preview(Path("photo.psb")) as jpg:
            img = Image.open(jpg).convert("RGB")
    """
    tmp = extract_jpeg_preview(path)
    if tmp is None:
        raise ValueError(
            f"Could not extract a JPEG preview from '{path.name}'. "
            "Ensure exiftool is installed and the file contains an embedded preview."
        )
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)
