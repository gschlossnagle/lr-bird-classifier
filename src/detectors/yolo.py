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
        model: str = "yolov8n.pt",
        *,
        confidence_threshold: float = 0.25,
        bird_class_ids: tuple[int, ...] = (14,),
        device: str | None = None,
        imgsz: int = 1280,
    ) -> None:
        self.model_path = model
        self.confidence_threshold = confidence_threshold
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
        results = self._model.predict(
            source=image,
            conf=self.confidence_threshold,
            verbose=False,
            device=self.device,
            imgsz=self.imgsz,
        )

        detections: list[Detection] = []
        for result in results:
            orig_h, orig_w = result.orig_shape
            image_area = max(orig_h * orig_w, 1)
            boxes = getattr(result, "boxes", None)
            if boxes is None:
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
        return detections
