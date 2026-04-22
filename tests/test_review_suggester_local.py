from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.review_suggester import _crop_with_padding


class ReviewSuggesterTest(unittest.TestCase):
    def test_crop_with_padding_expands_bbox(self) -> None:
        image = Image.new("RGB", (100, 100), color=(0, 0, 0))
        crop = _crop_with_padding(image, (20, 20, 40, 40), pad_ratio=0.1)
        self.assertEqual(crop.size, (24, 24))


if __name__ == "__main__":
    unittest.main()
