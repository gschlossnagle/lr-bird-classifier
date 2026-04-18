"""
Remove auto-classification keywords written by lr-bird-classifier.

Deletes all keywords under the Bird-Species, Birds, and Classifier-Confidence
hierarchies from the specified images.  Two modes are supported:

    # Remove all auto-classification tags from every tagged image
    python -m src.wipe /path/to/catalog.lrcat

    # Remove only images whose best recorded confidence is below 50%
    python -m src.wipe /path/to/catalog.lrcat --below-confidence 0.5

    # Preview what would be removed without writing anything
    python -m src.wipe /path/to/catalog.lrcat --dry-run
    python -m src.wipe /path/to/catalog.lrcat --below-confidence 0.5 --dry-run

The classification log (SQLite sidecar co-located with the catalog) is used
to resolve confidence thresholds.  If no log exists, --below-confidence will
report an error.
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
            "With no options, removes all auto-tags from every classified image."
        )
    )
    p.add_argument("catalog", help="Path to the .lrcat file")
    p.add_argument(
        "--below-confidence",
        type=float,
        default=None,
        metavar="THRESHOLD",
        help=(
            "Only remove tags from images whose best recorded confidence is "
            "below THRESHOLD (0–1). Requires a classification log from a prior run."
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

    # ------------------------------------------------------------------
    # Resolve the set of image IDs to wipe
    # ------------------------------------------------------------------
    cat = LightroomCatalog.open(
        catalog_path,
        readonly=args.dry_run,
        backup=(not args.dry_run and not args.no_backup),
    )

    with cat:
        all_classified: set[int] = cat.get_auto_classified_images()
        log.info(f"Found {len(all_classified)} auto-classified image(s) in catalog")

        if args.below_confidence is not None:
            clf_log_path = catalog_path.with_name(
                catalog_path.stem + "_lr_classifier.sqlite"
            )
            if not clf_log_path.exists():
                log.error(
                    f"No classification log found at {clf_log_path.name}. "
                    f"--below-confidence requires a log from a prior run."
                )
                return 1

            clf_log = ClassificationLog(catalog_path)
            below = clf_log.get_images_below_confidence(args.below_confidence)
            clf_log.close()

            # Only wipe images that are both in the catalog and below threshold
            targets = all_classified & below
            log.info(
                f"--below-confidence {args.below_confidence:.0%}: "
                f"{len(targets)} image(s) qualify (of {len(below)} below threshold in log)"
            )
        else:
            targets = all_classified

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
