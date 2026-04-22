"""
Benchmark manifest export for the annotation review workflow.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .review_store import ReviewStore
from .review_validation import validate_manifest_row


def _bbox_from_candidate(candidate: dict[str, Any]) -> dict[str, int]:
    return {
        "x1": candidate["bbox_x1"],
        "y1": candidate["bbox_y1"],
        "x2": candidate["bbox_x2"],
        "y2": candidate["bbox_y2"],
    }


def _materialized_name(source_type: str, index: int) -> str:
    return f"{source_type.replace('_', '-')}-{index:06d}.jpg"


class ManifestExporter:
    """Export reviewed candidates into benchmark manifests."""

    def __init__(self, store: ReviewStore) -> None:
        self.store = store

    def export_rows(self, source_type: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, record in enumerate(self._eligible_records(source_type), start=1):
            annotation = record["annotation"]
            candidate = record["candidate"]
            image = record["image"]

            row = {
                "sample_id": f"{source_type.replace('_', '-')}-{index:06d}",
                "source_type": source_type,
                "image_path": image["source_image_path"],
                "truth_label": annotation["truth_label"],
                "truth_sci_name": annotation["truth_sci_name"],
                "truth_common_name": annotation["truth_common_name"],
                "taxon_class": annotation["taxon_class"],
                "multi_label": False,
                "secondary_truth_labels": [],
                "source_image_id": image.get("source_image_id"),
                "candidate_id": candidate["id"],
                "region": image.get("region_hint"),
                "capture_datetime": image.get("capture_datetime"),
                "stress": annotation["stress"],
                "reject": annotation["reject_sample"],
                "detector_name": candidate["detector_name"],
                "detector_confidence": candidate["detector_confidence"],
                "bbox": _bbox_from_candidate(candidate),
                "notes": annotation.get("notes") or "",
            }
            validate_manifest_row(row)
            rows.append(row)
        return rows

    def export_jsonl(self, source_type: str, output_path: str | Path) -> int:
        rows = self.export_rows(source_type)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        return len(rows)

    def export_materialized_dataset(
        self,
        source_type: str,
        output_dir: str | Path,
        *,
        export_max_size_bytes: int | None = None,
        export_quality_floor: int | None = None,
    ) -> int:
        rows = self.export_rows(source_type)
        outdir = Path(output_dir)
        images_dir = outdir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        materialized_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            candidate = self.store.get_candidate(row["candidate_id"])
            assert candidate is not None
            preview_path = Path(candidate["preview_image_path"])
            exported_path = images_dir / _materialized_name(source_type, index)
            shutil.copy2(preview_path, exported_path)
            row = dict(row)
            row["exported_image_path"] = str(exported_path.resolve())
            row["export_format"] = "jpeg"
            row["exported_image_size_bytes"] = exported_path.stat().st_size
            if export_max_size_bytes is not None:
                row["export_max_size_bytes"] = export_max_size_bytes
            if export_quality_floor is not None:
                row["export_quality_floor"] = export_quality_floor
            materialized_rows.append(row)

        manifest_path = outdir / f"{source_type}.jsonl"
        with manifest_path.open("w", encoding="utf-8") as f:
            for row in materialized_rows:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        return len(materialized_rows)

    def _eligible_records(self, source_type: str) -> list[dict[str, Any]]:
        if source_type not in {"catalog_real_world", "catalog_stress", "catalog_reject"}:
            raise ValueError(f"Unsupported export source_type '{source_type}'")

        records: list[dict[str, Any]] = []
        for candidate in self.store.list_candidates(review_status="reviewed"):
            annotation = self.store.get_annotation(candidate["id"])
            if annotation is None:
                continue
            image = self.store.get_image(candidate["image_id"])
            if image is None:
                continue
            if self._include_annotation(source_type, annotation):
                records.append(
                    {
                        "candidate": candidate,
                        "annotation": annotation,
                        "image": image,
                    }
                )
        return records

    @staticmethod
    def _include_annotation(source_type: str, annotation: dict[str, Any]) -> bool:
        status = annotation["annotation_status"]
        if source_type == "catalog_real_world":
            return (
                status == "labeled"
                and not annotation["stress"]
                and not annotation["reject_sample"]
                and not annotation["unsure"]
                and not annotation["not_a_bird"]
                and not annotation["bad_crop"]
                and not annotation["duplicate_sample"]
            )
        if source_type == "catalog_stress":
            return status == "labeled" and annotation["stress"]
        return status in {"reject", "not_a_bird"} or annotation["reject_sample"] or annotation["not_a_bird"]
