"""
Utilities for loading camera RAW and Photoshop files as PIL Images.
Falls back to standard PIL loading for JPEG/PNG/TIFF.
"""

from pathlib import Path
from PIL import Image

RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2", ".dng", ".pef", ".srw"}
PSD_EXTENSIONS = {".psd", ".psb"}


def load_image(path: str | Path) -> Image.Image:
    """
    Load an image from *path* as an RGB PIL Image.

    Handles:
      - Camera RAW formats (ARW, CR2, NEF, DNG, …) via rawpy
      - Photoshop documents (PSD, PSB) by extracting the embedded JPEG
        preview via exiftool
      - Standard formats (JPEG, PNG, TIFF) via Pillow
    """
    path = Path(path)
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _load_raw(path)
    if path.suffix.lower() in PSD_EXTENSIONS:
        return _load_psd(path)
    return Image.open(path).convert("RGB")


def _load_psd(path: Path) -> Image.Image:
    """
    Load a PSD or PSB file by extracting its embedded JPEG preview.

    The preview is a full-resolution JPEG composite written by Photoshop
    whenever "Maximize Compatibility" is enabled (the default).  It is
    more than sufficient for classification — rawpy is not required.
    """
    from .preview import jpeg_preview   # local import keeps startup fast

    with jpeg_preview(path) as tmp:
        return Image.open(tmp).convert("RGB")


def _load_raw(path: Path) -> Image.Image:
    from .preview import jpeg_preview   # local import keeps startup fast

    try:
        with jpeg_preview(path) as tmp:
            return Image.open(tmp).convert("RGB")
    except Exception:
        pass

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
