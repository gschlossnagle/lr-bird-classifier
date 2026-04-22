"""
Catalog-to-review-store extraction orchestration.

This module deliberately keeps the detector and preview source abstract so the
review workflow can be tested locally without committing to a specific CV stack
or preview-generation strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .catalog import CatalogImage, LightroomCatalog
from .review_store import ReviewStore


@dataclass(frozen=True)
class Detection:
    """One detector-produced object candidate."""

    detected_class: str
    confidence: float
    x1: int
    y1: int
    x2: int
    y2: int
    area_fraction: float | None = None


class BirdDetector(Protocol):
    """Detector interface for candidate extraction."""

    name: str

    def detect(self, image_path: Path) -> list[Detection]:
        """Return zero or more bird detections for the image."""


class PreviewProvider(Protocol):
    """Preview-generation interface for review images."""

    def build_preview(self, image_path: Path, detection: Detection, candidate_id: str) -> Path:
        """Return a path to a preview asset for the candidate."""


class CatalogExtractor:
    """Populate a ReviewStore by running a detector over catalog images."""

    def __init__(self, store: ReviewStore, detector: BirdDetector, preview_provider: PreviewProvider) -> None:
        self.store = store
        self.detector = detector
        self.preview_provider = preview_provider

    def extract(
        self,
        catalog_path: str | Path,
        *,
        formats: set[str] | None = None,
        folder_filter: str | None = None,
        min_rating: int | None = None,
        limit: int | None = None,
        created_at: str,
    ) -> tuple[int, int]:
        """
        Scan catalog images, run the detector, and populate the review store.

        Returns:
            `(num_images_inserted, num_candidates_inserted)`
        """
        images_inserted = 0
        candidates_inserted = 0
        with LightroomCatalog.open(catalog_path, readonly=True, backup=False) as cat:
            images = cat.get_images(
                formats=formats,
                folder_filter=folder_filter,
                min_rating=min_rating,
                limit=limit,
            )
            burst_groups = self._assign_burst_groups(images)

            for image in images:
                image_path = Path(image.file_path)
                image_id = self._insert_image(image, created_at, burst_groups.get(image.id_local))
                images_inserted += 1
                detections = self.detector.detect(image_path)
                for idx, detection in enumerate(detections, start=1):
                    candidate_id = f"img{image.id_local}_det{idx:03d}"
                    preview_path = self.preview_provider.build_preview(
                        image_path,
                        detection,
                        candidate_id,
                    )
                    self.store.insert_candidate(
                        {
                            "candidate_id": candidate_id,
                            "image_id": image_id,
                            "detector_name": self.detector.name,
                            "detected_class": detection.detected_class,
                            "detector_confidence": detection.confidence,
                            "bbox_x1": detection.x1,
                            "bbox_y1": detection.y1,
                            "bbox_x2": detection.x2,
                            "bbox_y2": detection.y2,
                            "bbox_area_fraction": detection.area_fraction,
                            "preview_image_path": str(preview_path.resolve()),
                            "review_status": "unreviewed",
                            "safe_single_subject_burst": len(detections) == 1,
                            "created_at": created_at,
                        }
                    )
                    candidates_inserted += 1
        return images_inserted, candidates_inserted

    def _insert_image(self, image: CatalogImage, created_at: str, burst_group_id: str | None) -> int:
        return self.store.insert_image(
            {
                "source_image_id": image.id_local,
                "source_image_path": str(Path(image.file_path).resolve()),
                "capture_datetime": image.capture_time,
                "folder": str(Path(image.file_path).resolve().parent),
                "rating": image.rating,
                "burst_group_id": burst_group_id,
                "created_at": created_at,
            }
        )

    @staticmethod
    def _assign_burst_groups(images: list[CatalogImage]) -> dict[int, str]:
        """
        Assign conservative burst groups based on folder and capture-time proximity.

        This is intentionally strict. The goal is to enable explicit burst-apply
        on obvious tracking sequences, not to infer semantic grouping broadly.
        """
        groups: dict[int, str] = {}
        current_group: str | None = None
        current_index = 0
        prev_folder: str | None = None
        prev_dt: datetime | None = None
        prev_id: int | None = None

        for image in images:
            path = Path(image.file_path).resolve()
            folder = str(path.parent)
            dt = CatalogExtractor._parse_capture_time(image.capture_time)
            same_group = (
                prev_folder is not None
                and prev_dt is not None
                and dt is not None
                and folder == prev_folder
                and (dt - prev_dt).total_seconds() <= 1.0
            )
            if not same_group:
                current_group = None
            if same_group and current_group is None:
                current_index += 1
                current_group = f"burst_{current_index:06d}"
                if prev_id is not None:
                    groups[prev_id] = current_group
            if current_group is not None:
                groups[image.id_local] = current_group
            prev_folder = folder
            prev_dt = dt
            prev_id = image.id_local
        return groups

    @staticmethod
    def _parse_capture_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            cleaned = value.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned)
        except ValueError:
            return None
