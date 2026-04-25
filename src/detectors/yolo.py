"""
YOLO-backed bird detector for review candidate extraction.

This implementation targets the Ultralytics YOLO runtime while keeping the
public surface aligned with the lightweight `BirdDetector` protocol used by
`src.catalog_extract`.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..catalog_extract import Detection
from ..raw_utils import load_image
class YoloBirdDetector:
    """
    Detect bird objects using an Ultralytics YOLO model.

    Defaults assume a COCO-style model where class id 14 corresponds to `bird`.
    """

    def __init__(
        self,
        model: str = "yolov8s.pt",
        *,
        batch_size: int = 8,
        confidence_threshold: float = 0.25,
        duplicate_iou_threshold: float = 0.45,
        duplicate_containment_threshold: float = 0.8,
        moderate_duplicate_iou_threshold: float = 0.25,
        moderate_duplicate_containment_threshold: float = 0.45,
        moderate_duplicate_confidence_ratio: float = 0.85,
        same_subject_iou_threshold: float = 0.35,
        same_subject_containment_threshold: float = 0.65,
        same_subject_center_distance_ratio: float = 0.5,
        same_subject_area_ratio: float = 1.8,
        tiny_secondary_area_threshold: float = 0.02,
        tiny_secondary_primary_min_area: float = 0.08,
        tiny_secondary_area_ratio: float = 4.0,
        bird_class_ids: tuple[int, ...] = (14,),
        device: str | None = None,
        imgsz: int = 1280,
    ) -> None:
        self.model_path = model
        self.batch_size = batch_size
        self.confidence_threshold = confidence_threshold
        self.duplicate_iou_threshold = duplicate_iou_threshold
        self.duplicate_containment_threshold = duplicate_containment_threshold
        self.moderate_duplicate_iou_threshold = moderate_duplicate_iou_threshold
        self.moderate_duplicate_containment_threshold = moderate_duplicate_containment_threshold
        self.moderate_duplicate_confidence_ratio = moderate_duplicate_confidence_ratio
        self.same_subject_iou_threshold = same_subject_iou_threshold
        self.same_subject_containment_threshold = same_subject_containment_threshold
        self.same_subject_center_distance_ratio = same_subject_center_distance_ratio
        self.same_subject_area_ratio = same_subject_area_ratio
        self.tiny_secondary_area_threshold = tiny_secondary_area_threshold
        self.tiny_secondary_primary_min_area = tiny_secondary_primary_min_area
        self.tiny_secondary_area_ratio = tiny_secondary_area_ratio
        self.bird_class_ids = set(bird_class_ids)
        self.device = device
        self.imgsz = imgsz
        self.name = f"ultralytics:{model}"

        # Keep Ultralytics settings local to the current workspace unless the
        # caller has explicitly configured a different location.
        default_config_dir = Path.cwd() / ".ultralytics"
        default_config_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("YOLO_CONFIG_DIR", str(default_config_dir))

        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise ImportError(
                "Ultralytics is required for YoloBirdDetector. "
                "Install with: pip install ultralytics"
            ) from e

        self._model = YOLO(model)

    def detect(self, image_path: Path) -> list[Detection]:
        image = load_image(image_path)
        return self.detect_image(image, image_path)

    def detect_image(self, image, image_path: Path | None = None) -> list[Detection]:
        return self.detect_images([image], [image_path] if image_path is not None else [Path("<memory>")])[0]

    def detect_images(self, images: list, image_paths: list[Path]) -> list[list[Detection]]:
        results = self._model.predict(
            source=images,
            conf=self.confidence_threshold,
            verbose=False,
            device=self.device,
            imgsz=self.imgsz,
            batch=min(max(len(images), 1), self.batch_size),
        )
        detections_by_image: list[list[Detection]] = []
        for result in results:
            orig_h, orig_w = result.orig_shape
            image_area = max(orig_h * orig_w, 1)
            boxes = getattr(result, "boxes", None)
            detections: list[Detection] = []
            if boxes is None:
                detections_by_image.append(detections)
                continue

            xyxy = boxes.xyxy.tolist()
            confs = boxes.conf.tolist()
            classes = [int(v) for v in boxes.cls.tolist()]

            for coords, conf, class_id in zip(xyxy, confs, classes):
                if class_id not in self.bird_class_ids:
                    continue
                x1, y1, x2, y2 = [int(round(v)) for v in coords]
                width = max(x2 - x1, 0)
                height = max(y2 - y1, 0)
                detections.append(
                    Detection(
                        detected_class="bird",
                        confidence=float(conf),
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        area_fraction=(width * height) / image_area,
                    )
                )
            detections_by_image.append(self._suppress_near_duplicates(detections))
        return detections_by_image

    def _suppress_near_duplicates(self, detections: list[Detection]) -> list[Detection]:
        """
        Remove heavily overlapping duplicate detections for the same subject.

        Ultralytics already performs NMS, but in practice this workflow still
        sees occasional duplicate bird boxes on a single prominent subject. A
        stricter bird-only pass keeps the review queue from showing the same
        source image twice for effectively the same bird.
        """
        detections = self._suppress_tiny_secondary_fragments(detections)
        kept: list[Detection] = []
        for detection in sorted(detections, key=lambda d: d.confidence, reverse=True):
            if any(self._is_same_subject_duplicate(detection, existing) for existing in kept):
                continue
            kept.append(detection)
        return kept

    def _is_same_subject_duplicate(self, left: Detection, right: Detection) -> bool:
        return self._is_heavy_overlap(left, right) or self._is_close_center_same_subject(left, right)

    def _is_heavy_overlap(self, left: Detection, right: Detection) -> bool:
        overlap = self._overlap_metrics(left, right)
        lower_conf = min(left.confidence, right.confidence)
        higher_conf = max(left.confidence, right.confidence)
        return (
            overlap["iou"] >= self.duplicate_iou_threshold
            or overlap["containment"] >= self.duplicate_containment_threshold
            or (
                (
                    overlap["iou"] >= self.moderate_duplicate_iou_threshold
                    or overlap["containment"] >= self.moderate_duplicate_containment_threshold
                )
                and lower_conf <= (higher_conf * self.moderate_duplicate_confidence_ratio)
            )
        )

    def _is_close_center_same_subject(self, left: Detection, right: Detection) -> bool:
        overlap = self._overlap_metrics(left, right)
        if not (
            overlap["iou"] >= self.same_subject_iou_threshold
            or overlap["containment"] >= self.same_subject_containment_threshold
        ):
            return False

        left_area = max(left.area_fraction or 0.0, 0.0)
        right_area = max(right.area_fraction or 0.0, 0.0)
        if min(left_area, right_area) <= 0:
            return False
        area_ratio = max(left_area, right_area) / min(left_area, right_area)
        if area_ratio > self.same_subject_area_ratio:
            return False

        cx_left = (left.x1 + left.x2) / 2.0
        cy_left = (left.y1 + left.y2) / 2.0
        cx_right = (right.x1 + right.x2) / 2.0
        cy_right = (right.y1 + right.y2) / 2.0
        center_distance = ((cx_left - cx_right) ** 2 + (cy_left - cy_right) ** 2) ** 0.5

        left_diag = ((left.x2 - left.x1) ** 2 + (left.y2 - left.y1) ** 2) ** 0.5
        right_diag = ((right.x2 - right.x1) ** 2 + (right.y2 - right.y1) ** 2) ** 0.5
        small_diag = min(left_diag, right_diag)
        if small_diag <= 0:
            return False
        return (center_distance / small_diag) <= self.same_subject_center_distance_ratio

    def _suppress_tiny_secondary_fragments(self, detections: list[Detection]) -> list[Detection]:
        """
        Remove tiny detections when a much larger bird box exists in the frame.

        This is done as a global pre-pass rather than a confidence-ordered
        greedy pass, because tiny fragments can sometimes score higher than the
        main subject box.
        """
        kept: list[Detection] = []
        for candidate in detections:
            candidate_area = max(candidate.area_fraction or 0.0, 0.0)
            suppress = False
            if candidate_area <= self.tiny_secondary_area_threshold:
                for other in detections:
                    if other is candidate:
                        continue
                    other_area = max(other.area_fraction or 0.0, 0.0)
                    if (
                        other_area >= self.tiny_secondary_primary_min_area
                        and other_area >= (candidate_area * self.tiny_secondary_area_ratio)
                    ):
                        suppress = True
                        break
            if not suppress:
                kept.append(candidate)
        return kept

    @staticmethod
    def _overlap_metrics(left: Detection, right: Detection) -> dict[str, float]:
        x1 = max(left.x1, right.x1)
        y1 = max(left.y1, right.y1)
        x2 = min(left.x2, right.x2)
        y2 = min(left.y2, right.y2)
        inter_w = max(0, x2 - x1)
        inter_h = max(0, y2 - y1)
        intersection = inter_w * inter_h
        left_area = max(0, left.x2 - left.x1) * max(0, left.y2 - left.y1)
        right_area = max(0, right.x2 - right.x1) * max(0, right.y2 - right.y1)
        union = left_area + right_area - intersection
        min_area = min(left_area, right_area)
        return {
            "iou": (intersection / union) if union > 0 else 0.0,
            "containment": (intersection / min_area) if min_area > 0 else 0.0,
        }
