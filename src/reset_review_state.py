"""
Reset human review state while preserving extracted candidates and preview assets.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .review_store import ReviewStore


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reset review state in a review SQLite database.")
    p.add_argument("--db", required=True, help="Path to the review SQLite database")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"Review DB does not exist: {db_path}")

    with ReviewStore(db_path) as store:
        store.reset_review_state()

    print(f"Review state reset: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
