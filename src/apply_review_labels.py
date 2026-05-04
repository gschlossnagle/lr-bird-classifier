"""
CLI for bulk-applying reviewed labels from review.db back to Lightroom.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .review_apply import ApplyEngine, ApplyPolicy
from .review_apply_state import ReviewApplyState
from .review_store import ReviewStore

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply reviewed labels from a review SQLite DB to a Lightroom catalog.")
    p.add_argument("--db", required=True, help="Path to the review SQLite database")
    p.add_argument("--catalog", required=True, help="Path to the Lightroom catalog")
    p.add_argument("--state-db", required=True, help="Path to the apply-state SQLite database")
    p.add_argument("--scope", default=None, help="Optional review scope key to limit the apply run")
    p.add_argument("--dry-run", action="store_true", help="Compute and report changes without writing to Lightroom")
    p.add_argument("--force-reapply", action="store_true", help="Apply even when the catalog already appears current")
    p.add_argument("--allow-multi-species", action="store_true", help="Apply all reviewed species on multi-label images")
    p.add_argument("--exclude-stress", action="store_true", help="Ignore stress-labeled examples when building desired outcomes")
    p.add_argument("--no-mark-manually-classed", action="store_true", help="Do not add the 'manually classed' keyword")
    p.add_argument("--report", default=None, help="Optional JSONL report path")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def _exit_code_for_summary(summary: dict[str, int], *, dry_run: bool) -> int:
    if int(summary.get("errors") or 0) > 0:
        return 1
    if not dry_run and int(summary.get("conflicts") or 0) > 0:
        return 2
    return 0


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    review_db = Path(args.db)
    catalog_path = Path(args.catalog)
    state_db = Path(args.state_db)
    if not review_db.exists():
        log.error("Review DB not found: %s", review_db)
        return 1
    if not catalog_path.exists():
        log.error("Catalog not found: %s", catalog_path)
        return 1

    policy = ApplyPolicy(
        allow_multi_species=args.allow_multi_species,
        include_stress=not args.exclude_stress,
        mark_manually_classed=not args.no_mark_manually_classed,
        force_reapply=args.force_reapply,
        dry_run=args.dry_run,
        scope_key=args.scope,
        report_path=args.report,
    )

    with ReviewStore(review_db) as review_store, ReviewApplyState(state_db) as apply_state:
        engine = ApplyEngine(
            review_store=review_store,
            apply_state=apply_state,
            catalog_path=catalog_path,
            review_db_path=review_db,
            policy=policy,
        )
        summary = engine.run()

    label = "Dry run complete" if args.dry_run else "Apply complete"
    log.info(
        "%s: considered=%s applied=%s would_apply=%s repaired=%s would_repair=%s verified_skipped=%s conflicts=%s no_label=%s errors=%s",
        label,
        summary["considered"],
        summary["applied"],
        summary["would_apply"],
        summary["repaired"],
        summary["would_repair"],
        summary["verified_skipped"],
        summary["conflicts"],
        summary["no_label"],
        summary["errors"],
    )
    exit_code = _exit_code_for_summary(summary, dry_run=args.dry_run)
    if exit_code == 2:
        log.warning("Apply completed with unresolved conflicts. Re-run with a different policy or resolve the reviewed labels.")
    elif exit_code == 1:
        log.error("Apply completed with errors.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
