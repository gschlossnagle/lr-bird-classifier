"""
Shared review-to-Lightroom apply engine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import LightroomCatalog, confidence_band
from .classification_log import ClassificationLog
from .label_apply import (
    SpeciesLabel,
    managed_keyword_fingerprint,
    managed_keyword_names_for_labels,
    replace_catalog_species_labels,
    species_label_from_taxonomy,
    write_sidecar_species_labels,
)
from .review_apply_state import ReviewApplyState
from .review_store import ReviewStore

log = logging.getLogger(__name__)

POLICY_VERSION = "review-apply-v1-single-species"


@dataclass(frozen=True)
class ApplyPolicy:
    allow_multi_species: bool = False
    include_stress: bool = True
    mark_manually_classed: bool = True
    force_reapply: bool = False
    dry_run: bool = False
    scope_key: str | None = None
    report_path: str | None = None


@dataclass
class DesiredImageOutcome:
    image_key: str
    catalog_path: str
    scope_key: str | None
    source_image_id: int | None
    source_image_path: str
    workflow_type: str
    labels: list[SpeciesLabel]
    annotation_rows: list[dict[str, Any]]
    status: str
    message: str = ""

    def as_payload(self, *, manual: bool) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "labels": [
                {
                    "common_name": label.common_name,
                    "sci_name": label.sci_name,
                    "order": label.order,
                    "family": label.family,
                    "order_display": label.order_display,
                }
                for label in self.labels
            ],
            "manual": manual,
        }


@dataclass(frozen=True)
class ApplyResult:
    summary_key: str
    event_type: str
    message: str = ""


def review_fingerprint_for_outcome(outcome: DesiredImageOutcome, *, manual: bool) -> str:
    payload = json.dumps(outcome.as_payload(manual=manual), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_image_key(*, catalog_path: str, source_image_id: int | None, source_image_path: str) -> str:
    if source_image_id is not None:
        return f"{catalog_path}::id:{source_image_id}"
    return f"{catalog_path}::path:{source_image_path}"


def consolidate_image_outcome(rows: list[dict[str, Any]], policy: ApplyPolicy) -> DesiredImageOutcome:
    first = rows[0]
    image_key = make_image_key(
        catalog_path=first["catalog_path"],
        source_image_id=first.get("source_image_id"),
        source_image_path=first["source_image_path"],
    )
    labeled_rows = [row for row in rows if row["annotation_status"] == "labeled"]
    if not policy.include_stress:
        labeled_rows = [row for row in labeled_rows if not row.get("stress")]
    labels_by_truth: dict[str, SpeciesLabel] = {}
    for row in labeled_rows:
        truth_label = row.get("truth_label")
        if not truth_label:
            continue
        labels_by_truth[truth_label] = species_label_from_taxonomy(
            common_name=row["truth_common_name"],
            sci_name=row["truth_sci_name"],
            label=truth_label,
        )

    labels = list(labels_by_truth.values())
    if not labels:
        return DesiredImageOutcome(
            image_key=image_key,
            catalog_path=first["catalog_path"],
            scope_key=first.get("scope_key"),
            source_image_id=first.get("source_image_id"),
            source_image_path=first["source_image_path"],
            workflow_type=first.get("workflow_type") or "detector_review",
            labels=[],
            annotation_rows=rows,
            status="no_label",
            message="No labeled species outcomes for this image.",
        )
    if len(labels) > 1 and not policy.allow_multi_species:
        return DesiredImageOutcome(
            image_key=image_key,
            catalog_path=first["catalog_path"],
            scope_key=first.get("scope_key"),
            source_image_id=first.get("source_image_id"),
            source_image_path=first["source_image_path"],
            workflow_type=first.get("workflow_type") or "detector_review",
            labels=labels,
            annotation_rows=rows,
            status="conflict",
            message="Multiple labeled species present; multi-species apply is disabled.",
        )
    return DesiredImageOutcome(
        image_key=image_key,
        catalog_path=first["catalog_path"],
        scope_key=first.get("scope_key"),
        source_image_id=first.get("source_image_id"),
        source_image_path=first["source_image_path"],
        workflow_type=first.get("workflow_type") or "detector_review",
        labels=labels,
        annotation_rows=rows,
        status="apply",
    )


def _existing_flat_tags(clf_log: ClassificationLog, image_id: int) -> list[str]:
    from .taxonomy import get_order_display_name, parse_label

    flat: list[str] = []
    for row in clf_log.get_all_rows():
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


class ApplyEngine:
    def __init__(
        self,
        *,
        review_store: ReviewStore,
        apply_state: ReviewApplyState,
        catalog_path: str | Path,
        review_db_path: str | Path,
        policy: ApplyPolicy,
    ) -> None:
        self.review_store = review_store
        self.apply_state = apply_state
        self.catalog_path = str(Path(catalog_path).resolve())
        self.review_db_path = str(Path(review_db_path).resolve())
        self.policy = policy

    def run(self) -> dict[str, Any]:
        rows = self.review_store.list_reviewed_image_annotations(scope_key=self.policy.scope_key)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = make_image_key(
                catalog_path=row["catalog_path"],
                source_image_id=row.get("source_image_id"),
                source_image_path=row["source_image_path"],
            )
            grouped.setdefault(key, []).append(row)

        from datetime import UTC, datetime

        started_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        run_id = self.apply_state.start_run(
            started_at=started_at,
            review_db_path=self.review_db_path,
            catalog_path=self.catalog_path,
            scope_key=self.policy.scope_key,
            policy_version=POLICY_VERSION,
            dry_run=self.policy.dry_run,
        )

        summary = {
            "considered": 0,
            "applied": 0,
            "would_apply": 0,
            "would_repair": 0,
            "verified_skipped": 0,
            "repaired": 0,
            "conflicts": 0,
            "no_label": 0,
            "errors": 0,
        }
        report_rows: list[dict[str, Any]] = []

        with LightroomCatalog.open(self.catalog_path, readonly=self.policy.dry_run, backup=not self.policy.dry_run) as cat:
            clf_log = ClassificationLog(Path(self.catalog_path))
            try:
                for image_key, image_rows in grouped.items():
                    summary["considered"] += 1
                    outcome = consolidate_image_outcome(image_rows, self.policy)
                    result = self._process_image(cat, clf_log, run_id, outcome)
                    summary[result.summary_key] += 1
                    report_rows.append(
                        {
                            "image_key": image_key,
                            "source_image_path": outcome.source_image_path,
                            "status": outcome.status,
                            "result": result.summary_key,
                            "event_type": result.event_type,
                            "message": result.message or outcome.message,
                        }
                    )
            finally:
                clf_log.close()

        ended_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.apply_state.finish_run(run_id, ended_at=ended_at, summary=summary)
        if self.policy.report_path:
            Path(self.policy.report_path).write_text(
                "\n".join(json.dumps(row, sort_keys=True) for row in report_rows) + ("\n" if report_rows else ""),
                encoding="utf-8",
            )
        return summary

    def _process_image(
        self,
        cat: LightroomCatalog,
        clf_log: ClassificationLog,
        run_id: int,
        outcome: DesiredImageOutcome,
    ) -> ApplyResult:
        from datetime import UTC, datetime

        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if outcome.status == "conflict":
            self.apply_state.record_event(
                run_id=run_id,
                image_key=outcome.image_key,
                event_type="conflict",
                created_at=now,
                details={"message": outcome.message},
            )
            return ApplyResult("conflicts", "conflict", outcome.message)
        if outcome.status == "no_label":
            self.apply_state.record_event(
                run_id=run_id,
                image_key=outcome.image_key,
                event_type="no_label",
                created_at=now,
                details={"message": outcome.message},
            )
            return ApplyResult("no_label", "no_label", outcome.message)
        if outcome.source_image_id is None:
            message = "Reviewed image is missing source_image_id; cannot apply to Lightroom catalog."
            self.apply_state.record_event(
                run_id=run_id,
                image_key=outcome.image_key,
                event_type="error",
                created_at=now,
                details={"message": message},
            )
            return ApplyResult("errors", "error", message)

        confidence_name = confidence_band(1.0)
        desired_names = managed_keyword_names_for_labels(
            outcome.labels,
            confidence_band_name=confidence_name,
            manual=self.policy.mark_manually_classed,
        )
        desired_fingerprint = managed_keyword_fingerprint(desired_names)
        source_image_id = int(outcome.source_image_id)
        current_names = cat.get_managed_keyword_names_for_image(source_image_id)
        current_fingerprint = managed_keyword_fingerprint(current_names)
        review_fingerprint = review_fingerprint_for_outcome(outcome, manual=self.policy.mark_manually_classed)

        prior_state = self.apply_state.get_image_state(outcome.image_key)
        if not self.policy.force_reapply and desired_names == current_names:
            self.apply_state.record_event(
                run_id=run_id,
                image_key=outcome.image_key,
                event_type="verified_skip",
                created_at=now,
                review_fingerprint=review_fingerprint,
                catalog_fingerprint_before=current_fingerprint,
                catalog_fingerprint_after=current_fingerprint,
                details={
                    "status": "already_current",
                    "had_prior_state": prior_state is not None,
                },
            )
            self.apply_state.upsert_image_state(
                outcome.image_key,
                catalog_path=self.catalog_path,
                source_image_id=outcome.source_image_id,
                source_image_path=outcome.source_image_path,
                scope_key=outcome.scope_key,
                last_review_fingerprint=review_fingerprint,
                last_catalog_fingerprint=current_fingerprint,
                last_applied_outcome=outcome.as_payload(manual=self.policy.mark_manually_classed),
                last_applied_at=now,
                last_run_id=run_id,
                status="verified_skip",
                message="Catalog already matches desired managed state.",
            )
            return ApplyResult("verified_skipped", "verified_skip", "Catalog already matches desired managed state.")

        if self.policy.dry_run:
            event_type = "would_repair" if current_names else "would_apply"
            self.apply_state.record_event(
                run_id=run_id,
                image_key=outcome.image_key,
                event_type=event_type,
                created_at=now,
                review_fingerprint=review_fingerprint,
                catalog_fingerprint_before=current_fingerprint,
                details={"desired_names": sorted(desired_names)},
            )
            return ApplyResult(event_type, event_type, "Dry run: no Lightroom changes written.")

        replace_catalog_species_labels(
            cat,
            source_image_id,
            outcome.labels,
            confidence_band_name=confidence_name,
            manual=self.policy.mark_manually_classed,
        )
        write_sidecar_species_labels(
            Path(outcome.source_image_path),
            outcome.labels,
            replace_existing=True,
            flat_to_remove=_existing_flat_tags(clf_log, source_image_id),
        )
        post_names = cat.get_managed_keyword_names_for_image(source_image_id)
        post_fingerprint = managed_keyword_fingerprint(post_names)
        event_type = "repair" if current_names else "apply"
        if post_names != desired_names:
            message = "Managed Lightroom keywords did not match the desired state after apply."
            self.apply_state.record_event(
                run_id=run_id,
                image_key=outcome.image_key,
                event_type="error",
                created_at=now,
                review_fingerprint=review_fingerprint,
                catalog_fingerprint_before=current_fingerprint,
                catalog_fingerprint_after=post_fingerprint,
                details={"message": message, "desired_names": sorted(desired_names), "post_names": sorted(post_names)},
            )
            self.apply_state.upsert_image_state(
                outcome.image_key,
                catalog_path=self.catalog_path,
                source_image_id=outcome.source_image_id,
                source_image_path=outcome.source_image_path,
                scope_key=outcome.scope_key,
                last_review_fingerprint=review_fingerprint,
                last_catalog_fingerprint=post_fingerprint,
                last_applied_outcome=outcome.as_payload(manual=self.policy.mark_manually_classed),
                last_applied_at=now,
                last_run_id=run_id,
                status="error",
                message=message,
            )
            return ApplyResult("errors", "error", message)
        self.apply_state.record_event(
            run_id=run_id,
            image_key=outcome.image_key,
            event_type=event_type,
            created_at=now,
            review_fingerprint=review_fingerprint,
            catalog_fingerprint_before=current_fingerprint,
            catalog_fingerprint_after=post_fingerprint,
            details={"desired_names": sorted(desired_names), "post_names": sorted(post_names)},
        )
        self.apply_state.upsert_image_state(
            outcome.image_key,
            catalog_path=self.catalog_path,
            source_image_id=outcome.source_image_id,
            source_image_path=outcome.source_image_path,
            scope_key=outcome.scope_key,
            last_review_fingerprint=review_fingerprint,
            last_catalog_fingerprint=post_fingerprint,
            last_applied_outcome=outcome.as_payload(manual=self.policy.mark_manually_classed),
            last_applied_at=now,
            last_run_id=run_id,
            status=event_type,
            message="Applied review-derived labels to Lightroom.",
        )
        summary_key = "repaired" if event_type == "repair" else "applied"
        return ApplyResult(summary_key, event_type, "Applied review-derived labels to Lightroom.")
