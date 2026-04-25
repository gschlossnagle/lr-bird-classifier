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

import exifread
from PIL import Image
try:
    import pyexiv2
except ImportError:  # pragma: no cover - fallback path exercised in tests via patching
    pyexiv2 = None

from .catalog import CatalogImage, LightroomCatalog
from .raw_utils import load_image
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


@dataclass(frozen=True)
class SequenceMetadata:
    make: str | None = None
    sequence_image_number: int | None = None
    sequence_file_number: int | None = None
    sequence_length: str | None = None


class BirdDetector(Protocol):
    """Detector interface for candidate extraction."""

    name: str

    def detect(self, image_path: Path) -> list[Detection]:
        """Return zero or more bird detections for the image."""

    def detect_image(self, image, image_path: Path) -> list[Detection]:
        """Return zero or more bird detections for a preloaded image."""


class PreviewProvider(Protocol):
    """Preview-generation interface for review images."""

    def build_preview(self, image_path: Path, detection: Detection, candidate_id: str) -> Path:
        """Return a path to a preview asset for the candidate."""

    def build_preview_from_image(
        self,
        image,
        image_path: Path,
        detection: Detection,
        candidate_id: str,
        *,
        source_size: tuple[int, int] | None = None,
    ) -> Path:
        """Return a path to a preview asset using a preloaded image."""


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
        start_after_id: int | None = None,
        limit: int | None = None,
        created_at: str,
    ) -> tuple[int, int, int | None]:
        """
        Scan catalog images, run the detector, and populate the review store.

        Returns:
            `(num_images_scanned, num_candidates_inserted, last_image_id_processed)`
        """
        images_inserted = 0
        candidates_inserted = 0
        last_image_id_processed: int | None = None
        with LightroomCatalog.open(catalog_path, readonly=True, backup=False) as cat:
            images = cat.get_images(
                formats=formats,
                folder_filter=folder_filter,
                min_rating=min_rating,
                start_after_id=start_after_id,
                limit=limit,
            )
            sequence_metadata = self._load_sequence_metadata(images)
            burst_groups = self._assign_burst_groups(images, sequence_metadata)
            for image in images:
                image_path = Path(image.file_path)
                image_id = self._insert_image(image, created_at, burst_groups.get(image.id_local))
                images_inserted += 1
                last_image_id_processed = image.id_local
                loaded_image = None
                working_image = None
                source_size: tuple[int, int] | None = None
                if hasattr(self.detector, "detect_image") or hasattr(self.preview_provider, "build_preview_from_image"):
                    loaded_image = load_image(image_path)
                    source_size = loaded_image.size
                    working_image = _make_working_image(
                        loaded_image,
                        max_dimension=_working_max_dimension(self.detector, self.preview_provider),
                    )
                if working_image is not None and hasattr(self.detector, "detect_image"):
                    detections = self.detector.detect_image(working_image, image_path)
                    if source_size is not None and working_image.size != source_size:
                        detections = _rescale_detections(
                            detections,
                            from_size=working_image.size,
                            to_size=source_size,
                        )
                else:
                    detections = self.detector.detect(image_path)
                burst_safe_flags = self._burst_safe_detection_flags(detections)
                for idx, detection in enumerate(detections, start=1):
                    candidate_id = f"img{image.id_local}_det{idx:03d}"
                    if working_image is not None and hasattr(self.preview_provider, "build_preview_from_image"):
                        preview_path = self.preview_provider.build_preview_from_image(
                            working_image,
                            image_path,
                            detection,
                            candidate_id,
                            source_size=source_size,
                        )
                    else:
                        preview_path = self.preview_provider.build_preview(
                            image_path,
                            detection,
                            candidate_id,
                        )
                    inserted = self.store.insert_candidate(
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
                            "safe_single_subject_burst": burst_safe_flags[idx - 1],
                            "created_at": created_at,
                        }
                    )
                    if inserted:
                        candidates_inserted += 1
        return images_inserted, candidates_inserted, last_image_id_processed

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
    def _burst_safe_detection_flags(detections: list[Detection]) -> list[bool]:
        """
        Mark detections that are safe targets for burst-level label propagation.

        A single detection is always safe. For multi-detection frames, only mark
        the dominant detection as safe when it is clearly larger than the next
        largest candidate. This preserves the common "one main bird plus tiny
        incidental background bird" case without spreading labels across
        genuinely ambiguous multi-subject frames.
        """
        if not detections:
            return []
        if len(detections) == 1:
            return [True]

        ranked = sorted(
            enumerate(detections),
            key=lambda item: item[1].area_fraction,
            reverse=True,
        )
        largest_idx, largest = ranked[0]
        second_largest = ranked[1][1]
        largest_area = max(largest.area_fraction, 0.0)
        second_area = max(second_largest.area_fraction, 0.0)

        # Require a reasonably large primary subject and a clear size gap.
        if largest_area >= 0.05 and largest_area >= (second_area * 3.0):
            flags = [False] * len(detections)
            flags[largest_idx] = True
            return flags
        return [False] * len(detections)

    @staticmethod
    def _assign_burst_groups(
        images: list[CatalogImage],
        sequence_metadata: dict[int, SequenceMetadata] | None = None,
    ) -> dict[int, str]:
        """
        Assign burst groups using EXIF sequence metadata when available, falling
        back to conservative folder/time proximity otherwise.

        The EXIF-based path is preferred because it reflects native camera burst
        state. The time-based fallback remains intentionally strict.
        """
        sequence_metadata = sequence_metadata or {}
        groups: dict[int, str] = {}
        current_group: str | None = None
        current_index = 0
        prev_folder: str | None = None
        prev_dt: datetime | None = None
        prev_id: int | None = None
        prev_seq: SequenceMetadata | None = None

        for image in images:
            path = Path(image.file_path).resolve()
            folder = str(path.parent)
            dt = CatalogExtractor._parse_capture_time(image.capture_time)
            seq = sequence_metadata.get(image.id_local)
            same_group = CatalogExtractor._same_burst(
                folder=folder,
                dt=dt,
                seq=seq,
                prev_folder=prev_folder,
                prev_dt=prev_dt,
                prev_seq=prev_seq,
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
            prev_seq = seq
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

    @staticmethod
    def _same_burst(
        *,
        folder: str,
        dt: datetime | None,
        seq: SequenceMetadata | None,
        prev_folder: str | None,
        prev_dt: datetime | None,
        prev_seq: SequenceMetadata | None,
    ) -> bool:
        if prev_folder is None or folder != prev_folder:
            return False

        if CatalogExtractor._sequence_continues(prev_seq, seq):
            return True

        return (
            prev_dt is not None
            and dt is not None
            and (dt - prev_dt).total_seconds() <= 1.0
        )

    @staticmethod
    def _sequence_continues(prev_seq: SequenceMetadata | None, seq: SequenceMetadata | None) -> bool:
        if prev_seq is None or seq is None:
            return False
        if not CatalogExtractor._is_supported_sequence_make(prev_seq.make, seq.make):
            return False
        if not prev_seq.sequence_length or not seq.sequence_length:
            return False
        if prev_seq.sequence_length != seq.sequence_length:
            return False
        if CatalogExtractor._sony_sequence_continues(prev_seq, seq):
            return True
        return False

    @staticmethod
    def _is_supported_sequence_make(prev_make: str | None, make: str | None) -> bool:
        if not prev_make or not make:
            return False
        return prev_make.strip().upper() == "SONY" and make.strip().upper() == "SONY"

    @staticmethod
    def _sony_sequence_continues(prev_seq: SequenceMetadata, seq: SequenceMetadata) -> bool:
        if prev_seq.sequence_file_number is not None and seq.sequence_file_number is not None:
            return seq.sequence_file_number == prev_seq.sequence_file_number + 1
        if prev_seq.sequence_image_number is not None and seq.sequence_image_number is not None:
            return seq.sequence_image_number == prev_seq.sequence_image_number + 1
        return False

    @staticmethod
    def _load_sequence_metadata(images: list[CatalogImage]) -> dict[int, SequenceMetadata]:
        if not images:
            return {}

        metadata = CatalogExtractor._load_sequence_metadata_pyexiv2(images)
        if metadata:
            return metadata

        return CatalogExtractor._load_sequence_metadata_exifread(images)

    @staticmethod
    def _load_sequence_metadata_pyexiv2(images: list[CatalogImage]) -> dict[int, SequenceMetadata]:
        if pyexiv2 is None:
            return {}

        metadata: dict[int, SequenceMetadata] = {}
        for image in images:
            path = Path(image.file_path).resolve()
            try:
                exif = pyexiv2.Image(str(path))
                tags = exif.read_exif()
            except Exception:
                continue

            make = _as_text(tags.get("Exif.Image.Make"))
            sequence_number = _as_int(tags.get("Exif.Sony2.SequenceNumber"))
            release_mode = _sony_release_mode_text(tags.get("Exif.Sony2.ReleaseMode"))

            if make is None and sequence_number is None and release_mode is None:
                continue

            metadata[image.id_local] = SequenceMetadata(
                make=make,
                sequence_image_number=sequence_number,
                sequence_file_number=sequence_number,
                sequence_length=release_mode,
            )
        return metadata

    @staticmethod
    def _load_sequence_metadata_exifread(images: list[CatalogImage]) -> dict[int, SequenceMetadata]:
        metadata: dict[int, SequenceMetadata] = {}
        for image in images:
            path = Path(image.file_path).resolve()
            try:
                with path.open("rb") as f:
                    tags = exifread.process_file(f, details=True)
            except OSError:
                continue

            make = _as_text(tags.get("Image Make"))
            sequence_number = _as_int(tags.get("MakerNote SequenceNumber"))
            release_mode = _as_text(tags.get("MakerNote ReleaseMode"))

            if make is None and sequence_number is None and release_mode is None:
                continue

            metadata[image.id_local] = SequenceMetadata(
                make=make,
                sequence_image_number=sequence_number,
                sequence_file_number=sequence_number,
                sequence_length=release_mode,
            )
        return metadata


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            text = str(value).strip()
            return int(text)
        except (TypeError, ValueError):
            return None


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sony_release_mode_text(value: object) -> str | None:
    numeric = _as_int(value)
    if numeric == 2:
        return "Continuous"
    return _as_text(value)


def _working_max_dimension(detector: BirdDetector, preview_provider: PreviewProvider) -> int:
    detector_imgsz = int(getattr(detector, "imgsz", 1024))
    preview_max_dimension = int(getattr(preview_provider, "max_dimension", detector_imgsz))
    return min(detector_imgsz, preview_max_dimension)


def _make_working_image(image: Image.Image, max_dimension: int) -> Image.Image:
    if max(image.size) <= max_dimension:
        return image
    working = image.copy()
    working.thumbnail((max_dimension, max_dimension), Image.Resampling.BILINEAR)
    return working


def _rescale_detections(
    detections: list[Detection],
    *,
    from_size: tuple[int, int],
    to_size: tuple[int, int],
) -> list[Detection]:
    from_w, from_h = from_size
    to_w, to_h = to_size
    scale_x = to_w / max(from_w, 1)
    scale_y = to_h / max(from_h, 1)
    image_area = max(to_w * to_h, 1)
    scaled: list[Detection] = []
    for detection in detections:
        x1 = int(round(detection.x1 * scale_x))
        y1 = int(round(detection.y1 * scale_y))
        x2 = int(round(detection.x2 * scale_x))
        y2 = int(round(detection.y2 * scale_y))
        width = max(x2 - x1, 0)
        height = max(y2 - y1, 0)
        scaled.append(
            Detection(
                detected_class=detection.detected_class,
                confidence=detection.confidence,
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                area_fraction=(width * height) / image_area,
            )
        )
    return scaled
