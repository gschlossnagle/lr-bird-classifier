"""
Classifier-backed suggestion helper for the review UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .raw_utils import load_image


@dataclass(frozen=True)
class SuggestedLabel:
    truth_common_name: str
    truth_sci_name: str
    truth_label: str
    confidence: float


class ReviewSuggester:
    """Generate top-1 classifier suggestions for candidate detections."""

    def __init__(self) -> None:
        from .classifier import Classifier

        self._classifier = Classifier(top_k=1, birds_only=True)

    def suggest(self, image_path: str | Path, bbox: tuple[int, int, int, int]) -> SuggestedLabel | None:
        prediction = self._predict_top1(str(Path(image_path).resolve()), bbox)
        if prediction is None:
            return None
        return SuggestedLabel(
            truth_common_name=prediction.common_name,
            truth_sci_name=prediction.sci_name,
            truth_label=prediction.label,
            confidence=prediction.confidence,
        )

    @lru_cache(maxsize=512)
    def _predict_top1(self, image_path: str, bbox: tuple[int, int, int, int]):
        image = load_image(image_path)
        crop = _crop_with_padding(image, bbox)
        predictions = self._classifier.predict_image(crop, top_k=1)
        return predictions[0] if predictions else None


def _crop_with_padding(image: Image.Image, bbox: tuple[int, int, int, int], pad_ratio: float = 0.08) -> Image.Image:
    x1, y1, x2, y2 = bbox
    width = max(x2 - x1, 1)
    height = max(y2 - y1, 1)
    pad_x = round(width * pad_ratio)
    pad_y = round(height * pad_ratio)
    left = max(x1 - pad_x, 0)
    upper = max(y1 - pad_y, 0)
    right = min(x2 + pad_x, image.width)
    lower = min(y2 + pad_y, image.height)
    return image.crop((left, upper, right, lower)).convert("RGB")
