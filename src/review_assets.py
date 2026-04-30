"""
Preview asset generation for the annotation review workflow.

This is the first concrete preview provider. It renders a JPEG preview with
the current detection box overlaid. For RAW files it currently falls back to
the existing image loader, which is not the final performance strategy but is
good enough to validate the end-to-end flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageStat

from .catalog_extract import Detection
from .raw_utils import load_image
from .subject_size_estimate import load_subject_size_metadata, estimate_subject_box_size_cm


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
        self._cached_path: Optional[Path] = None
        self._cached_image: Optional[Image.Image] = None
        self._cached_size: Optional[tuple[int, int]] = None

    def build_preview(self, image_path: Path, detection: Detection, candidate_id: str) -> Path:
        image_path = image_path.resolve()
        base_image = self._load_base_preview(image_path)
        return self._render_preview(base_image, detection, candidate_id)

    def build_preview_from_image(
        self,
        image,
        image_path: Path,
        detection: Detection,
        candidate_id: str,
        *,
        source_size: tuple[int, int] | None = None,
    ) -> Path:
        self._cached_path = image_path.resolve()
        self._cached_size = source_size or image.size
        self._cached_image = image.copy()
        return self._render_preview(self._cached_image, detection, candidate_id)

    def _render_preview(self, base_image: Image.Image, detection: Detection, candidate_id: str) -> Path:
        image = base_image.copy()
        orig_w, orig_h = self._cached_size or image.size
        if max(image.size) > self.max_dimension:
            image.thumbnail((self.max_dimension, self.max_dimension))
        new_w, new_h = image.size

        scale_x = new_w / max(orig_w, 1)
        scale_y = new_h / max(orig_h, 1)
        scaled_x1 = round(detection.x1 * scale_x)
        scaled_y1 = round(detection.y1 * scale_y)
        scaled_x2 = round(detection.x2 * scale_x)
        scaled_y2 = round(detection.y2 * scale_y)

        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [
                scaled_x1,
                scaled_y1,
                scaled_x2,
                scaled_y2,
            ],
            outline=(255, 64, 64),
            width=6,
        )
        self._draw_box_dimensions(
            image,
            draw,
            image_size=image.size,
            image_path=self._cached_path,
            source_size=(orig_w, orig_h),
            x1=scaled_x1,
            y1=scaled_y1,
            x2=scaled_x2,
            y2=scaled_y2,
            source_x1=int(detection.x1),
            source_y1=int(detection.y1),
            source_x2=int(detection.x2),
            source_y2=int(detection.y2),
        )

        out = self.output_dir / f"{candidate_id}.jpg"
        image.save(out, format="JPEG", quality=self.jpeg_quality, optimize=True)
        return out

    def _draw_box_dimensions(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        *,
        image_size: tuple[int, int],
        image_path: Path | None,
        source_size: tuple[int, int],
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        source_x1: int,
        source_y1: int,
        source_x2: int,
        source_y2: int,
    ) -> None:
        size_cm = self._estimate_box_size_cm(
            image_path=image_path,
            source_size=source_size,
            x1=source_x1,
            y1=source_y1,
            x2=source_x2,
            y2=source_y2,
        )
        if size_cm is None:
            return
        width_label = self._format_real_world_size(size_cm[0])
        height_label = self._format_real_world_size(size_cm[1])
        font = self._label_font(image_size)
        self._draw_centered_label(
            image,
            draw,
            image_size=image_size,
            text=width_label,
            center_x=(x1 + x2) / 2,
            center_y=y2 + 15,
            font=font,
        )
        self._draw_centered_label(
            image,
            draw,
            image_size=image_size,
            text=height_label,
            center_x=x2,
            center_y=(y1 + y2) / 2,
            font=font,
            min_left=x2 + 4,
        )

    def _estimate_box_size_cm(
        self,
        *,
        image_path: Path | None,
        source_size: tuple[int, int],
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> tuple[float, float] | None:
        if image_path is None:
            return None
        metadata = load_subject_size_metadata(image_path)
        focus_distance_m = metadata.get("focus_distance_m")
        focal_length_35mm_mm = metadata.get("focal_length_35mm_mm")
        if focus_distance_m is None or focal_length_35mm_mm is None:
            return None
        return estimate_subject_box_size_cm(
            image_width=int(metadata.get("image_width") or source_size[0]),
            image_height=int(metadata.get("image_height") or source_size[1]),
            bbox_x1=x1,
            bbox_y1=y1,
            bbox_x2=x2,
            bbox_y2=y2,
            focus_distance_m=float(focus_distance_m),
            focal_length_35mm_mm=float(focal_length_35mm_mm),
        )

    def _label_font(self, image_size: tuple[int, int]) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
        font_size = max(16, round(max(image_size) / 64))
        for candidate in (
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
            "/System/Library/Fonts/Supplemental/Helvetica.ttc",
        ):
            if Path(candidate).exists():
                try:
                    return ImageFont.truetype(candidate, font_size)
                except OSError:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _format_real_world_size(size_cm: float) -> str:
        if size_cm >= 100.0:
            return f"{size_cm / 100.0:.2f} m"
        return f"{size_cm:.1f} cm"

    def _draw_centered_label(
        self,
        image: Image.Image,
        draw: ImageDraw.ImageDraw,
        *,
        image_size: tuple[int, int],
        text: str,
        center_x: float,
        center_y: float,
        font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
        min_left: int | None = None,
    ) -> None:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        text_w = right - left
        text_h = bottom - top
        pad_x = 6
        pad_y = 4
        image_w, image_h = image_size
        text_left = round(center_x - text_w / 2)
        text_top = round(center_y - text_h / 2)
        if min_left is not None:
            text_left = max(text_left, min_left)
        box_left = max(0, min(text_left - pad_x, image_w - (text_w + 2 * pad_x)))
        box_top = max(0, min(text_top - pad_y, image_h - (text_h + 2 * pad_y)))
        box_right = box_left + text_w + 2 * pad_x
        box_bottom = box_top + text_h + 2 * pad_y
        text_color, stroke_color = self._label_colors(
            image,
            box=(box_left, box_top, box_right, box_bottom),
        )
        draw.text(
            (box_left + pad_x, box_top + pad_y),
            text,
            font=font,
            fill=text_color,
            stroke_width=2,
            stroke_fill=stroke_color,
        )

    @staticmethod
    def _label_colors(image: Image.Image, *, box: tuple[int, int, int, int]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        crop = image.crop(box).convert("RGB")
        mean = ImageStat.Stat(crop).mean
        luminance = (0.2126 * mean[0]) + (0.7152 * mean[1]) + (0.0722 * mean[2])
        if luminance >= 110:
            return (176, 32, 32), (255, 245, 245)
        return (255, 64, 64), (40, 0, 0)

    def _load_base_preview(self, image_path: Path) -> Image.Image:
        if self._cached_path == image_path and self._cached_image is not None:
            return self._cached_image

        image = load_image(image_path)
        self._cached_size = image.size
        image.thumbnail((self.max_dimension, self.max_dimension))
        self._cached_path = image_path
        self._cached_image = image
        return image
