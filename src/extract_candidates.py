"""
CLI entry point for detector-backed review candidate extraction.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
from pathlib import Path

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
        "--formats",
        default="RAW,DNG,JPEG,TIFF",
        help="Comma-separated Lightroom file formats to scan (default excludes PSD/PSB)",
    )
    p.add_argument("--max-preview-dimension", type=int, default=2048)
    p.add_argument("--jpeg-quality", type=int, default=85)
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


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

    with ReviewStore(Path(args.db)) as store:
        extractor = CatalogExtractor(store, detector, provider)
        images_inserted, candidates_inserted = extractor.extract(
            args.catalog,
            formats=formats,
            folder_filter=args.folder,
            min_rating=args.min_stars,
            limit=args.limit,
            created_at=created_at,
        )

    log.info(
        "Extraction complete: %s images scanned, %s candidates created",
        images_inserted,
        candidates_inserted,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
