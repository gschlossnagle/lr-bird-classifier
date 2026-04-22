"""
Validation helpers for the annotation review workflow.

These helpers enforce the core invariants from the working plans without
tying callers to a particular UI or storage layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class ValidationError(ValueError):
    """Raised when review workflow data violates a contract."""


_CANDIDATE_REVIEW_STATUSES = {"unreviewed", "in_review", "reviewed", "skipped"}
_ANNOTATION_STATUSES = {
    "labeled",
    "reject",
    "unsure",
    "not_a_bird",
    "bad_crop",
    "duplicate",
}
_SOURCE_TYPES = {
    "control_inat",
    "catalog_real_world",
    "catalog_stress",
    "catalog_reject",
}


def _require_fields(row: dict[str, Any], fields: list[str], kind: str) -> None:
    missing = [field for field in fields if field not in row]
    if missing:
        raise ValidationError(f"{kind}: missing required field(s): {', '.join(missing)}")


def _require_bool_fields(row: dict[str, Any], fields: list[str], kind: str) -> None:
    for field in fields:
        value = row.get(field)
        if not isinstance(value, bool):
            raise ValidationError(f"{kind}: '{field}' must be a boolean")


def validate_candidate_row(
    row: dict[str, Any],
    *,
    require_existing_preview: bool = False,
) -> None:
    """Validate a detector-produced candidate row."""
    _require_fields(
        row,
        [
            "candidate_id",
            "image_id",
            "detector_name",
            "detected_class",
            "detector_confidence",
            "bbox_x1",
            "bbox_y1",
            "bbox_x2",
            "bbox_y2",
            "preview_image_path",
            "review_status",
            "created_at",
        ],
        "candidate",
    )

    if row["review_status"] not in _CANDIDATE_REVIEW_STATUSES:
        raise ValidationError(f"candidate: invalid review_status '{row['review_status']}'")

    if row["bbox_x2"] <= row["bbox_x1"]:
        raise ValidationError("candidate: bbox_x2 must be greater than bbox_x1")
    if row["bbox_y2"] <= row["bbox_y1"]:
        raise ValidationError("candidate: bbox_y2 must be greater than bbox_y1")

    preview_path = Path(row["preview_image_path"])
    if not preview_path.is_absolute():
        raise ValidationError("candidate: preview_image_path must be absolute")
    if require_existing_preview and not preview_path.exists():
        raise ValidationError("candidate: preview_image_path does not exist")

    safe_burst = row.get("safe_single_subject_burst", False)
    if not isinstance(safe_burst, bool):
        raise ValidationError("candidate: safe_single_subject_burst must be a boolean when provided")


def validate_annotation_row(row: dict[str, Any]) -> None:
    """Validate a normalized annotation row."""
    _require_fields(
        row,
        [
            "candidate_id",
            "annotation_status",
            "stress",
            "reject_sample",
            "unsure",
            "not_a_bird",
            "bad_crop",
            "duplicate_sample",
            "annotated_at",
        ],
        "annotation",
    )
    _require_bool_fields(
        row,
        ["stress", "reject_sample", "unsure", "not_a_bird", "bad_crop", "duplicate_sample"],
        "annotation",
    )

    status = row["annotation_status"]
    if status not in _ANNOTATION_STATUSES:
        raise ValidationError(f"annotation: invalid annotation_status '{status}'")

    primary_flags = {
        "reject": row["reject_sample"],
        "unsure": row["unsure"],
        "not_a_bird": row["not_a_bird"],
        "bad_crop": row["bad_crop"],
        "duplicate": row["duplicate_sample"],
    }

    if status == "labeled":
        _require_fields(
            row,
            ["truth_common_name", "truth_sci_name", "truth_label", "taxon_class"],
            "annotation",
        )
        if any(primary_flags.values()):
            raise ValidationError("annotation: labeled rows cannot set reject-style flags")
        return

    if row["stress"]:
        raise ValidationError("annotation: stress is only valid for labeled rows")

    expected_flag = {
        "reject": "reject",
        "unsure": "unsure",
        "not_a_bird": "not_a_bird",
        "bad_crop": "bad_crop",
        "duplicate": "duplicate",
    }[status]

    for flag_name, enabled in primary_flags.items():
        if flag_name == expected_flag and not enabled:
            raise ValidationError(f"annotation: status '{status}' must set its matching flag")
        if flag_name != expected_flag and enabled:
            raise ValidationError(
                f"annotation: status '{status}' cannot also set '{flag_name}'"
            )


def validate_manifest_row(row: dict[str, Any]) -> None:
    """Validate an exported benchmark manifest row."""
    _require_fields(
        row,
        [
            "sample_id",
            "source_type",
            "image_path",
            "truth_label",
            "truth_sci_name",
            "truth_common_name",
            "taxon_class",
            "multi_label",
            "secondary_truth_labels",
        ],
        "manifest",
    )
    if row["source_type"] not in _SOURCE_TYPES:
        raise ValidationError(f"manifest: invalid source_type '{row['source_type']}'")
    if not isinstance(row["multi_label"], bool):
        raise ValidationError("manifest: multi_label must be a boolean")
    if not isinstance(row["secondary_truth_labels"], list):
        raise ValidationError("manifest: secondary_truth_labels must be a list")
    image_path = Path(row["image_path"])
    if not image_path.is_absolute():
        raise ValidationError("manifest: image_path must be absolute")
    if not row["multi_label"] and row["secondary_truth_labels"]:
        raise ValidationError(
            "manifest: secondary_truth_labels must be empty when multi_label is false"
        )
