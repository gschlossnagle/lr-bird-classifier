from __future__ import annotations

import json
import math
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


def _parse_length_meters(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().lower()
    if text.endswith(" m"):
        return float(text[:-2].strip())
    if text.endswith("cm"):
        return float(text[:-2].strip()) / 100.0
    if text.endswith(" mm"):
        return float(text[:-3].strip()) / 1000.0
    if text.endswith(" ft"):
        return float(text[:-3].strip()) * 0.3048
    return float(text)


def _parse_length_millimeters(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().lower()
    if text.endswith(" mm"):
        return float(text[:-3].strip())
    if text.endswith("cm"):
        return float(text[:-2].strip()) * 10.0
    return float(text)


@lru_cache(maxsize=2048)
def load_subject_size_metadata(image_path: str | Path) -> dict[str, Any]:
    path = str(Path(image_path).resolve())
    result = subprocess.run(
        [
            "exiftool",
            "-j",
            "-FocusDistance2",
            "-FocalLengthIn35mmFormat",
            "-ImageWidth",
            "-ImageHeight",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        return {}
    rows = json.loads(result.stdout)
    if not rows:
        return {}
    row = rows[0]
    return {
        "focus_distance_m": _parse_length_meters(row.get("FocusDistance2")),
        "focal_length_35mm_mm": _parse_length_millimeters(row.get("FocalLengthIn35mmFormat")),
        "image_width": row.get("ImageWidth"),
        "image_height": row.get("ImageHeight"),
    }


def estimate_subject_box_size_cm(
    *,
    image_width: int,
    image_height: int,
    bbox_x1: int,
    bbox_y1: int,
    bbox_x2: int,
    bbox_y2: int,
    focus_distance_m: float,
    focal_length_35mm_mm: float,
) -> tuple[float, float] | None:
    if (
        image_width <= 0
        or image_height <= 0
        or bbox_x2 <= bbox_x1
        or bbox_y2 <= bbox_y1
        or focus_distance_m <= 0
        or focal_length_35mm_mm <= 0
    ):
        return None

    bbox_width_fraction = (bbox_x2 - bbox_x1) / image_width
    bbox_height_fraction = (bbox_y2 - bbox_y1) / image_height

    horizontal_fov = 2.0 * math.atan(36.0 / (2.0 * focal_length_35mm_mm))
    vertical_fov = 2.0 * math.atan(math.tan(horizontal_fov / 2.0) * (image_height / image_width))

    scene_width_m = 2.0 * focus_distance_m * math.tan(horizontal_fov / 2.0)
    scene_height_m = 2.0 * focus_distance_m * math.tan(vertical_fov / 2.0)

    return (
        scene_width_m * bbox_width_fraction * 100.0,
        scene_height_m * bbox_height_fraction * 100.0,
    )


def estimated_subject_box_size_for_candidate(
    image_path: str | Path,
    candidate: dict[str, Any],
) -> tuple[float, float] | None:
    metadata = load_subject_size_metadata(image_path)
    focus_distance_m = metadata.get("focus_distance_m")
    focal_length_35mm_mm = metadata.get("focal_length_35mm_mm")
    image_width = metadata.get("image_width")
    image_height = metadata.get("image_height")
    if (
        focus_distance_m is None
        or focal_length_35mm_mm is None
        or image_width is None
        or image_height is None
    ):
        return None
    return estimate_subject_box_size_cm(
        image_width=int(image_width),
        image_height=int(image_height),
        bbox_x1=int(candidate["bbox_x1"]),
        bbox_y1=int(candidate["bbox_y1"]),
        bbox_x2=int(candidate["bbox_x2"]),
        bbox_y2=int(candidate["bbox_y2"]),
        focus_distance_m=float(focus_distance_m),
        focal_length_35mm_mm=float(focal_length_35mm_mm),
    )
