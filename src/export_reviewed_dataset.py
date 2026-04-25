"""
CLI for exporting reviewed annotations into benchmark manifests or
materialized standalone datasets.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .export_manifest import ManifestExporter
from .review_store import ReviewStore


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export reviewed candidates into benchmark datasets.")
    p.add_argument("--db", required=True, help="Path to the review SQLite database")
    p.add_argument(
        "--source-type",
        required=True,
        choices=["catalog_real_world", "catalog_stress", "catalog_reject"],
        help="Which reviewed subset to export",
    )
    p.add_argument(
        "--output",
        required=True,
        help="Output JSONL path for manifest-only export, or output directory for materialized export",
    )
    p.add_argument(
        "--materialize",
        action="store_true",
        help="Write standalone JPEG assets plus a manifest into the output directory",
    )
    p.add_argument("--export-max-size-bytes", type=int, default=None)
    p.add_argument("--export-quality-floor", type=int, default=None)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    with ReviewStore(args.db) as store:
        exporter = ManifestExporter(store)
        if args.materialize:
            count = exporter.export_materialized_dataset(
                args.source_type,
                args.output,
                export_max_size_bytes=args.export_max_size_bytes,
                export_quality_floor=args.export_quality_floor,
            )
            print(
                f"Exported {count} reviewed samples to "
                f"{Path(args.output).resolve() / f'{args.source_type}.jsonl'}"
            )
        else:
            count = exporter.export_jsonl(args.source_type, args.output)
            print(f"Exported {count} reviewed samples to {Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
