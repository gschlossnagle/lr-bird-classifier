"""
Build a canonical label inventory from existing region-species JSON files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data" / "region_species"


def collect_labels(region_files: list[Path]) -> list[str]:
    labels: set[str] = set()
    for path in region_files:
        data = json.loads(path.read_text())
        labels.update(data.get("labels", []))
    return sorted(labels)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a canonical label inventory from region-species files.")
    p.add_argument("--output", required=True, help="Path to the output text file")
    p.add_argument(
        "--region",
        action="append",
        default=[],
        help="Specific region filename stem to include, e.g. north_america (repeatable)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.region:
        region_files = [DATA_DIR / f"{name}.json" for name in args.region]
    else:
        region_files = sorted(DATA_DIR.glob("*.json"))

    missing = [str(path) for path in region_files if not path.exists()]
    if missing:
        raise SystemExit(f"Missing region file(s): {', '.join(missing)}")

    labels = collect_labels(region_files)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{label}\n" for label in labels), encoding="utf-8")
    print(f"Wrote {len(labels)} labels to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
