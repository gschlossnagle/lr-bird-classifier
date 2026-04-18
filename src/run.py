"""
Main entry point: scan a Lightroom catalog, classify bird images,
and write species keywords back into the catalog.

Usage::

    python -m src.run /path/to/catalog.lrcat [options]

    # Dry run (no writes) on RAW+DNG files:
    python -m src.run catalog.lrcat --dry-run

    # Classify everything, min 50% confidence:
    python -m src.run catalog.lrcat --min-confidence 0.5

    # Limit to a specific folder substring:
    python -m src.run catalog.lrcat --folder "VeroBeach"
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Classify birds in a Lightroom catalog and write species keywords."
    )
    p.add_argument("catalog", help="Path to the .lrcat file")
    p.add_argument(
        "--formats",
        default="RAW,DNG",
        help="Comma-separated file formats to classify (default: RAW,DNG)",
    )
    p.add_argument(
        "--folder",
        default=None,
        help="Only classify images whose path contains this string",
    )
    p.add_argument(
        "--min-confidence",
        type=float,
        default=0.25,
        help="Minimum confidence (0-1) to apply a keyword (default: 0.25)",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=1,
        help="Number of top predictions to tag per image (default: 1)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify but do not write to the catalog",
    )
    p.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a catalog backup before writing",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of images to process (useful for testing)",
    )
    p.add_argument(
        "--remap",
        default=None,
        metavar="FROM:TO",
        help="Remap a path prefix in image paths, e.g. '/Volumes/Old:/Volumes/New'",
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
    from src.classifier import Classifier

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1

    formats = {f.strip().upper() for f in args.formats.split(",")}
    log.info(f"Formats: {formats}  |  min confidence: {args.min_confidence:.0%}  |  dry-run: {args.dry_run}")

    # Load classifier
    clf = Classifier(top_k=args.top_k, birds_only=True)

    # Open catalog
    cat = LightroomCatalog.open(
        catalog_path,
        readonly=args.dry_run,
        backup=(not args.dry_run and not args.no_backup),
    )

    with cat:
        images = cat.get_images(
            formats=formats,
            folder_filter=args.folder,
            limit=args.limit,
        )
        log.info(f"Found {len(images)} images to classify")

        tagged = skipped = errors = no_bird = 0

        # Build optional path remapper
        remap_from, remap_to = None, None
        if args.remap:
            parts = args.remap.split(":", 1)
            if len(parts) == 2:
                remap_from, remap_to = parts
                log.info(f"Path remap: '{remap_from}' → '{remap_to}'")
            else:
                log.warning(f"Invalid --remap format (expected FROM:TO): {args.remap}")

        for i, img in enumerate(images, 1):
            raw_path = img.file_path
            if remap_from and remap_to:
                raw_path = raw_path.replace(remap_from, remap_to, 1)
            path = Path(raw_path)
            if not path.exists():
                log.debug(f"[{i}/{len(images)}] MISSING  {path.name}")
                skipped += 1
                continue

            try:
                preds = clf.predict(path)
            except Exception as e:
                log.warning(f"[{i}/{len(images)}] ERROR    {path.name}: {e}")
                errors += 1
                continue

            # Filter by confidence
            confident = [p for p in preds if p.confidence >= args.min_confidence]

            if not confident:
                log.info(
                    f"[{i}/{len(images)}] NO BIRD  {path.name}"
                    + (f"  (best: {preds[0].common_name} {preds[0].confidence:.0%})" if preds else "")
                )
                no_bird += 1
                continue

            for pred in confident:
                label = f"{pred.common_name} ({pred.sci_name})  {pred.confidence:.1%}"
                if args.dry_run:
                    log.info(f"[{i}/{len(images)}] DRY-RUN  {path.name}  →  {label}")
                else:
                    kw_id = cat.ensure_keyword(pred.common_name)
                    newly = cat.tag_image(img.id_local, kw_id)
                    status = "tagged" if newly else "already tagged"
                    log.info(f"[{i}/{len(images)}] {status.upper():14s}  {path.name}  →  {label}")
                tagged += 1

    log.info(
        f"\nDone. tagged={tagged}  no_bird={no_bird}  missing={skipped}  errors={errors}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
