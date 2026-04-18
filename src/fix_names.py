"""
Fix stale common-name keywords written by lr-bird-classifier.

When the classifier ran before the taxonomy cache was fully populated,
bird keywords were written using scientific names as the display name
(e.g. "Toxostoma Curvirostre" instead of "Curve-Billed Thrasher").
This script repairs those tags using the now-complete taxonomy cache.

For each classified image it:
  1. Reads the original iNat21 label from the classification log
  2. Resolves the correct common name from data/taxonomy_cache.json
  3. Swaps stale keywords for correct ones in the Lightroom catalog:
       Bird-Species / {old}              →  Bird-Species / {new}
       Birds / Order  / {order} / {old}  →  Birds / Order  / {order} / {new}
       Birds / Species / {old}           →  Birds / Species / {new}   (if present)
  4. Updates the classification log common_name column

Usage:
    python -m src.fix_names /path/to/catalog.lrcat --dry-run
    python -m src.fix_names /path/to/catalog.lrcat
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent.parent / "data" / "taxonomy_cache.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Fix:
    image_id: int
    label: str
    sci_name: str
    old_name: str   # what is currently in the catalog / log
    new_name: str   # what it should be


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _correct_name(sci_name: str, cache: dict[str, str]) -> str:
    """
    Return the correct title-cased display name for *sci_name*.
    Mirrors the logic in Classifier.predict():
      - common name from cache, title-cased
      - falls back to the sci_name as-is
    """
    common = cache.get(sci_name, "")
    return common.title() if common else sci_name


def _collect_fixes(log_rows: list[dict], cache: dict[str, str]) -> list[Fix]:
    """
    Compare every classification log row against the current cache and
    return Fix objects for rows whose stored common_name is stale.
    """
    fixes: list[Fix] = []
    for row in log_rows:
        sci_name = row.get("sci_name") or ""
        old_name = row.get("common_name") or ""
        if not sci_name:
            continue
        new_name = _correct_name(sci_name, cache)
        if old_name != new_name:
            fixes.append(Fix(
                image_id=row["image_id"],
                label=row["label"],
                sci_name=sci_name,
                old_name=old_name,
                new_name=new_name,
            ))
    return fixes


def _swap_keyword_under(
    cat,
    image_id: int,
    container_id: int,
    old_name: str,
    new_name: str,
    *,
    dry_run: bool,
) -> bool:
    """
    Under *container_id*, replace the *old_name* keyword with *new_name* on
    *image_id* if the old keyword is currently applied.  Returns True if a
    swap was performed (or would be in dry-run).
    """
    old_kw_id = cat.find_keyword_id(old_name, container_id)
    if old_kw_id is None:
        return False  # keyword doesn't exist at all — nothing to fix here

    # Check it's actually applied to this image
    row = cat._conn.execute(
        "SELECT 1 FROM AgLibraryKeywordImage WHERE image = ? AND tag = ?",
        (image_id, old_kw_id),
    ).fetchone()
    if not row:
        return False  # not tagged with the old name

    if not dry_run:
        new_kw_id = cat._get_or_create_keyword(new_name, container_id)
        cat.untag_image(image_id, old_kw_id)
        cat.tag_image(image_id, new_kw_id)
    return True


def _apply_fixes(cat, clf_log, fixes: list[Fix], *, dry_run: bool) -> int:
    """
    Apply all fixes to the catalog and classification log.
    Returns the number of keyword swaps made across all images.
    """
    from .catalog import KEYWORD_BIRD_SPECIES, KEYWORD_ROOT_NAME, KEYWORD_ORDER_NAME, KEYWORD_PARENT_NAME
    from .taxonomy import parse_label, get_order_display_name

    root_id = cat._get_root_keyword_id()

    # Resolve container IDs for the three common-name hierarchies once
    def _find(name, parent_id):
        return cat.find_keyword_id(name, parent_id)

    birds_id = _find(KEYWORD_ROOT_NAME, root_id)
    bs_container = _find(KEYWORD_BIRD_SPECIES, root_id)          # Bird-Species
    order_container = _find(KEYWORD_ORDER_NAME, birds_id) if birds_id else None   # Birds > Order
    species_container = _find(KEYWORD_PARENT_NAME, birds_id) if birds_id else None  # Birds > Species

    swaps = 0
    for fix in fixes:
        parsed = parse_label(fix.label)
        order_raw = parsed.get("order", "")
        order_display = get_order_display_name(order_raw) if order_raw else None

        made_any = False

        # 1. Bird-Species / {name}
        if bs_container:
            if _swap_keyword_under(cat, fix.image_id, bs_container,
                                   fix.old_name, fix.new_name, dry_run=dry_run):
                swaps += 1
                made_any = True

        # 2. Birds / Order / {order} / {name}
        if order_container and order_display:
            order_kw_id = _find(order_display, order_container)
            if order_kw_id:
                if _swap_keyword_under(cat, fix.image_id, order_kw_id,
                                       fix.old_name, fix.new_name, dry_run=dry_run):
                    swaps += 1
                    made_any = True

        # 3. Birds / Species / {name}  (legacy hierarchy, may not exist)
        if species_container:
            if _swap_keyword_under(cat, fix.image_id, species_container,
                                   fix.old_name, fix.new_name, dry_run=dry_run):
                swaps += 1
                made_any = True

        # Update the classification log
        if made_any and not dry_run:
            clf_log.update_common_name(fix.image_id, fix.label, fix.new_name)

        if made_any:
            log.debug(
                f"  image {fix.image_id}: {fix.old_name!r} → {fix.new_name!r}"
            )

    return swaps


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Fix stale common-name keywords in a Lightroom catalog. "
            "Updates Bird-Species and Birds/Order keywords that were written "
            "using scientific names before the taxonomy cache was complete."
        )
    )
    p.add_argument("catalog", help="Path to the .lrcat file")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be changed without writing anything",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip catalog backup before writing",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1

    log_path = catalog_path.with_name(catalog_path.stem + "_lr_classifier.sqlite")
    if not log_path.exists():
        log.error(
            f"Classification log not found: {log_path.name}\n"
            "This script requires a log from a prior classification run."
        )
        return 1

    if not CACHE_PATH.exists():
        log.error(f"Taxonomy cache not found: {CACHE_PATH}")
        return 1

    # Load taxonomy cache
    cache: dict[str, str] = json.loads(CACHE_PATH.read_text())
    log.info(f"Taxonomy cache: {sum(1 for v in cache.values() if v)} named species")

    # Read classification log
    from .classification_log import ClassificationLog
    clf_log = ClassificationLog(catalog_path)
    log_rows = clf_log.get_all_rows()
    log.info(f"Classification log: {len(log_rows)} row(s) across "
             f"{len({r['image_id'] for r in log_rows})} image(s)")

    # Determine what needs fixing
    fixes = _collect_fixes(log_rows, cache)

    if not fixes:
        log.info("All common names are already up to date — nothing to do.")
        clf_log.close()
        return 0

    # Summarise by species change
    by_change: dict[tuple[str, str], list[int]] = defaultdict(list)
    for fix in fixes:
        by_change[(fix.old_name, fix.new_name)].append(fix.image_id)

    affected_images = len({f.image_id for f in fixes})
    log.info(
        f"Found {len(fixes)} classification(s) to update "
        f"across {affected_images} image(s), "
        f"{len(by_change)} distinct name change(s):"
    )
    for (old, new), image_ids in sorted(by_change.items(), key=lambda x: -len(x[1])):
        log.info(f"  {old!r:45s} → {new!r}  ({len(image_ids)} image(s))")

    if args.dry_run:
        print(f"\nDRY RUN — {len(fixes)} fix(es) across {affected_images} image(s). "
              "Pass without --dry-run to apply.")
        clf_log.close()
        return 0

    # Apply
    from .catalog import LightroomCatalog
    cat = LightroomCatalog.open(
        catalog_path,
        readonly=False,
        backup=(not args.no_backup),
    )
    with cat:
        swaps = _apply_fixes(cat, clf_log, fixes, dry_run=False)

    clf_log.close()
    log.info(f"Done. {swaps} keyword swap(s) across {affected_images} image(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
