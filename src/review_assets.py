"""
Preview asset generation for the annotation review workflow.

This is the first concrete preview provider. It renders a JPEG preview with
the current detection box overlaid. For RAW files it currently falls back to
the existing image loader, which is not the final performance strategy but is
good enough to validate the end-to-end flow.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from .catalog_extract import Detection
from .raw_utils import load_image


class BoxedPreviewProvider:
    """Create full-image JPEG previews with the detection box overlaid."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        max_dimension: int = 2048,
        jpeg_quality: int = 85,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_dimension = max_dimension
        self.jpeg_quality = jpeg_quality

    def build_preview(self, image_path: Path, detection: Detection, candidate_id: str) -> Path:
        image = load_image(image_path)
        orig_w, orig_h = image.size
        image.thumbnail((self.max_dimension, self.max_dimension))
        new_w, new_h = image.size

        scale_x = new_w / max(orig_w, 1)
        scale_y = new_h / max(orig_h, 1)

        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [
                round(detection.x1 * scale_x),
                round(detection.y1 * scale_y),
                round(detection.x2 * scale_x),
                round(detection.y2 * scale_y),
            ],
            outline=(255, 64, 64),
            width=6,
        )

        out = self.output_dir / f"{candidate_id}.jpg"
        image.save(out, format="JPEG", quality=self.jpeg_quality, optimize=True)
        return out
