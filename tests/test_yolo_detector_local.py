from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from tests.detection_corpus import DETECTION_CORPUS


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


class _FakeDuplicateBoxes:
    def __init__(self):
        self.xyxy = _FakeTensor(
            [
                [1359.0, 719.0, 3821.0, 3167.0],
                [975.0, 706.0, 4896.0, 3345.0],
            ]
        )
        self.conf = _FakeTensor([0.60, 0.39])
        self.cls = _FakeTensor([14, 14])


class _FakeResult:
    def __init__(self):
        self.orig_shape = (400, 800)
        self.boxes = _FakeBoxes()


class _FakeDuplicateResult:
    def __init__(self):
        self.orig_shape = (4000, 6000)
        self.boxes = _FakeDuplicateBoxes()


class _FakeYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        source = kwargs.get("source")
        if isinstance(source, list):
            return [_FakeResult() for _ in source]
        return [_FakeResult()]


class _FakeDuplicateYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        return [_FakeDuplicateResult()]


class _FakeContainedBoxes:
    def __init__(self):
        self.xyxy = _FakeTensor(
            [
                [1800.0, 945.0, 3762.0, 3151.0],
                [1775.0, 1600.0, 2384.0, 1961.0],
            ]
        )
        self.conf = _FakeTensor([0.78, 0.44])
        self.cls = _FakeTensor([14, 14])


class _FakeContainedResult:
    def __init__(self):
        self.orig_shape = (4000, 6000)
        self.boxes = _FakeContainedBoxes()


class _FakeContainedYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        return [_FakeContainedResult()]


class _FakeModerateDuplicateBoxes:
    def __init__(self):
        self.xyxy = _FakeTensor(
            [
                [2450.0, 1312.0, 5445.0, 2395.0],
                [898.0, 1215.0, 5010.0, 2456.0],
            ]
        )
        self.conf = _FakeTensor([0.6682437658309937, 0.33764103055000305])
        self.cls = _FakeTensor([14, 14])


class _FakeModerateDuplicateResult:
    def __init__(self):
        self.orig_shape = (4000, 6000)
        self.boxes = _FakeModerateDuplicateBoxes()


class _FakeModerateDuplicateYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        return [_FakeModerateDuplicateResult()]


class _FakeTinySecondaryBoxes:
    def __init__(self):
        self.xyxy = _FakeTensor(
            [
                [3185.0, 626.0, 4670.0, 2455.0],
                [2330.0, 2777.0, 2869.0, 3234.0],
            ]
        )
        self.conf = _FakeTensor([0.6357280611991882, 0.5373627543449402])
        self.cls = _FakeTensor([14, 14])


class _FakeTinySecondaryResult:
    def __init__(self):
        self.orig_shape = (4000, 6000)
        self.boxes = _FakeTinySecondaryBoxes()


class _FakeTinySecondaryYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        return [_FakeTinySecondaryResult()]


class _FakeTinyWinsBoxes:
    def __init__(self):
        self.xyxy = _FakeTensor(
            [
                [4243.0, 3145.0, 4507.0, 3353.0],
                [3496.0, 3215.0, 4155.0, 3557.0],
                [1220.0, 725.0, 3998.0, 2737.0],
            ]
        )
        self.conf = _FakeTensor([0.525871753692627, 0.43900033831596375, 0.28350672125816345])
        self.cls = _FakeTensor([14, 14, 14])


class _FakeTinyWinsResult:
    def __init__(self):
        self.orig_shape = (4000, 6000)
        self.boxes = _FakeTinyWinsBoxes()


class _FakeTinyWinsYOLO:
    def __init__(self, model):
        self.model = model

    def predict(self, **kwargs):
        return [_FakeTinyWinsResult()]


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

    def test_detect_images_returns_one_result_list_per_image(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt", batch_size=8)
            detections = detector.detect_images(
                [Image.new("RGB", (800, 400)), Image.new("RGB", (800, 400))],
                [Path("/tmp/one.jpg"), Path("/tmp/two.jpg")],
            )
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 2)
        self.assertEqual(len(detections[0]), 1)

    def test_detect_suppresses_near_duplicate_bird_boxes(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeDuplicateYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt")
            with patch(
                "src.detectors.yolo.load_image",
                return_value=Image.new("RGB", (6000, 4000)),
            ):
                detections = detector.detect(Path("/tmp/fake.jpg"))
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].confidence, 0.60)

    def test_detect_suppresses_contained_duplicate_bird_box(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeContainedYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt")
            with patch(
                "src.detectors.yolo.load_image",
                return_value=Image.new("RGB", (6000, 4000)),
            ):
                detections = detector.detect(Path("/tmp/fake.jpg"))
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].confidence, 0.78)

    def test_detect_suppresses_moderate_overlap_when_lower_confidence_is_much_weaker(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeModerateDuplicateYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt")
            with patch(
                "src.detectors.yolo.load_image",
                return_value=Image.new("RGB", (6000, 4000)),
            ):
                detections = detector.detect(Path("/tmp/fake.jpg"))
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].confidence, 0.6682437658309937)

    def test_detect_suppresses_tiny_secondary_fragment_when_large_primary_exists(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeTinySecondaryYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt")
            with patch(
                "src.detectors.yolo.load_image",
                return_value=Image.new("RGB", (6000, 4000)),
            ):
                detections = detector.detect(Path("/tmp/fake.jpg"))
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].confidence, 0.6357280611991882)

    def test_detect_keeps_large_box_when_tiny_fragments_have_higher_confidence(self) -> None:
        ultralytics = types.ModuleType("ultralytics")
        ultralytics.YOLO = _FakeTinyWinsYOLO
        old = sys.modules.get("ultralytics")
        sys.modules["ultralytics"] = ultralytics
        try:
            from src.detectors.yolo import YoloBirdDetector

            detector = YoloBirdDetector(model="fake.pt")
            with patch(
                "src.detectors.yolo.load_image",
                return_value=Image.new("RGB", (6000, 4000)),
            ):
                detections = detector.detect(Path("/tmp/fake.jpg"))
        finally:
            if old is None:
                sys.modules.pop("ultralytics", None)
            else:
                sys.modules["ultralytics"] = old

        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0].confidence, 0.28350672125816345)

    def test_regression_detection_corpus(self) -> None:
        from src.detectors.yolo import YoloBirdDetector

        detector = YoloBirdDetector.__new__(YoloBirdDetector)
        detector.duplicate_iou_threshold = 0.45
        detector.duplicate_containment_threshold = 0.8
        detector.moderate_duplicate_iou_threshold = 0.25
        detector.moderate_duplicate_containment_threshold = 0.45
        detector.moderate_duplicate_confidence_ratio = 0.85
        detector.same_subject_iou_threshold = 0.35
        detector.same_subject_containment_threshold = 0.65
        detector.same_subject_center_distance_ratio = 0.5
        detector.same_subject_area_ratio = 1.8
        detector.tiny_secondary_area_threshold = 0.02
        detector.tiny_secondary_primary_min_area = 0.08
        detector.tiny_secondary_area_ratio = 4.0

        for case in DETECTION_CORPUS:
            with self.subTest(case=case["name"]):
                kept = detector._suppress_near_duplicates(case["detections"])
                self.assertEqual(len(kept), case["expected_count"])


if __name__ == "__main__":
    unittest.main()
