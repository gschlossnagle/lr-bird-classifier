"""
Regular-user attended tagging workflow.
"""

from __future__ import annotations

import argparse
import json
import logging
from http.server import ThreadingHTTPServer
from pathlib import Path
from datetime import UTC, datetime

from .catalog import LightroomCatalog, confidence_band
from .catalog_extract import Detection
from .classification_log import ClassificationLog
from .classifier import Classifier
from .geo_filter import GeoFilter, normalize_region, resolve_region_from_coords
from .label_apply import replace_catalog_species_labels, species_label_from_prediction, write_sidecar_species_labels
from .raw_utils import load_image
from .review_app import ReviewAppHandler, load_label_inventory
from .review_apply import ApplyEngine, ApplyPolicy
from .review_apply_state import ReviewApplyState
from .review_assets import BoxedPreviewProvider
from .review_store import ReviewStore

log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    from .config import load as _load_config

    cfg = _load_config()
    p = argparse.ArgumentParser(description="Run the regular-user attended bird-tagging workflow.")
    p.add_argument("-c", "--catalog", default=cfg.get("catalog"), help="Path to the Lightroom catalog")
    p.add_argument("--review-db", required=True, help="Path to the review SQLite database")
    p.add_argument("--preview-dir", required=True, help="Directory for review preview JPEGs")
    p.add_argument("--labels-file", required=True, help="Canonical labels file used by review_app")
    p.add_argument("--apply-state-db", required=True, help="Path to the apply-state SQLite database")
    p.add_argument("--formats", default=cfg.get("formats", "RAW,DNG"), help="Comma-separated file formats")
    p.add_argument("--folder", default=None, help="Optional folder filter")
    p.add_argument("--min-confidence", type=float, default=cfg.get("min_confidence", 0.25))
    p.add_argument("--auto-apply-threshold", type=float, default=0.90)
    p.add_argument("--model", default=cfg.get("model"), metavar="NETWORK/TAG")
    p.add_argument("--top-k", type=int, default=1)
    p.add_argument("--min-stars", type=int, default=None)
    p.add_argument("--region", default=cfg.get("region"), metavar="REGION")
    p.add_argument("--no-geo-filter", action="store_true")
    p.add_argument("--no-skip-tagged", action="store_true")
    p.add_argument("--retag-below-confidence", type=float, default=None, metavar="THRESHOLD")
    p.add_argument("--remap", default=None, metavar="FROM:TO")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-launch-ui", action="store_true")
    p.add_argument("--no-apply-reviewed", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def make_scope_key(args: argparse.Namespace, catalog_path: Path, formats: set[str], model: str) -> str:
    payload = {
        "catalog_path": str(catalog_path.resolve()),
        "folder": args.folder or "",
        "min_stars": args.min_stars,
        "formats": sorted(formats),
        "model": model,
        "min_confidence": args.min_confidence,
        "auto_apply_threshold": args.auto_apply_threshold,
        "region": "" if args.no_geo_filter else (args.region or ""),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _scope_name(catalog_path: Path, folder: str | None) -> tuple[str, str]:
    catalog_name = catalog_path.stem
    trip_folder = folder or "Entire Catalog"
    return f"{catalog_name} / {trip_folder}", trip_folder


def _full_image_detection(image) -> Detection:
    width, height = image.size
    return Detection(
        detected_class="bird",
        confidence=1.0,
        x1=0,
        y1=0,
        x2=max(width, 1),
        y2=max(height, 1),
        area_fraction=1.0,
    )


def _auto_apply_labels(predictions) -> list:
    return [species_label_from_prediction(predictions[0])]


def _create_classifier(args: argparse.Namespace) -> tuple[Classifier, str]:
    if args.model:
        parts = args.model.split("/", 1)
        if len(parts) != 2:
            raise SystemExit(f"Invalid --model '{args.model}'")
        clf = Classifier(network=parts[0], tag=parts[1], top_k=args.top_k, birds_only=True)
    else:
        clf = Classifier(top_k=args.top_k, birds_only=True)
    return clf, f"{clf.network}/{clf.tag}"


def _launch_review_ui(
    *,
    review_db: Path,
    labels_file: Path,
    host: str,
    port: int,
) -> None:
    from .label_resolver import LabelResolver
    from .review_app import QueueTopUpCoordinator, _utc_now

    labels = load_label_inventory(labels_file)
    if not labels:
        raise SystemExit("No labels loaded from labels file")

    store = ReviewStore(review_db)
    resolver = LabelResolver(labels)
    try:
        from .review_suggester import ReviewSuggester

        suggester = ReviewSuggester()
        suggestion_status = None
    except Exception as exc:
        suggester = None
        suggestion_status = f"Classifier stack is not available in this environment: {exc}"

    class Handler(ReviewAppHandler):
        pass

    Handler.store = store
    Handler.resolver = resolver
    Handler.suggester = suggester
    Handler.suggestion_status = suggestion_status
    Handler.topup_runner = None
    Handler.topup_coordinator = QueueTopUpCoordinator()
    Handler.topup_low_watermark = 0

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        print(f"Review UI: http://{host}:{port}/review")
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Review UI stopped by user")
    finally:
        server.server_close()
        store.close()


def _seed_deferred_candidate(
    *,
    store: ReviewStore,
    provider: BoxedPreviewProvider,
    scope_key: str,
    image_path: Path,
    img,
    confident,
    clf_model_str: str,
    geo_filtered: bool,
) -> None:
    image = load_image(image_path)
    image_row_id = store.insert_image(
        {
            "scope_key": scope_key,
            "source_image_id": img.id_local,
            "source_image_path": str(image_path.resolve()),
            "capture_datetime": img.capture_time,
            "folder": str(image_path.resolve().parent),
            "rating": img.rating,
            "created_at": _utc_now(),
        }
    )
    candidate_id = f"runimg_{img.id_local}"
    preview_path = provider.build_preview_from_image(
        image,
        image_path,
        _full_image_detection(image),
        candidate_id,
        source_size=image.size,
    )
    store.insert_candidate(
        {
            "candidate_id": candidate_id,
            "image_id": image_row_id,
            "detector_name": "review_run",
            "detected_class": "bird",
            "detector_confidence": confident[0].confidence,
            "bbox_x1": 0,
            "bbox_y1": 0,
            "bbox_x2": max(image.size[0], 1),
            "bbox_y2": max(image.size[1], 1),
            "bbox_area_fraction": 1.0,
            "preview_image_path": str(preview_path.resolve()),
            "review_status": "unreviewed",
            "created_at": _utc_now(),
        }
    )
    store.upsert_seed_suggestion(
        candidate_id,
        model=clf_model_str,
        best_truth_label=confident[0].label,
        best_common_name=confident[0].common_name,
        best_sci_name=confident[0].sci_name,
        best_confidence=confident[0].confidence,
        top_predictions=[
            {
                "truth_label": pred.label,
                "common_name": pred.common_name,
                "sci_name": pred.sci_name,
                "confidence": pred.confidence,
            }
            for pred in confident
        ],
        geo_filtered=geo_filtered,
        seeded_at=_utc_now(),
    )


def _run_apply_phase(
    *,
    review_db: Path,
    apply_state_db: Path,
    catalog_path: Path,
    scope_key: str,
    dry_run: bool,
) -> dict[str, int]:
    with ReviewStore(review_db) as review_store, ReviewApplyState(apply_state_db) as apply_state:
        engine = ApplyEngine(
            review_store=review_store,
            apply_state=apply_state,
            catalog_path=catalog_path,
            review_db_path=review_db,
            policy=ApplyPolicy(
                dry_run=dry_run,
                scope_key=scope_key,
                mark_manually_classed=True,
                allow_multi_species=False,
            ),
        )
        return engine.run()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.auto_apply_threshold < args.min_confidence:
        raise SystemExit("--auto-apply-threshold must be greater than or equal to --min-confidence")
    if not args.catalog:
        raise SystemExit("No catalog specified.")

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        raise SystemExit(f"Catalog not found: {catalog_path}")

    formats = {part.strip().upper() for part in args.formats.split(",") if part.strip()}
    clf, clf_model_str = _create_classifier(args)
    scope_key = make_scope_key(args, catalog_path, formats, clf_model_str)
    scope_name, trip_folder = _scope_name(catalog_path, args.folder)
    provider = BoxedPreviewProvider(args.preview_dir)
    review_db_path = Path(args.review_db)
    apply_state_db_path = Path(args.apply_state_db)
    labels_file_path = Path(args.labels_file)

    with ReviewStore(review_db_path) as store:
        store.ensure_scope(
            scope_key=scope_key,
            scope_name=scope_name,
            catalog_name=catalog_path.stem,
            catalog_path=str(catalog_path.resolve()),
            trip_folder=trip_folder,
            workflow_type="run_hybrid_review",
            created_at=_utc_now(),
        )

        with LightroomCatalog.open(
            catalog_path,
            readonly=args.dry_run,
            backup=(not args.dry_run and not args.no_backup),
        ) as cat:
            images = cat.get_images(
                formats=formats,
                folder_filter=args.folder,
                min_rating=args.min_stars,
            )
            manually_classed = cat.get_manually_classed_images()
            already_tagged = set() if args.no_skip_tagged else cat.get_species_tagged_images()
            clf_log = ClassificationLog(catalog_path)
            retag_candidates: set[int] = set()
            if args.retag_below_confidence is not None:
                retag_candidates = clf_log.get_images_below_confidence(args.retag_below_confidence) & already_tagged
                already_tagged -= retag_candidates

            geo_filter: GeoFilter | None = None
            if not args.no_geo_filter:
                gps = cat.get_first_gps([img.id_local for img in images])
                if gps:
                    region = resolve_region_from_coords(*gps)
                elif args.region:
                    region = normalize_region(args.region)
                else:
                    region = "north_america"
                geo_filter = GeoFilter(region)

            remap_from, remap_to = None, None
            if args.remap:
                remap_from, remap_to = args.remap.split(":", 1)

            deferred = auto_applied = skipped = errors = 0
            for img in images:
                if img.id_local in manually_classed:
                    skipped += 1
                    continue
                if img.id_local in already_tagged:
                    skipped += 1
                    continue

                raw_path = img.file_path
                if remap_from and remap_to:
                    raw_path = raw_path.replace(remap_from, remap_to, 1)
                image_path = Path(raw_path)
                if not image_path.exists():
                    skipped += 1
                    continue

                try:
                    preds = clf.predict(image_path)
                except Exception as exc:
                    log.warning("Prediction failed for %s: %s", image_path.name, exc)
                    errors += 1
                    continue
                if geo_filter and geo_filter.active:
                    preds = geo_filter.filter(preds)
                confident = [pred for pred in preds if pred.confidence >= args.min_confidence]
                if not confident:
                    skipped += 1
                    continue

                if confident[0].confidence >= args.auto_apply_threshold:
                    if not args.dry_run:
                        if img.id_local in retag_candidates:
                            cat.remove_auto_classifications(img.id_local)
                        label_payloads = _auto_apply_labels(confident)
                        replace_catalog_species_labels(
                            cat,
                            img.id_local,
                            label_payloads,
                            confidence_band_name=confidence_band(confident[0].confidence),
                            manual=False,
                        )
                        write_sidecar_species_labels(
                            image_path,
                            label_payloads,
                            replace_existing=img.id_local in retag_candidates,
                        )
                        clf_log.record(img.id_local, confident, clf_model_str)
                    auto_applied += 1
                    continue

                _seed_deferred_candidate(
                    store=store,
                    provider=provider,
                    scope_key=scope_key,
                    image_path=image_path,
                    img=img,
                    confident=confident,
                    clf_model_str=clf_model_str,
                    geo_filtered=bool(geo_filter and geo_filter.active),
                )
                deferred += 1

            clf_log.close()

    log.info("review_run seed complete: auto_applied=%s deferred=%s skipped=%s errors=%s", auto_applied, deferred, skipped, errors)

    if deferred and not args.no_launch_ui:
        _launch_review_ui(
            review_db=review_db_path,
            labels_file=labels_file_path,
            host=args.host,
            port=args.port,
        )

    if not args.no_apply_reviewed:
        summary = _run_apply_phase(
            review_db=review_db_path,
            apply_state_db=apply_state_db_path,
            catalog_path=catalog_path,
            scope_key=scope_key,
            dry_run=args.dry_run,
        )
        log.info("review_run apply summary: %s", summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
