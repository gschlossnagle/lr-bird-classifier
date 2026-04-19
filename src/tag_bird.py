"""
Manually tag a Lightroom photo with a bird species by common name.

Replaces all classifier-written keyword tags (catalog + XMP sidecar) for the
given image with the full hierarchy for the specified bird.  No model inference
is run — this is purely a manual correction tool.

Usage:
    python src/tag_bird.py --species "Bald Eagle" --images /path/to/image.arw
    python src/tag_bird.py -s "Bald Eagle" -i img1.arw img2.arw img3.arw
    python src/tag_bird.py -s "Bald Eagle" -i *.arw --dry-run
    python src/tag_bird.py -s "Bald Eagle" -i img.arw --catalog /path/to/catalog.lrcat

If 'catalog' is set in ~/.lrbc-config the --catalog flag may be omitted.

Partial / substring matching is supported:
    python src/tag_bird.py -s thrasher -i img.arw
    → lists all thrasher species; exits without writing

After writing, the image is marked "manually classed" so future auto-runs skip it.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import pickle
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

CACHE_PATH  = ROOT / "data" / "taxonomy_cache.json"
MODELS_DIR  = ROOT / "models"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fast class_to_idx loader (no model weights loaded)
# ---------------------------------------------------------------------------

class _SkipTensorsUnpickler(pickle.Unpickler):
    """Unpickler that skips tensor storage, returning None for all tensors."""

    def persistent_load(self, pid):
        return None  # suppress loading of external tensor blobs

    def find_class(self, module: str, name: str):
        if module == "torch._utils" and name in ("_rebuild_tensor_v2", "_rebuild_tensor"):
            return lambda *a, **kw: None
        if module == "torch.storage" and name == "_load_from_bytes":
            return lambda *a, **kw: None
        return super().find_class(module, name)


def _load_class_to_idx(models_dir: Path) -> dict[str, int]:
    """
    Load class_to_idx from a birder iNat21 .pt model file without materialising
    model weights.  Reads the zip archive directly and skips tensor blobs,
    so this is fast even for large models.

    Only iNat21 models are usable here — their labels encode order/family in
    the format "{id}_{kingdom}_{phylum}_{class}_{order}_{family}_{genus}_{species}".
    Models trained on "il-common" or "il-all" use plain common names as labels
    and are skipped.
    """
    # Prefer models with "inat21" in the filename; they use the structured label format
    pt_files = sorted(models_dir.glob("*inat21*.pt"))
    if not pt_files:
        pt_files = sorted(models_dir.glob("*.pt"))
    if not pt_files:
        raise FileNotFoundError(f"No .pt model files found in {models_dir}")

    pt_file = pt_files[0]
    log.debug(f"Reading label index from {pt_file.name}")

    with zipfile.ZipFile(pt_file, "r") as zf:
        pkl_names = [n for n in zf.namelist() if n.endswith("data.pkl")]
        if not pkl_names:
            raise ValueError(f"Unexpected model file format: {pt_file.name}")
        with zf.open(pkl_names[0]) as raw:
            data = _SkipTensorsUnpickler(io.BytesIO(raw.read())).load()

    class_to_idx = data.get("class_to_idx")
    if not class_to_idx:
        raise ValueError(f"class_to_idx not found in {pt_file.name}")

    # Validate that labels are in iNat21 format (e.g. "04402_Animalia_...")
    sample = next(iter(class_to_idx))
    if "_" not in sample or not sample.split("_")[0].isdigit():
        raise ValueError(
            f"{pt_file.name} uses non-iNat21 labels (e.g. {sample!r}). "
            "Download an iNat21 model (name contains 'inat21') and retry."
        )

    return class_to_idx


# ---------------------------------------------------------------------------
# Name index
# ---------------------------------------------------------------------------

def _build_name_index(models_dir: Path, cache_path: Path) -> dict[str, tuple[str, str]]:
    """
    Return {common_name.lower(): (inat21_label, sci_name)} for every bird
    species that has a known common name in the taxonomy cache.
    """
    from src.taxonomy import parse_label

    cache: dict[str, str] = json.loads(cache_path.read_text())
    class_to_idx = _load_class_to_idx(models_dir)

    # sci_name (lower) → inat21 label
    sci_to_label: dict[str, str] = {}
    for label in class_to_idx:
        parsed = parse_label(label)
        if parsed.get("is_bird") and parsed.get("sci_name"):
            sci_to_label[parsed["sci_name"].lower()] = label

    # common_name (lower) → (label, sci_name)
    index: dict[str, tuple[str, str]] = {}
    for sci_name, common_name in cache.items():
        if not common_name:
            continue
        label = sci_to_label.get(sci_name.lower())
        if label:
            index[common_name.title().lower()] = (label, sci_name)

    return index


# ---------------------------------------------------------------------------
# Catalog helpers
# ---------------------------------------------------------------------------

def _find_image_id(cat, image_path: Path) -> int | None:
    """Return id_local for *image_path* in the catalog, or None."""
    stem = image_path.stem
    ext  = image_path.suffix.lstrip(".")
    rows = cat._conn.execute(
        """
        SELECT i.id_local,
               rf.absolutePath || fo.pathFromRoot AS dir_path,
               fi.baseName,
               fi.extension
        FROM   Adobe_images        i
        JOIN   AgLibraryFile       fi ON fi.id_local = i.rootFile
        JOIN   AgLibraryFolder     fo ON fo.id_local = fi.folder
        JOIN   AgLibraryRootFolder rf ON rf.id_local = fo.rootFolder
        WHERE  LOWER(fi.baseName)  = LOWER(?)
          AND  LOWER(fi.extension) = LOWER(?)
        """,
        (stem, ext),
    ).fetchall()

    if not rows:
        return None

    target = str(image_path.resolve())
    for row in rows:
        candidate = row["dir_path"] + row["baseName"] + "." + row["extension"]
        if candidate == target:
            return row["id_local"]

    # Unique basename — accept it even if the path root differs slightly
    if len(rows) == 1:
        return rows[0]["id_local"]

    return None


def _existing_flat_tags(clf_log, image_id: int) -> list[str]:
    """Build the flat-keyword list from the classification log for XMP cleanup."""
    from src.taxonomy import parse_label, get_order_display_name

    flat: list[str] = []
    for row in clf_log.get_all_rows():
        if row["image_id"] != image_id:
            continue
        parsed      = parse_label(row.get("label") or "")
        order_raw   = parsed.get("order", "")
        order_disp  = get_order_display_name(order_raw) if order_raw else ""
        flat.extend([
            row.get("common_name") or "",
            order_disp,
            order_raw,
            parsed.get("family", ""),
            row.get("sci_name") or "",
        ])
    return flat


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    from src.config import load as _load_config
    cfg = _load_config()

    p = argparse.ArgumentParser(
        description=(
            "Manually tag one or more Lightroom photos with a bird species by common name. "
            "Replaces all existing classifier keywords in the catalog and XMP sidecars."
        )
    )
    p.add_argument("-s", "--species", dest="common_name", help="Bird common name, e.g. 'Bald Eagle'")
    p.add_argument("-i", "--images",  nargs="+",          help="One or more image file paths")
    p.add_argument(
        "-c", "--catalog",
        default=cfg.get("catalog"),
        help=(
            "Path to the .lrcat catalog file. "
            "May be omitted if 'catalog' is set in ~/.lrbc-config."
        ),
    )
    p.add_argument("--dry-run",   action="store_true", help="Show what would change without writing")
    p.add_argument("--no-backup", action="store_true", help="Skip catalog backup")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.catalog:
        log.error("No catalog specified. Use --catalog or set 'catalog' in ~/.lrbc-config.")
        return 1
    if not args.common_name:
        log.error("No species specified. Use --species / -s.")
        return 1
    if not args.images:
        log.error("No images specified. Use --images / -i.")
        return 1

    catalog_path  = Path(args.catalog)
    image_paths   = [Path(p) for p in args.images]

    for image_path in image_paths:
        if not image_path.exists():
            log.error(f"Image not found: {image_path}")
            return 1
    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1
    if not CACHE_PATH.exists():
        log.error(f"Taxonomy cache not found: {CACHE_PATH}")
        return 1

    # ── Species lookup ────────────────────────────────────────────────────
    try:
        name_index = _build_name_index(MODELS_DIR, CACHE_PATH)
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        return 1

    query = args.common_name.strip().lower()

    # Exact match takes priority; fall back to substring
    if query in name_index:
        matches = {args.common_name.title(): name_index[query]}
    else:
        matches = {name.title(): name_index[name] for name in name_index if query in name}

    if not matches:
        log.error(f"No species found matching {args.common_name!r}")
        log.error("Try a shorter substring, or check data/taxonomy_cache.json")
        return 1

    if len(matches) > 1:
        print(f"{len(matches)} species match {args.common_name!r} — be more specific:\n")
        for display, (_, sci) in sorted(matches.items()):
            print(f"  {display:45s}  ({sci})")
        return 1

    display_name = next(iter(matches))
    label, sci_name = matches[display_name]

    from src.taxonomy import parse_label, get_order_display_name
    parsed      = parse_label(label)
    order       = parsed.get("order", "")
    family      = parsed.get("family", "")
    order_disp  = get_order_display_name(order) if order else order

    log.info(f"Species : {display_name} ({sci_name})")
    log.info(f"Taxonomy: {order_disp} / {family}")

    if args.dry_run:
        print(f"\nDRY RUN — would tag {len(image_paths)} image(s) as {display_name!r} ({sci_name})")
        print(f"  Order: {order_disp}  Family: {family}")
        for p in image_paths:
            print(f"  {p.name}")
        print("Pass without --dry-run to apply.")
        return 0

    # ── Open catalog + log ────────────────────────────────────────────────
    from src.catalog import LightroomCatalog, confidence_band
    from src.classification_log import ClassificationLog
    from src.classifier import Prediction
    from src.xmp_writer import clean_xmp_keywords, write_bird_keywords

    manual_pred = Prediction(
        rank=1,
        common_name=display_name,
        sci_name=sci_name,
        confidence=1.0,
        is_bird=True,
        label=label,
    )

    clf_log = ClassificationLog(catalog_path)
    errors  = 0

    with LightroomCatalog.open(
        catalog_path,
        readonly=False,
        backup=(not args.no_backup),
    ) as cat:

        # Pre-cache keyword IDs that are shared across all images
        band_kw_id    = cat.ensure_confidence_keyword(confidence_band(1.0))
        manual_kw_id  = cat.ensure_manually_classed_keyword()
        top_id        = cat.ensure_bird_species_keyword(display_name)
        ord_id, kw_id = cat.ensure_species_keyword(order_disp or order, display_name)
        sci_kw_ids    = (
            cat.ensure_scientific_keywords(order, family, sci_name)
            if order and family and sci_name else None
        )

        for image_path in image_paths:
            image_id = _find_image_id(cat, image_path)
            if image_id is None:
                log.error(f"Not found in catalog: {image_path}")
                errors += 1
                continue

            existing_flat = _existing_flat_tags(clf_log, image_id)

            removed = cat.remove_auto_classifications(image_id)
            if removed:
                log.debug(f"  {image_path.name}: removed {removed} existing keyword(s)")

            cat.tag_image(image_id, top_id)
            cat.tag_image(image_id, ord_id)
            cat.tag_image(image_id, kw_id)
            if sci_kw_ids:
                for kid in sci_kw_ids:
                    cat.tag_image(image_id, kid)
            cat.tag_image(image_id, band_kw_id)
            cat.tag_image(image_id, manual_kw_id)

            # XMP + log outside the catalog context is fine, but do them here
            # so a keyboard interrupt doesn't leave catalog and XMP out of sync.
            clean_xmp_keywords(image_path, existing_flat)
            write_bird_keywords(
                image_path, display_name, order_disp or order, order, family, sci_name
            )
            clf_log.record(image_id, [manual_pred], model="manual")

            log.info(f"Tagged: {image_path.name} → {display_name}")

    clf_log.close()

    total = len(image_paths)
    ok    = total - errors
    log.info(f"Done. {ok}/{total} image(s) tagged as {display_name!r}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
