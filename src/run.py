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
        help=(
            "Comma-separated file formats to classify (default: RAW,DNG). "
            "Add PSD to include Photoshop documents — previews are extracted "
            "via exiftool so 'Maximize Compatibility' must be enabled in PS."
        ),
    )
    p.add_argument(
        "--folder",
        default=None,
        help=(
            "Filter images by folder path. "
            "If it starts with '/' it is treated as an absolute path prefix "
            "(e.g. /Volumes/FastDrive/Photos/2024/Birds) so you can target a "
            "specific disk. Otherwise it is a substring match against the full "
            "path (e.g. 'Birds' or '2024/Hawks')."
        ),
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
        "--min-stars",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Only classify images with a Lightroom star rating of at least N (1–5). "
            "Unrated images are excluded when this option is set."
        ),
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
    p.add_argument(
        "--retag-below-confidence",
        type=float,
        default=None,
        metavar="THRESHOLD",
        help=(
            "Re-classify images whose previously recorded best confidence is "
            "below THRESHOLD (0-1).  Existing auto-classification keywords are "
            "removed before re-tagging.  Requires a classification log from a "
            "prior run."
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def lightroom_is_running(catalog_path: Path) -> bool:
    """
    Return True if Lightroom currently has *catalog_path* open.

    Lightroom writes a <catalog>.lrcat.lock file containing its app path and
    PID while the catalog is open, and removes it on clean exit.  This is more
    reliable than checking the process list because it is catalog-specific and
    works regardless of how the process is named on different OS versions.
    """
    lock = catalog_path.with_suffix(".lrcat.lock")
    if not lock.exists():
        return False

    # Confirm the PID recorded in the lock file is still alive.
    # If LR crashed the lock file may be stale, so we don't want to block.
    try:
        text = lock.read_text().strip()
        lines = text.splitlines()
        if len(lines) >= 2:
            pid = int(lines[1].strip())
            import os
            try:
                os.kill(pid, 0)   # signal 0: check existence without sending a signal
                return True        # process exists → LR really is running
            except ProcessLookupError:
                log.warning(
                    f"Stale lock file found ({lock.name}) — "
                    "Lightroom may have crashed. Proceeding anyway."
                )
                return False
    except Exception:
        pass

    # Lock file exists but we couldn't parse it — be safe and block.
    return True


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    from src.catalog import LightroomCatalog, confidence_band
    from src.classification_log import ClassificationLog
    from src.classifier import Classifier
    from src.geo_filter import GeoFilter, normalize_region, resolve_region_from_coords
    from src.taxonomy import get_order_display_name, parse_label
    from src.xmp_writer import write_bird_keywords

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        log.error(f"Catalog not found: {catalog_path}")
        return 1

    if not args.dry_run and lightroom_is_running(catalog_path):
        log.error(
            "Lightroom is currently open. Close it before running this script — "
            "concurrent writes will be lost or corrupt the catalog."
        )
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
            min_rating=args.min_stars,
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

        # --retag-below-confidence: pull qualifying images out of already_tagged
        clf_log = ClassificationLog(catalog_path)
        retag_candidates: set[int] = set()
        if args.retag_below_confidence is not None:
            retag_candidates = clf_log.get_images_below_confidence(
                args.retag_below_confidence
            )
            # Only retag images that are actually in our already_tagged set
            # (i.e. ones we previously auto-tagged, not manually-tagged images)
            retag_candidates &= already_tagged
            already_tagged -= retag_candidates
            log.info(
                f"--retag-below-confidence {args.retag_below_confidence:.0%}: "
                f"{len(retag_candidates)} image(s) queued for re-classification"
            )

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
        tagged = skipped = geo_skipped = errors = no_bird = already = manually = retagged = 0
        clf_model_str = f"{clf.network}/{clf.tag}"

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

            is_retag = img.id_local in retag_candidates

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

            if not args.dry_run:
                # Strip old auto-classification tags before re-tagging
                if is_retag:
                    removed = cat.remove_auto_classifications(img.id_local)
                    log.debug(f"[{i}/{len(images)}] RETAG    removed {removed} old tag(s)")

                newly = False
                for pred in confident:
                    parsed = parse_label(pred.label)
                    order = parsed.get("order", "")
                    family = parsed.get("family", "")
                    order_display = get_order_display_name(order) if order else ""

                    # Quick-access keyword: Bird-Species > {common_name}
                    top_id = cat.ensure_bird_species_keyword(pred.common_name)
                    if cat.tag_image(img.id_local, top_id):
                        newly = True

                    # Birds > Order > {order_display} > {common_name}
                    ord_id, kw_id = cat.ensure_species_keyword(
                        order_display or order, pred.common_name
                    )
                    cat.tag_image(img.id_local, kw_id)
                    cat.tag_image(img.id_local, ord_id)

                    # Birds > Scientific > {order} > {family} > {sci_name}
                    if order and family and pred.sci_name:
                        ord_kw_id, fam_kw_id, spc_kw_id = cat.ensure_scientific_keywords(
                            order, family, pred.sci_name
                        )
                        cat.tag_image(img.id_local, ord_kw_id)
                        cat.tag_image(img.id_local, fam_kw_id)
                        cat.tag_image(img.id_local, spc_kw_id)

                    # Keep XMP sidecar in sync
                    write_bird_keywords(
                        path,
                        pred.common_name,
                        order_display or order,
                        order,
                        family,
                        pred.sci_name,
                    )

                # Record all confident predictions in the classification log
                clf_log.record(img.id_local, confident, clf_model_str)

                # Write confidence band keyword for LR smart-collection filtering
                best_conf = max(p.confidence for p in confident)
                band_kw_id = cat.ensure_confidence_keyword(confidence_band(best_conf))
                cat.tag_image(img.id_local, band_kw_id)

                status = "RETAG   " if is_retag else ("TAGGED  " if newly else "EXISTING")
                label_str = "  |  ".join(
                    f"{p.common_name} ({p.sci_name}) {p.confidence:.1%}"
                    for p in confident
                )
                log.info(f"[{i}/{len(images)}] {status}  {path.name}  →  {label_str}")
                if is_retag:
                    retagged += 1
            else:
                for pred in confident:
                    parsed = parse_label(pred.label)
                    order = parsed.get("order", "")
                    family = parsed.get("family", "")
                    order_display = get_order_display_name(order) if order else ""
                    label = (
                        f"{pred.common_name} ({pred.sci_name})  {pred.confidence:.1%}"
                        f"  [{order_display} / {family}]"
                    )
                    log.info(f"[{i}/{len(images)}] DRY-RUN  {path.name}  →  {label}")

            tagged += 1

    summary = clf_log.confidence_summary() if not args.dry_run else {}
    clf_log.close()

    log.info(
        f"\nDone.  tagged={tagged}  retagged={retagged}  no_bird={no_bird}"
        f"  skip_tagged={already}  manually_classed={manually}"
        f"  missing={skipped}  errors={errors}"
    )
    if summary and summary.get("total", 0) > 0:
        log.info(
            f"Confidence (all-time):  mean={summary['mean']:.0%}"
            f"  ≥90%={summary['pct_above_90']:.0%}"
            f"  ≥75%={summary['pct_above_75']:.0%}"
            f"  ≥50%={summary['pct_above_50']:.0%}"
            f"  <50%={summary['pct_below_50']:.0%}"
            f"  ({summary['total']} images)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
