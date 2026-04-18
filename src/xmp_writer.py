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

import json as _json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Prefixes that identify classifier-written lr:hierarchicalSubject entries.
# Any entry starting with one of these was written by lr-bird-classifier.
_CLASSIFIER_HIER_PREFIXES = (
    "Bird-Species|",
    "Birds|Order|",
    "Birds|Scientific|",
    "Birds|Species|",     # legacy hierarchy, may exist in older catalogs
)


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


def clean_xmp_keywords(
    image_path: Path,
    flat_to_remove: list[str],
    *,
    dry_run: bool = False,
) -> bool:
    """
    Remove classifier-written keywords from the XMP sidecar for *image_path*.

    Removes all lr:hierarchicalSubject entries whose path begins with a
    classifier-owned prefix (Bird-Species|, Birds|Order|, Birds|Scientific|,
    Birds|Species|).

    Also removes each entry in *flat_to_remove* from dc:subject.  Pass the
    complete list of flat tags that were written for this image (common name,
    order display name, order, family, scientific name).

    Returns True  if the sidecar exists and was updated (or would be in dry-run).
    Returns False if no sidecar exists (not an error) or on exiftool failure.
    """
    xmp = sidecar_path(image_path)
    if not xmp.exists():
        log.debug(f"No XMP sidecar for {image_path.name}; skipping clean")
        return False

    # ── Read current lr:hierarchicalSubject values ────────────────────────
    try:
        read_result = subprocess.run(
            ["exiftool", "-json", "-HierarchicalSubject", str(xmp)],
            capture_output=True, text=True, timeout=30,
        )
        if read_result.returncode != 0:
            log.warning(f"exiftool read failed ({xmp.name}): {read_result.stderr.strip()}")
            return False
        data = _json.loads(read_result.stdout) if read_result.stdout.strip() else []
        raw_hier = data[0].get("HierarchicalSubject", []) if data else []
        if isinstance(raw_hier, str):
            raw_hier = [raw_hier]
    except FileNotFoundError:
        log.warning("exiftool not found — install it to keep XMP sidecars in sync")
        return False
    except Exception as e:
        log.warning(f"XMP read error ({xmp.name}): {e}")
        return False

    # ── Determine what to remove ──────────────────────────────────────────
    hier_to_remove = [
        h for h in raw_hier
        if any(h.startswith(p) for p in _CLASSIFIER_HIER_PREFIXES)
    ]
    flat_unique = list(dict.fromkeys(v for v in flat_to_remove if v))

    if not hier_to_remove and not flat_unique:
        log.debug(f"No classifier keywords found in {xmp.name}; nothing to clean")
        return True

    if dry_run:
        log.info(
            f"DRY-RUN XMP {xmp.name}: would remove "
            f"{len(hier_to_remove)} hierarchical + {len(flat_unique)} flat tag(s)"
        )
        return True

    # ── Build and run exiftool removal command ────────────────────────────
    cmd = ["exiftool", "-overwrite_original"]
    for h in hier_to_remove:
        cmd.append(f"-HierarchicalSubject-={h}")
    for f in flat_unique:
        cmd.append(f"-Subject-={f}")
    cmd.append(str(xmp))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log.debug(
                f"XMP cleaned: {xmp.name} "
                f"({len(hier_to_remove)} hier, {len(flat_unique)} flat removed)"
            )
            return True
        log.warning(f"exiftool failed ({xmp.name}): {result.stderr.strip()}")
        return False
    except FileNotFoundError:
        log.warning("exiftool not found — install it to keep XMP sidecars in sync")
        return False
    except Exception as e:
        log.warning(f"XMP write error ({xmp.name}): {e}")
        return False
