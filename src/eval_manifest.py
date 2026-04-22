"""
Benchmark manifest loading and validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .review_validation import validate_manifest_row


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a JSONL benchmark manifest."""
    manifest_path = Path(path)
    rows: list[dict[str, Any]] = []
    with manifest_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{manifest_path}:{lineno}: invalid JSON: {e}") from e
            validate_manifest_row(row)
            rows.append(row)
    return rows


def write_manifest(path: str | Path, rows: list[dict[str, Any]]) -> None:
    """Write a validated JSONL benchmark manifest."""
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as f:
        for row in rows:
            validate_manifest_row(row)
            f.write(json.dumps(row, sort_keys=True) + "\n")
