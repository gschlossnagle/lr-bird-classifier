"""
Remove auto-classification keywords written by lr-bird-classifier.

Deletes all keywords under the Bird-Species, Birds, and Classifier-Confidence
hierarchies from the specified images.  Filters can be combined freely:

    # Remove all auto-classification tags from every tagged image
    python -m src.wipe /path/to/catalog.lrcat

    # Remove only images in a specific folder
    python -m src.wipe /path/to/catalog.lrcat --folder "2024/Costa Rica"

    # Remove only images whose best recorded confidence is below 50%
    python -m src.wipe /path/to/catalog.lrcat --below-confidence 0.5

    # Remove all images misidentified as a specific species
    python -m src.wipe /path/to/catalog.lrcat --species "Penelope purpurascens"

    # Remove that species only when the model was under 60% confident
    python -m src.wipe /path/to/catalog.lrcat --species "Penelope purpurascens" --below-confidence 0.6

    # Combine all three filters
    python -m src.wipe /path/to/catalog.lrcat \\
        --species "Penelope purpurascens" --below-confidence 0.6 --folder "2024/Costa Rica"

    # Preview what would be removed without writing anything
    python -m src.wipe /path/to/catalog.lrcat --species "Penelope purpurascens" --dry-run

The classification log (SQLite sidecar co-located with the catalog) is required
when using --species or --below-confidence.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Remove auto-classification keywords written by lr-bird-classifier. "
            "With no options, removes all auto-tags from every classified image. "
            "Filters (--folder, --species, --below-confidence) can be combined."
        )
    )
    p.add_argument("catalog", help="Path to the .lrcat file")
    p.add_argument(
        "--folder",
        default=None,
        help=(
            "Limit wipe to images in this folder. "
            "Absolute paths (starting with /) match as a prefix; "
            "otherwise substring match against the full path."
        ),
    )
    p.add_argument(
        "--species",
        default=None,
        metavar="SCI_NAME",
        help=(
            "Only remove tags from images where this species was predicted "
            "(matched against the scientific name in the classification log, "
            "case-insensitive). Example: 'Penelope purpurascens'."
        ),
    )
    p.add_argument(
        "--below-confidence",
        type=float,
        default=None,
        metavar="THRESHOLD",
        help=(
            "Only remove tags from images below THRESHOLD (0–1). "
            "When combined with --species, filters on the confidence for that "
            "specific species rather than the image's overall best confidence."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without writing to the catalog",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip catalog backup before writing",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def _require_log(catalog_path: Path, flag: str) -> bool:
    """Return True if the classification log exists, else log an error."""
    log_path = catalog_path.with_name(catalog_path.stem + "_lr_classifier.sqlite")
    if not log_path.exists():
        log.error(
            f"No classification log found at {log_path.name}. "
            f"{flag} requires a log from a prior run."
        )
        return False
    return True


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    from src.catalog import LightroomCatalog
    from src.classification_log import ClassificationLog

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1

    # Validate log availability up front
    needs_log = args.species is not None or args.below_confidence is not None
    if needs_log and not _require_log(catalog_path, "--species or --below-confidence"):
        return 1

    # ------------------------------------------------------------------
    # Resolve the set of image IDs to wipe
    # ------------------------------------------------------------------
    cat = LightroomCatalog.open(
        catalog_path,
        readonly=args.dry_run,
        backup=(not args.dry_run and not args.no_backup),
    )

    with cat:
        targets: set[int] = cat.get_auto_classified_images()
        log.info(f"Found {len(targets)} auto-classified image(s) in catalog")

        # -- Folder filter ------------------------------------------------
        if args.folder:
            folder_ids = {
                img.id_local
                for img in cat.get_images(folder_filter=args.folder)
            }
            targets &= folder_ids
            log.info(
                f"Folder filter '{args.folder}': {len(targets)} classified image(s) in scope"
            )

        # -- Species / confidence filter ----------------------------------
        if args.species or args.below_confidence is not None:
            clf_log = ClassificationLog(catalog_path)

            if args.species:
                # When a species is named, --below-confidence filters on that
                # species' confidence specifically (not the image's best overall).
                species_ids = clf_log.get_images_for_species(
                    args.species,
                    max_confidence=args.below_confidence,
                )
                targets &= species_ids
                if args.below_confidence is not None:
                    log.info(
                        f"Species '{args.species}' below {args.below_confidence:.0%}: "
                        f"{len(targets)} image(s) qualify"
                    )
                else:
                    log.info(
                        f"Species '{args.species}': {len(targets)} image(s) qualify"
                    )
            else:
                # No species specified: --below-confidence filters on best overall.
                below = clf_log.get_images_below_confidence(args.below_confidence)
                targets &= below
                log.info(
                    f"--below-confidence {args.below_confidence:.0%}: "
                    f"{len(targets)} image(s) qualify"
                )

            clf_log.close()

        if not targets:
            log.info("Nothing to wipe.")
            return 0

        # ------------------------------------------------------------------
        # Wipe
        # ------------------------------------------------------------------
        wiped = skipped = 0
        for image_id in sorted(targets):
            if args.dry_run:
                log.info(f"DRY-RUN  would wipe image_id={image_id}")
                wiped += 1
            else:
                removed = cat.remove_auto_classifications(image_id)
                if removed:
                    log.debug(f"Wiped image_id={image_id} ({removed} tag(s) removed)")
                    wiped += 1
                else:
                    skipped += 1

        action = "Would wipe" if args.dry_run else "Wiped"
        log.info(f"\nDone.  {action} {wiped} image(s).  Skipped {skipped}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
