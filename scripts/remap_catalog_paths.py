#!/usr/bin/env python3
"""
Remap root-folder absolute paths in a Lightroom catalog.

Useful when a catalog has been moved or restored and the AgLibraryRootFolder
paths no longer match the actual location of the files on disk.

Usage:
    python scripts/remap_catalog_paths.py <catalog.lrcat> <old_prefix> <new_prefix>

Examples:
    # After restoring a catalog from Desktop to /tmp:
    python scripts/remap_catalog_paths.py \
        "/tmp/Wildlife 1stars/Wildlife 1stars.lrcat" \
        "/Users/george/Desktop/Wildlife 1stars" \
        "/tmp/Wildlife 1stars"

    # Dry-run (show what would change, write nothing):
    python scripts/remap_catalog_paths.py \
        "/tmp/Wildlife 1stars/Wildlife 1stars.lrcat" \
        "/Users/george/Desktop/Wildlife 1stars" \
        "/tmp/Wildlife 1stars" \
        --dry-run

IMPORTANT: Close Lightroom before running this script.
           A timestamped backup of the catalog is created automatically
           unless you pass --no-backup.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def remap_catalog_paths(
    catalog_path: Path,
    old_prefix: str,
    new_prefix: str,
    *,
    dry_run: bool = False,
    backup: bool = True,
) -> int:
    """
    Replace *old_prefix* with *new_prefix* in AgLibraryRootFolder.absolutePath.

    Returns the number of rows updated.
    """
    if not catalog_path.exists():
        print(f"ERROR: catalog not found: {catalog_path}", file=sys.stderr)
        return -1

    # Normalise: ensure both prefixes end without a trailing slash so the
    # replacement is consistent regardless of how the user typed them.
    old = old_prefix.rstrip("/")
    new = new_prefix.rstrip("/")

    conn = sqlite3.connect(catalog_path)
    conn.row_factory = sqlite3.Row

    # Show current state
    rows = conn.execute(
        "SELECT id_local, absolutePath FROM AgLibraryRootFolder"
    ).fetchall()

    affected = [r for r in rows if r["absolutePath"].startswith(old + "/") or r["absolutePath"] == old]

    if not affected:
        print("No rows match the old prefix — nothing to do.")
        print("\nCurrent root folders:")
        for r in rows:
            print(f"  [{r['id_local']}] {r['absolutePath']}")
        conn.close()
        return 0

    print(f"Found {len(affected)} root folder(s) to update:\n")
    for r in affected:
        updated = new + r["absolutePath"][len(old):]
        print(f"  [{r['id_local']}]  {r['absolutePath']}")
        print(f"           →  {updated}")

    if dry_run:
        print("\nDry-run: no changes written.")
        conn.close()
        return len(affected)

    # Backup before writing
    if backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = catalog_path.with_stem(f"{catalog_path.stem}_backup_{ts}")
        shutil.copy2(catalog_path, backup_path)
        print(f"\nBackup created: {backup_path}")

    # Apply updates
    for r in affected:
        updated = new + r["absolutePath"][len(old):]
        conn.execute(
            "UPDATE AgLibraryRootFolder SET absolutePath = ? WHERE id_local = ?",
            (updated, r["id_local"]),
        )

    conn.commit()
    conn.close()

    print(f"\nUpdated {len(affected)} row(s) successfully.")
    return len(affected)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Remap root-folder absolute paths in a Lightroom catalog.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("catalog", help="Path to the .lrcat file")
    p.add_argument("old_prefix", help="Path prefix to replace (e.g. /Users/george/Desktop/Wildlife)")
    p.add_argument("new_prefix", help="Replacement prefix (e.g. /tmp/Wildlife)")
    p.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    p.add_argument("--no-backup", action="store_true", help="Skip creating a backup")
    args = p.parse_args()

    result = remap_catalog_paths(
        Path(args.catalog),
        args.old_prefix,
        args.new_prefix,
        dry_run=args.dry_run,
        backup=not args.no_backup,
    )
    return 0 if result >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
