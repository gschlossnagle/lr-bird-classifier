"""
Shared Lightroom/XMP label-application helpers.

This module centralizes the managed keyword hierarchy writes used by the
different tagging workflows so the codebase does not accumulate multiple
slightly-different writer paths.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .taxonomy import get_order_display_name, parse_label
from .xmp_writer import clean_xmp_keywords, write_bird_keywords


@dataclass(frozen=True)
class SpeciesLabel:
    """Normalized species payload for Lightroom/XMP writeback."""

    common_name: str
    sci_name: str
    order: str
    family: str
    order_display: str


def species_label_from_prediction(prediction) -> SpeciesLabel:
    """Build a normalized label payload from a classifier prediction."""
    parsed = parse_label(prediction.label)
    order = parsed.get("order", "")
    family = parsed.get("family", "")
    return SpeciesLabel(
        common_name=prediction.common_name,
        sci_name=prediction.sci_name,
        order=order,
        family=family,
        order_display=get_order_display_name(order) if order else "",
    )


def species_label_from_taxonomy(
    *,
    common_name: str,
    sci_name: str,
    label: str,
) -> SpeciesLabel:
    """Build a normalized label payload from a resolved taxonomy label."""
    parsed = parse_label(label)
    order = parsed.get("order", "")
    family = parsed.get("family", "")
    return SpeciesLabel(
        common_name=common_name,
        sci_name=sci_name,
        order=order,
        family=family,
        order_display=get_order_display_name(order) if order else "",
    )


def flat_keywords_for_label(label: SpeciesLabel) -> list[str]:
    """Return the flat keyword values written for this species label."""
    return list(
        dict.fromkeys(
            [
                label.common_name,
                label.order_display,
                label.order,
                label.family,
                label.sci_name,
            ]
        )
    )


def existing_flat_tags_from_log(clf_log, image_id: int) -> list[str]:
    """Build the managed flat-keyword set for an image from classification-log rows."""
    try:
        rows = clf_log.get_all_rows()
    except Exception:
        return []

    flat: list[str] = []
    try:
        iterator = iter(rows)
    except TypeError:
        return []

    for row in iterator:
        if row["image_id"] != image_id:
            continue
        parsed = parse_label(row.get("label") or "")
        order_raw = parsed.get("order", "")
        order_disp = get_order_display_name(order_raw) if order_raw else ""
        flat.extend(
            [
                row.get("common_name") or "",
                order_disp,
                order_raw,
                parsed.get("family", ""),
                row.get("sci_name") or "",
            ]
        )
    return list(dict.fromkeys(v for v in flat if v))


def managed_keyword_names_for_labels(
    labels: Iterable[SpeciesLabel],
    *,
    confidence_band_name: str | None = None,
    manual: bool = False,
) -> set[str]:
    """Return the managed Lightroom keyword names expected for these labels."""
    names: set[str] = set()
    for label in labels:
        names.add(label.common_name)
        if label.order_display:
            names.add(label.order_display)
        if label.order:
            names.add(label.order)
        if label.family:
            names.add(label.family)
        if label.sci_name:
            names.add(label.sci_name)
    if confidence_band_name:
        names.add(confidence_band_name)
    if manual:
        names.add("manually classed")
    return names


def managed_keyword_fingerprint(names: Iterable[str]) -> str:
    """Compute a stable fingerprint for a managed keyword-name set."""
    payload = json.dumps(sorted(set(name for name in names if name)), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_catalog_species_label(
    cat,
    image_id: int,
    label: SpeciesLabel,
    *,
    confidence_band_name: str | None = None,
    manual: bool = False,
) -> bool:
    """
    Apply one normalized species label to a Lightroom catalog image.

    Returns True if the top-level Bird-Species keyword was newly added.
    """
    top_id = cat.ensure_bird_species_keyword(label.common_name)
    newly_tagged = cat.tag_image(image_id, top_id)

    order_id, keyword_id = cat.ensure_species_keyword(
        label.order_display or label.order,
        label.common_name,
    )
    cat.tag_image(image_id, keyword_id)
    cat.tag_image(image_id, order_id)

    if label.order and label.family and label.sci_name:
        sci_ids = cat.ensure_scientific_keywords(
            label.order,
            label.family,
            label.sci_name,
        )
        for keyword_id in sci_ids:
            cat.tag_image(image_id, keyword_id)

    if confidence_band_name:
        band_id = cat.ensure_confidence_keyword(confidence_band_name)
        cat.tag_image(image_id, band_id)

    if manual:
        manual_id = cat.ensure_manually_classed_keyword()
        cat.tag_image(image_id, manual_id)

    return newly_tagged


def replace_catalog_species_labels(
    cat,
    image_id: int,
    labels: Iterable[SpeciesLabel],
    *,
    confidence_band_name: str | None = None,
    manual: bool = False,
) -> None:
    """Replace managed catalog labels on an image with the provided species set."""
    cat.remove_auto_classifications(image_id)
    for idx, label in enumerate(labels):
        apply_catalog_species_label(
            cat,
            image_id,
            label,
            confidence_band_name=confidence_band_name if idx == 0 else None,
            manual=manual if idx == 0 else False,
        )


def write_sidecar_species_labels(
    image_path: Path,
    labels: Iterable[SpeciesLabel],
    *,
    replace_existing: bool = False,
    flat_to_remove: Iterable[str] | None = None,
) -> bool:
    """
    Synchronize one or more species labels to an existing XMP sidecar.

    When ``replace_existing`` is true, managed classifier keywords are removed
    from the sidecar before the new labels are written.
    """
    if replace_existing:
        clean_xmp_keywords(
            image_path,
            list(flat_to_remove or []),
        )

    ok = True
    for label in labels:
        wrote = write_bird_keywords(
            image_path,
            label.common_name,
            label.order_display or label.order,
            label.order,
            label.family,
            label.sci_name,
        )
        ok = ok and wrote
    return ok
