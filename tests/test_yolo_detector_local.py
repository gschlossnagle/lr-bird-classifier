from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class _FakeTensor:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return self._values


class _FakeBoxes:
    def __init__(self):
        self.xyxy = _FakeTensor([[10.2, 20.4, 110.6, 220.1], [1.0, 2.0, 5.0, 6.0]])
        self.conf = _FakeTensor([0.91, 0.80])
        self.cls = _FakeTensor([14, 0])


class _FakeResult:
    def __init__(self):
        self.orig_shape = (400, 800)
        self.boxes = _FakeBoxes()


class _FakeYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        return [_FakeResult()]


class YoloDetectorTest(unittest.TestCase):
    def test_detect_filters_to_birds(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt")
            with patch(
                "src.detectors.yolo.load_image",
                return_value=Image.new("RGB", (800, 400)),
            ):
                detections = detector.detect(Path("/tmp/fake.jpg"))
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].detected_class, "bird")
        self.assertEqual(detections[0].x1, 10)
        self.assertEqual(detections[0].y2, 220)
        self.assertGreater(detections[0].area_fraction, 0)


if __name__ == "__main__":
    unittest.main()
