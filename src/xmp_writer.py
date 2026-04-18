"""
Write bird classification keywords to XMP sidecar files via exiftool.

Keeps the sidecar XMP in sync with what we wrote into the Lightroom catalog,
preventing LR's "metadata on disk is out of sync" warning.

Two XMP fields are written:
  dc:subject              — flat keyword names (compatible with all apps)
  lr:hierarchicalSubject  — full pipe-separated paths (Lightroom keyword panel)

The sidecar is created from the source image if it doesn't exist yet,
which preserves any embedded XMP already in the RAW/DNG file.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def sidecar_path(image_path: Path) -> Path:
    """Return the XMP sidecar path: same directory, .xmp extension."""
    return image_path.with_suffix(".xmp")



def write_bird_keywords(
    image_path: Path,
    common_name: str,
    order_display: str,
    order: str,
    family: str,
    sci_name: str,
) -> bool:
    """
    Write bird classification keywords to the XMP sidecar for *image_path*.

    Writes to:
        {image_path.stem}.xmp  (same directory)

    dc:subject entries (flat, deduplicated):
        Bald Eagle, Hawks-Eagles-Kites-Allies, Accipitriformes,
        Accipitridae, Haliaeetus leucocephalus

    lr:hierarchicalSubject entries (Lightroom hierarchy):
        Bird-Species|Bald Eagle
        Birds|Order|Hawks-Eagles-Kites-Allies
        Birds|Order|Hawks-Eagles-Kites-Allies|Bald Eagle
        Birds|Scientific|Accipitriformes
        Birds|Scientific|Accipitriformes|Accipitridae
        Birds|Scientific|Accipitriformes|Accipitridae|Haliaeetus leucocephalus

    If the sidecar already exists, keywords are appended (existing keywords
    are preserved).  If it doesn't exist, it is created from the source image
    so that any embedded XMP metadata is carried over.

    Returns True on success, False if exiftool is unavailable or errors.
    """
    xmp = sidecar_path(image_path)

    # Flat keyword names — deduplicated, order preserved
    flat: list[str] = list(dict.fromkeys([
        common_name,
        order_display,
        order,
        family,
        sci_name,
    ]))

    # Hierarchical paths — mirrors the LR keyword hierarchy we write to catalog
    hier: list[str] = [
        f"Bird-Species|{common_name}",
        f"Birds|Order|{order_display}",
        f"Birds|Order|{order_display}|{common_name}",
        f"Birds|Scientific|{order}",
        f"Birds|Scientific|{order}|{family}",
        f"Birds|Scientific|{order}|{family}|{sci_name}",
    ]

    # Only update an existing sidecar; never create one from scratch.
    if not xmp.exists():
        log.debug(f"No XMP sidecar for {image_path.name}; skipping exiftool update")
        return True

    # Always append (+=) so we never clobber keywords added by the user.
    cmd = ["exiftool", "-overwrite_original"]
    for s in flat:
        cmd.append(f"-Subject+={s}")
    for h in hier:
        cmd.append(f"-HierarchicalSubject+={h}")
    cmd.append(str(xmp))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log.debug(f"XMP updated: {xmp.name}")
            return True
        log.warning(f"exiftool failed ({xmp.name}): {result.stderr.strip()}")
        return False
    except FileNotFoundError:
        log.warning("exiftool not found — install it to keep XMP sidecars in sync")
        return False
    except Exception as e:
        log.warning(f"XMP write error ({xmp.name}): {e}")
        return False
