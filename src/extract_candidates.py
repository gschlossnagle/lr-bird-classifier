"""
CLI entry point for detector-backed review candidate extraction.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
from pathlib import Path

from .catalog import LightroomCatalog
from .catalog_extract import CatalogExtractor
from .review_assets import BoxedPreviewProvider
from .review_store import ReviewStore

log = logging.getLogger(__name__)


def _load_object(import_path: str):
    module_name, _, attr_name = import_path.rpartition(".")
    if not module_name:
        raise ValueError(f"Invalid import path '{import_path}'")
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract review candidates from a Lightroom catalog.")
    p.add_argument("--catalog", required=True, help="Path to the Lightroom .lrcat catalog")
    p.add_argument("--db", required=True, help="Path to the review SQLite database")
    p.add_argument("--preview-dir", required=True, help="Directory for generated preview JPEGs")
    p.add_argument(
        "--detector",
        required=True,
        help="Import path for a detector class or factory, e.g. package.module.Detector",
    )
    p.add_argument("--limit", type=int, default=None, help="Limit the number of catalog images processed")
    p.add_argument("--folder", default=None, help="Optional folder filter passed through to catalog enumeration")
    p.add_argument("--min-stars", type=int, default=None, help="Optional minimum Lightroom rating")
    p.add_argument(
        "--rescan",
        action="store_true",
        help="Ignore any saved extraction cursor and rescan the selected scope from the beginning",
    )
    p.add_argument(
        "--formats",
        default="RAW,DNG,JPEG,TIFF",
        help="Comma-separated Lightroom file formats to scan (default excludes PSD/PSB)",
    )
    p.add_argument("--max-preview-dimension", type=int, default=2048)
    p.add_argument("--jpeg-quality", type=int, default=85)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def _scope_key(
    *,
    catalog: str,
    folder: str | None,
    min_stars: int | None,
    formats: set[str],
) -> str:
    payload = {
        "catalog": str(Path(catalog).resolve()),
        "folder": folder or "",
        "min_stars": min_stars,
        "formats": sorted(formats),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    detector_factory = _load_object(args.detector)
    detector = detector_factory()
    provider = BoxedPreviewProvider(
        args.preview_dir,
        max_dimension=args.max_preview_dimension,
        jpeg_quality=args.jpeg_quality,
    )
    formats = {part.strip().upper() for part in args.formats.split(",") if part.strip()}
    created_at = __import__("datetime").datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    started = time.monotonic()

    with ReviewStore(Path(args.db)) as store:
        extractor = CatalogExtractor(store, detector, provider)
        scope_key = _scope_key(
            catalog=args.catalog,
            folder=args.folder,
            min_stars=args.min_stars,
            formats=formats,
        )
        start_after_id: int | None = None
        if args.rescan:
            store.clear_extraction_cursor(scope_key)
            log.info("Rescanning scope from the beginning")
        else:
            cursor = store.get_extraction_cursor(scope_key)
            if cursor is not None:
                start_after_id = int(cursor)
                log.info("Resuming scope after catalog image id %s", start_after_id)

        with LightroomCatalog.open(args.catalog, readonly=True, backup=False) as cat:
            if args.rescan:
                scoped_images = cat.get_images(
                    formats=formats,
                    folder_filter=args.folder,
                    min_rating=args.min_stars,
                    start_after_id=None,
                    limit=args.limit,
                )
                deleted = store.delete_images_by_source_paths(
                    [str(Path(image.file_path).resolve()) for image in scoped_images]
                )
                log.info("Cleared %s previously extracted image rows for rescan scope", deleted)
            total_images = cat.count_images(
                formats=formats,
                folder_filter=args.folder,
                min_rating=args.min_stars,
                start_after_id=start_after_id,
                limit=args.limit,
            )

        log.info(
            "Starting extraction: %s images queued%s",
            total_images,
            f" (limit={args.limit})" if args.limit is not None else "",
        )

        images_inserted, candidates_inserted, last_image_id_processed = extractor.extract(
            args.catalog,
            formats=formats,
            folder_filter=args.folder,
            min_rating=args.min_stars,
            start_after_id=start_after_id,
            limit=args.limit,
            created_at=created_at,
        )
        if last_image_id_processed is not None:
            store.set_extraction_cursor(scope_key, str(last_image_id_processed), created_at)

    elapsed = max(time.monotonic() - started, 1e-6)
    log.info(
        "Extraction complete: %s/%s images scanned, %s candidates created in %.1fs (%.2f images/s)",
        images_inserted,
        total_images,
        candidates_inserted,
        elapsed,
        images_inserted / elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
