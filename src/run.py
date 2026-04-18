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

    # Force a region hint (country code or region name):
    python -m src.run catalog.lrcat --region US
    python -m src.run catalog.lrcat --region europe

    # Skip geo filtering entirely:
    python -m src.run catalog.lrcat --no-geo-filter

    # Re-classify images that already have species tags:
    python -m src.run catalog.lrcat --no-skip-tagged
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
    p.add_argument(
        "--region",
        default=None,
        metavar="REGION",
        help=(
            "Region or country-code hint used when photos have no GPS EXIF data. "
            "Examples: north_america, europe, US, GB, AU. "
            "Defaults to north_america when no GPS is found."
        ),
    )
    p.add_argument(
        "--no-geo-filter",
        action="store_true",
        help="Disable geographic species filtering entirely",
    )
    p.add_argument(
        "--no-skip-tagged",
        action="store_true",
        help="Re-classify images that already have species keywords (default: skip them)",
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
    from src.geo_filter import GeoFilter, normalize_region, resolve_region_from_coords
    from src.taxonomy import get_group_tag

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1

    formats = {f.strip().upper() for f in args.formats.split(",")}
    log.info(
        f"Formats: {formats}  |  min confidence: {args.min_confidence:.0%}"
        f"  |  dry-run: {args.dry_run}"
        f"  |  geo-filter: {not args.no_geo_filter}"
        f"  |  skip-tagged: {not args.no_skip_tagged}"
    )

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

        # ------------------------------------------------------------------
        # Idempotency: collect images to skip
        # ------------------------------------------------------------------
        manually_classed: set[int] = cat.get_manually_classed_images()
        already_tagged: set[int] = set()
        if not args.no_skip_tagged:
            already_tagged = cat.get_species_tagged_images()

        log.info(
            f"Skipping {len(manually_classed)} manually-classed"
            f" + {len(already_tagged)} already-tagged images"
        )

        # ------------------------------------------------------------------
        # Geo filter setup
        # ------------------------------------------------------------------
        geo_filter: GeoFilter | None = None

        if not args.no_geo_filter:
            # 1. Try GPS EXIF from the batch of images
            image_ids = [img.id_local for img in images]
            gps = cat.get_first_gps(image_ids)

            if gps:
                lat, lon = gps
                region = resolve_region_from_coords(lat, lon)
                log.info(
                    f"Using GPS-derived region: {region} "
                    f"(from coordinates {lat:.4f}, {lon:.4f})"
                )
            elif args.region:
                region = normalize_region(args.region)
                log.info(f"Using --region hint: {region}")
            else:
                region = "north_america"
                log.info("No GPS or --region hint; defaulting to north_america")

            geo_filter = GeoFilter(region)
            if geo_filter.active:
                log.info(
                    f"Geo filter active: {region} "
                    f"({len(geo_filter._whitelist)} species)"  # type: ignore[arg-type]
                )
            else:
                log.warning(
                    f"Geo filter inactive (no species list for '{region}'). "
                    f"Run: python -m src.build_region_lists {region}"
                )

        # ------------------------------------------------------------------
        # Build optional path remapper
        # ------------------------------------------------------------------
        remap_from, remap_to = None, None
        if args.remap:
            parts = args.remap.split(":", 1)
            if len(parts) == 2:
                remap_from, remap_to = parts
                log.info(f"Path remap: '{remap_from}' → '{remap_to}'")
            else:
                log.warning(f"Invalid --remap format (expected FROM:TO): {args.remap}")

        # ------------------------------------------------------------------
        # Main classification loop
        # ------------------------------------------------------------------
        tagged = skipped = geo_skipped = errors = no_bird = already = manually = 0

        for i, img in enumerate(images, 1):
            # Skip manually-classed images
            if img.id_local in manually_classed:
                log.debug(f"[{i}/{len(images)}] MANUAL   {img.base_name}.{img.extension}")
                manually += 1
                continue

            # Skip already-tagged images (idempotency)
            if img.id_local in already_tagged:
                log.debug(f"[{i}/{len(images)}] SKIP     {img.base_name}.{img.extension}")
                already += 1
                continue

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

            # Apply geo filter
            if geo_filter and geo_filter.active:
                filtered = geo_filter.filter(preds)
                if len(filtered) < len(preds):
                    removed = [p.common_name for p in preds if p not in filtered]
                    log.debug(
                        f"[{i}/{len(images)}] Geo-filtered out: {', '.join(removed)}"
                    )
                preds = filtered

            # Filter by confidence
            confident = [p for p in preds if p.confidence >= args.min_confidence]

            if not confident:
                best = (
                    f"  (best: {preds[0].common_name} {preds[0].confidence:.0%})"
                    if preds
                    else ""
                )
                log.info(f"[{i}/{len(images)}] NO BIRD  {path.name}{best}")
                no_bird += 1
                continue

            for pred in confident:
                group = get_group_tag(pred.common_name)
                label = (
                    f"{pred.common_name} ({pred.sci_name})  {pred.confidence:.1%}"
                    f"  [group: {group}]"
                )
                if args.dry_run:
                    log.info(f"[{i}/{len(images)}] DRY-RUN  {path.name}  →  {label}")
                else:
                    # Species keyword under Birds > Species
                    kw_id = cat.ensure_keyword(pred.common_name)
                    newly = cat.tag_image(img.id_local, kw_id)

                    # Group keyword under Birds > Group
                    grp_id = cat.ensure_group_keyword(group)
                    cat.tag_image(img.id_local, grp_id)

                    status = "TAGGED  " if newly else "EXISTING"
                    log.info(
                        f"[{i}/{len(images)}] {status}  {path.name}  →  {label}"
                    )
                tagged += 1

    log.info(
        f"\nDone.  tagged={tagged}  no_bird={no_bird}"
        f"  skip_tagged={already}  manually_classed={manually}"
        f"  missing={skipped}  errors={errors}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
