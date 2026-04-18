"""
Utilities for loading camera RAW files (e.g. Sony ARW) as PIL Images.
Falls back to standard PIL loading for JPEG/PNG/TIFF.
"""

from pathlib import Path
from PIL import Image

RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2", ".dng", ".pef", ".srw"}


def load_image(path: str | Path) -> Image.Image:
    """
    Load an image from *path* as an RGB PIL Image.

    Handles camera RAW formats (ARW, CR2, NEF, etc.) via rawpy,
    and standard formats (JPEG, PNG, TIFF) via Pillow.
    """
    path = Path(path)
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _load_raw(path)
    return Image.open(path).convert("RGB")


def _load_raw(path: Path) -> Image.Image:
    try:
        import rawpy
        import numpy as np
    except ImportError as e:
        raise ImportError(
            "rawpy is required to load RAW files. Install with: pip install rawpy"
        ) from e

    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)

    return Image.fromarray(rgb)
