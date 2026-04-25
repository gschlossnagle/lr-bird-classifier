"""
Utilities for loading camera RAW and Photoshop files as PIL Images.
Falls back to standard PIL loading for JPEG/PNG/TIFF.
"""

from io import BytesIO
from pathlib import Path
from PIL import Image, ImageOps

RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".nef", ".orf", ".raf", ".rw2", ".dng", ".pef", ".srw"}
PSD_EXTENSIONS = {".psd", ".psb"}


def load_image(path: str | Path) -> Image.Image:
    """
    Load an image from *path* as an RGB PIL Image.

    Handles:
      - Camera RAW formats (ARW, CR2, NEF, DNG, …) via rawpy thumbnail extraction
        first, then embedded-preview fallback, then full raw decode
      - Photoshop documents (PSD, PSB) by extracting the embedded JPEG
        preview via exiftool
      - Standard formats (JPEG, PNG, TIFF) via Pillow
    """
    path = Path(path)
    if path.suffix.lower() in RAW_EXTENSIONS:
        return _load_raw(path)
    if path.suffix.lower() in PSD_EXTENSIONS:
        return _load_psd(path)
    return _open_pil_image(path)


def _load_psd(path: Path) -> Image.Image:
    """
    Load a PSD or PSB file by extracting its embedded JPEG preview.

    The preview is a full-resolution JPEG composite written by Photoshop
    whenever "Maximize Compatibility" is enabled (the default).  It is
    more than sufficient for classification — rawpy is not required.
    """
    from .preview import jpeg_preview   # local import keeps startup fast

    with jpeg_preview(path) as tmp:
        return _open_pil_image(tmp)


def _load_raw(path: Path) -> Image.Image:
    image = _load_raw_thumbnail(path)
    if image is not None:
        return image

    try:
        from .preview import jpeg_preview   # local import keeps startup fast

        with jpeg_preview(path) as tmp:
            return _open_pil_image(tmp)
    except Exception:
        pass

    try:
        import rawpy
    except ImportError as e:
        raise ImportError(
            "rawpy is required to load RAW files. Install with: pip install rawpy"
        ) from e

    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)

    return Image.fromarray(rgb)


def _load_raw_thumbnail(path: Path) -> Image.Image | None:
    try:
        import rawpy
    except ImportError:
        return None

    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
    except (
        rawpy.LibRawNoThumbnailError,
        rawpy.LibRawUnsupportedThumbnailError,
        rawpy.LibRawIOError,
        rawpy.LibRawFileUnsupportedError,
        rawpy.LibRawError,
    ):
        return None

    if thumb.format == rawpy.ThumbFormat.JPEG:
        return _open_pil_image(BytesIO(thumb.data))
    if thumb.format == rawpy.ThumbFormat.BITMAP:
        return Image.fromarray(thumb.data).convert("RGB")
    return None


def _open_pil_image(source) -> Image.Image:
    image = Image.open(source)
    return ImageOps.exif_transpose(image).convert("RGB")
