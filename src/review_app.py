"""
Minimal local web UI for annotation review.

This is intentionally small and self-contained. It is meant to get the review
workflow into a testable state, not to be a polished final product.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .catalog_extract import CatalogExtractor
from .ebird_reference import resolve_reference
from .extract_candidates import _build_detector, _load_object, _scope_key
from .label_resolver import AmbiguousLabelError, LabelResolver, ResolvedLabel, UnknownLabelError
from .review_queue import QueueFilters, ReviewQueue
from .review_assets import BoxedPreviewProvider
from .review_suggester import ReviewSuggester, SuggestedLabel
from .review_store import ReviewStore
from .subject_size_estimate import estimated_subject_box_size_for_candidate

STRESS_SUGGESTION_CONFIDENCE_THRESHOLD = 0.35
WORKFLOW_DETECTOR_REVIEW = "detector_review"
WORKFLOW_RUN_HYBRID_REVIEW = "run_hybrid_review"
log = logging.getLogger(__name__)


class QueueTopUpRunner:
    def __init__(
        self,
        *,
        catalog: str,
        detector_import_path: str,
        preview_dir: str,
        formats: set[str],
        folder: str | None,
        scope_folder: str | None,
        min_stars: int | None,
        batch_limit: int,
        max_preview_dimension: int,
        jpeg_quality: int,
        detector_model: str | None = None,
        verbose: bool = False,
    ) -> None:
        detector_factory = _load_object(detector_import_path)
        detector = _build_detector(detector_factory, model=detector_model)
        provider = BoxedPreviewProvider(
            preview_dir,
            max_dimension=max_preview_dimension,
            jpeg_quality=jpeg_quality,
        )
        self.extractor = CatalogExtractor(None, detector, provider)  # store injected per run
        self.catalog = catalog
        self.catalog_path = str(Path(catalog).resolve())
        self.formats = formats
        self.folder = folder
        self.scope_folder = scope_folder
        self.min_stars = min_stars
        self.batch_limit = batch_limit
        self.verbose = verbose

    def top_up(self, store: ReviewStore, active_scope: dict[str, Any] | None = None) -> tuple[int, int]:
        from datetime import UTC, datetime

        active_trip_folder = active_scope["trip_folder"] if active_scope else None
        effective_scope_folder = self.scope_folder or active_trip_folder
        effective_folder = self.folder
        if effective_folder is None and active_scope is not None:
            effective_folder = active_trip_folder
        extraction_scope_key = _scope_key(
            catalog=self.catalog,
            folder=effective_folder,
            scope_folder=effective_scope_folder,
            min_stars=self.min_stars,
            formats=self.formats,
        )
        start_after_id: int | None = None
        cursor = store.get_extraction_cursor(extraction_scope_key)
        if cursor is not None:
            start_after_id = int(cursor)

        self.extractor.store = store
        images_total = 0
        candidates_total = 0

        def _queue_depth() -> int:
            if active_scope is not None:
                return store.count_candidate_images(
                    scope_key=active_scope["scope_key"],
                    review_status="unreviewed",
                )
            return sum(
                int(scope.get("unreviewed_image_count") or 0)
                for scope in store.list_scopes()
                if scope.get("catalog_path") == self.catalog_path
            )

        while _queue_depth() < self.batch_limit:
            created_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            images_scanned, candidates_created, last_image_id = self.extractor.extract(
                self.catalog,
                formats=self.formats,
                folder_filter=effective_folder,
                scope_folder_override=effective_scope_folder,
                min_rating=self.min_stars,
                start_after_id=start_after_id,
                limit=self.batch_limit,
                created_at=created_at,
                verbose=self.verbose,
            )
            images_total += images_scanned
            candidates_total += candidates_created
            if last_image_id is not None:
                start_after_id = last_image_id
                store.set_extraction_cursor(extraction_scope_key, str(last_image_id), created_at)
            if images_scanned == 0:
                break
        return images_total, candidates_total

    def needs_scope_discovery(self, scopes: list[dict[str, Any]]) -> bool:
        catalog_scopes = [scope for scope in scopes if scope.get("catalog_path") == self.catalog_path]
        if not catalog_scopes:
            return True

        scope_hint = self.scope_folder or self.folder
        if scope_hint and not any(scope.get("trip_folder") == scope_hint for scope in catalog_scopes):
            return True

        return False


class QueueTopUpError(RuntimeError):
    pass


class QueueTopUpCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._last_error: str | None = None
        self._last_images_scanned = 0
        self._last_candidates_created = 0
        self._scope_key: str | None = None

    def ensure_running(self, store: ReviewStore, runner: QueueTopUpRunner, active_scope: dict[str, Any] | None = None) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = None
            self._last_images_scanned = 0
            self._last_candidates_created = 0
            self._scope_key = active_scope["scope_key"] if active_scope else None
        log.info(
            "Starting automatic top-up for %s",
            active_scope["scope_name"] if active_scope else f"catalog discovery: {runner.catalog_path}",
        )

        thread = threading.Thread(
            target=self._run,
            args=(store, runner, active_scope),
            daemon=True,
            name="review-topup",
        )
        thread.start()
        return True

    def _run(self, store: ReviewStore, runner: QueueTopUpRunner, active_scope: dict[str, Any] | None) -> None:
        images_total = 0
        candidates_total = 0
        try:
            while True:
                images_scanned, candidates_created = runner.top_up(store, active_scope)
                images_total += images_scanned
                candidates_total += candidates_created
                if candidates_created > 0 or images_scanned == 0:
                    break
        except Exception as exc:
            log.exception("Automatic queue top-up failed")
            with self._lock:
                self._running = False
                self._last_error = str(exc)
                self._last_images_scanned = images_total
                self._last_candidates_created = candidates_total
                self._scope_key = active_scope["scope_key"] if active_scope else None
            return

        log.info(
            "Finished automatic top-up for %s: scanned=%s candidates=%s",
            active_scope["scope_name"] if active_scope else f"catalog discovery: {runner.catalog_path}",
            images_total,
            candidates_total,
        )
        with self._lock:
            self._running = False
            self._last_error = None
            self._last_images_scanned = images_total
            self._last_candidates_created = candidates_total
            self._scope_key = active_scope["scope_key"] if active_scope else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "last_error": self._last_error,
                "last_images_scanned": self._last_images_scanned,
                "last_candidates_created": self._last_candidates_created,
                "scope_key": self._scope_key,
            }


def load_label_inventory(path: str | Path) -> list[str]:
    """
    Load canonical labels from a plain-text file or JSONL manifest.

    Supported formats:
    - one canonical label per line
    - JSONL rows containing `truth_label`
    """
    labels: list[str] = []
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                row = json.loads(line)
                if "truth_label" in row:
                    labels.append(row["truth_label"])
            else:
                labels.append(line)
    # preserve order while de-duping
    return list(dict.fromkeys(labels))


AGGREGATE_SCOPE_KEY_PREFIX = "__aggregate__:"


def _aggregate_scope_key(catalog_path: str, trip_folder: str) -> str:
    payload = {
        "catalog_path": str(Path(catalog_path).resolve()),
        "trip_folder": str(Path(trip_folder).resolve()),
    }
    return AGGREGATE_SCOPE_KEY_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _parse_aggregate_scope_key(scope_key: str | None) -> dict[str, str] | None:
    if not scope_key or not scope_key.startswith(AGGREGATE_SCOPE_KEY_PREFIX):
        return None
    try:
        payload = json.loads(scope_key.removeprefix(AGGREGATE_SCOPE_KEY_PREFIX))
    except json.JSONDecodeError:
        return None
    catalog_path = payload.get("catalog_path")
    trip_folder = payload.get("trip_folder")
    if not catalog_path or not trip_folder:
        return None
    return {
        "catalog_path": str(Path(catalog_path).resolve()),
        "trip_folder": str(Path(trip_folder).resolve()),
    }


def _html_page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; background: #f4f1ea; color: #1b1b1b; }}
    .page {{ padding: 20px; }}
    .app-shell {{ display: grid; gap: 16px; }}
    .topbar {{ display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 12px; }}
    .topbar h1 {{ margin: 0; font-size: 28px; line-height: 1.1; }}
    .scope-pill {{ background: #f7f2ea; border: 1px solid #ddd4c5; border-radius: 10px; padding: 8px 12px; min-width: 0; }}
    .scope-pill .label {{ display: block; font-size: 12px; color: #5e564a; margin-bottom: 2px; }}
    .scope-pill .value {{ display: block; font-size: 15px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .topbar-actions {{ display: flex; gap: 8px; align-items: center; }}
    .header-status {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .status-badge {{ background: #f7f2ea; border: 1px solid #ddd4c5; border-radius: 999px; padding: 7px 10px; font-size: 13px; white-space: nowrap; }}
    .wrap {{ display: grid; grid-template-columns: minmax(0, 1.3fr) minmax(360px, 0.9fr); gap: 20px; align-items: start; }}
    .panel {{ background: #fffdf9; border: 1px solid #ddd4c5; border-radius: 12px; padding: 14px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    .preview-panel {{ display: flex; flex-direction: column; }}
    .preview-frame {{ display: flex; justify-content: center; align-items: flex-start; min-height: 0; }}
    img {{ max-width: 100%; max-height: 82vh; width: auto; height: auto; display: block; border-radius: 8px; }}
    .candidate-preview {{ max-height: 50vh; }}
    .preview-controls {{ margin-top: 12px; display: grid; gap: 10px; }}
    .meta {{ font-size: 14px; line-height: 1.5; }}
    .meta code {{ font-size: 12px; }}
    .error {{ color: #8b1e1e; font-weight: 600; }}
    .recent form, .actions form {{ display: inline-block; margin: 4px 4px 0 0; }}
    button {{ border: 1px solid #aa9f8f; background: #f7f2ea; border-radius: 8px; padding: 6px 10px; cursor: pointer; font-size: 13px; line-height: 1.25; }}
    button.primary {{ background: #184e3b; color: white; border-color: #184e3b; }}
    input[type=text] {{ width: 100%; padding: 8px 10px; border-radius: 8px; border: 1px solid #c9bfaf; box-sizing: border-box; }}
    input.manual-label-input {{ width: 100%; max-width: 420px; }}
    label.toggle {{ display: inline-flex; align-items: center; gap: 8px; margin: 8px 0; }}
    .small {{ font-size: 12px; color: #5e564a; }}
    .resolved {{ background: #f7f2ea; border-radius: 8px; padding: 8px 10px; margin-top: 8px; }}
    .full {{ grid-column: 1 / -1; }}
    .nav {{ display: flex; gap: 10px; margin-bottom: 14px; }}
    .nav.compact {{ margin-bottom: 0; }}
    .details-meta {{ margin-top: 8px; }}
    details.details-meta {{ background: #f7f2ea; border-radius: 10px; border: 1px solid #ddd4c5; padding: 0; overflow: hidden; }}
    details.details-meta summary {{ cursor: pointer; list-style: none; padding: 10px 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    details.details-meta summary::-webkit-details-marker {{ display: none; }}
    .details-summary-main {{ min-width: 0; }}
    .details-summary-title {{ display: block; font-size: 12px; color: #5e564a; margin-bottom: 2px; }}
    .details-summary-value {{ display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14px; }}
    .details-summary-burst {{ flex: 0 0 auto; font-size: 14px; color: #5e564a; }}
    .details-body {{ padding: 0 12px 12px 12px; }}
    .details-grid {{ display: grid; grid-template-columns: 1fr; gap: 4px; }}
    .meta-strip {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; background: #f7f2ea; border: 1px solid #ddd4c5; border-radius: 10px; padding: 8px 12px; margin-bottom: 8px; }}
    .meta-strip .source-compact {{ min-width: 0; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .meta-strip .burst-compact {{ flex: 0 0 auto; font-size: 13px; color: #5e564a; }}
    .right-stack {{ display: grid; gap: 8px; align-content: start; }}
    .reference-card {{ display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 10px; align-items: start; }}
    .reference-thumb {{ width: 96px; height: 96px; object-fit: cover; max-height: 96px; }}
    .reference-card img {{ max-height: 96px; width: 96px; object-fit: cover; }}
    .button-cluster {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
    .manual-label-row {{ display: grid; grid-template-columns: minmax(220px, 420px) auto; align-items: center; column-gap: 12px; row-gap: 8px; margin-top: 6px; }}
    .manual-label-row .manual-label-input {{ min-width: 0; }}
    .form-actions {{ display: flex; align-items: center; justify-content: flex-start; gap: 12px; flex-wrap: nowrap; margin-top: 0; }}
    .form-actions-left {{ display: flex; gap: 8px; align-items: center; flex-wrap: nowrap; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #ddd4c5; vertical-align: top; }}
    th {{ font-size: 13px; color: #5e564a; }}
    @media (max-width: 980px) {{
      .wrap {{ grid-template-columns: 1fr; }}
      .topbar {{ grid-template-columns: 1fr; }}
      .scope-pill .value {{ white-space: normal; }}
      .reference-card {{ grid-template-columns: 1fr; }}
      .reference-card img, .reference-thumb {{ width: 100%; height: auto; max-height: 180px; }}
      .meta-strip {{ align-items: flex-start; flex-direction: column; }}
      .manual-label-row {{ grid-template-columns: 1fr; }}
      .form-actions, .form-actions-left {{ flex-wrap: wrap; }}
    }}
  </style>
</head>
<body>
{body}
<script>
document.addEventListener('keydown', function (event) {{
  const active = document.activeElement;
  const activeTag = (active && active.tagName || '').toLowerCase();
  const activeType = (active && active.type || '').toLowerCase();
  const isTextInput = activeTag === 'textarea' || (activeTag === 'input' && !['hidden', 'checkbox', 'button', 'submit'].includes(activeType));
  const textInput = document.querySelector('input[name="label_input"]');
  const reviewForm = document.querySelector('form[data-role="review-form"]');
  const selectedTruthLabel = document.getElementById('selected-truth-label');

  function submitResolvedLabel(truthLabel) {{
    if (!reviewForm || !selectedTruthLabel) {{
      return false;
    }}
    if (textInput) {{
      textInput.value = '';
    }}
    selectedTruthLabel.value = truthLabel;
    reviewForm.submit();
    return true;
  }}

  if (/^[1-5]$/.test(event.key) && !isTextInput) {{
    const form = document.querySelector('form[data-recent-index="' + event.key + '"]');
    if (form) {{
      const truthLabel = form.getAttribute('data-truth-label');
      event.preventDefault();
      if (truthLabel && submitResolvedLabel(truthLabel)) {{
        return;
      }}
      form.submit();
      return;
    }}
  }}

  if ((event.key === 'r' || event.key === 'R') && !event.metaKey && !event.ctrlKey && !isTextInput) {{
    const form = document.querySelector('form[data-action="reject"]');
    if (form) {{
      event.preventDefault();
      form.submit();
      return;
    }}
  }}

  if (event.key === '0' && !isTextInput) {{
    const form = document.querySelector('form[data-suggestion="true"]');
    if (form) {{
      const truthLabel = form.getAttribute('data-truth-label');
      event.preventDefault();
      if (truthLabel && submitResolvedLabel(truthLabel)) {{
        return;
      }}
      form.submit();
      return;
    }}
  }}

  if ((event.key === 't' || event.key === 'T') && !isTextInput) {{
    const stress = document.getElementById('stress-toggle');
    if (stress) {{
      event.preventDefault();
      stress.checked = !stress.checked;
    }}
  }}

  if (!isTextInput && (event.key === 'k' || event.key === 'K')) {{
    const form = document.querySelector('form[data-action="skip"]');
    if (form) {{
      event.preventDefault();
      form.submit();
    }}
  }}

  if (!isTextInput && (event.key === 'n' || event.key === 'N')) {{
    const form = document.querySelector('form[data-action="not_a_bird"]');
    if (form) {{
      event.preventDefault();
      form.submit();
    }}
  }}

  if (!isTextInput && (event.key === 'f' || event.key === 'F') && textInput) {{
    event.preventDefault();
    textInput.focus();
    textInput.select();
  }}

  if (!isTextInput && (event.key === 'p' || event.key === 'P')) {{
    const link = document.querySelector('[data-nav="prev"]');
    if (link) {{
      event.preventDefault();
      window.location = link.getAttribute('href');
    }}
  }}
}});
</script>
</body>
</html>"""
    return doc.encode("utf-8")


class ReviewAppHandler(BaseHTTPRequestHandler):
    store: ReviewStore
    resolver: LabelResolver
    suggester: ReviewSuggester | None
    suggestion_status: str | None
    topup_runner: QueueTopUpRunner | None
    topup_coordinator: QueueTopUpCoordinator | None
    topup_low_watermark: int

    @staticmethod
    def _workflow_type(scope: dict[str, Any] | None) -> str:
        if scope is None:
            return WORKFLOW_DETECTOR_REVIEW
        return str(scope.get("workflow_type") or WORKFLOW_DETECTOR_REVIEW)

    @classmethod
    def _is_run_hybrid_review(cls, scope: dict[str, Any] | None) -> bool:
        return cls._workflow_type(scope) == WORKFLOW_RUN_HYBRID_REVIEW

    @classmethod
    def _allows_stress(cls, scope: dict[str, Any] | None) -> bool:
        return not cls._is_run_hybrid_review(scope)

    @classmethod
    def _allows_burst_apply(cls, scope: dict[str, Any] | None) -> bool:
        return not cls._is_run_hybrid_review(scope)

    @classmethod
    def _allowed_actions(cls, scope: dict[str, Any] | None) -> set[str]:
        if cls._is_run_hybrid_review(scope):
            return {"save", "skip"}
        return {"save", "save_burst", "skip", "reject", "unsure", "not_a_bird", "bad_crop", "duplicate"}

    @staticmethod
    def _scope_member_keys(scope: dict[str, Any] | None) -> tuple[str, ...]:
        if scope is None:
            return ()
        scope_keys = scope.get("scope_keys")
        if scope_keys:
            return tuple(str(value) for value in scope_keys if value)
        scope_key = scope.get("scope_key")
        return (str(scope_key),) if scope_key else ()

    @staticmethod
    def _is_aggregate_scope(scope: dict[str, Any] | None) -> bool:
        return bool(scope and scope.get("scope_kind") == "aggregate")

    def _touch_scope(self, scope: dict[str, Any] | None) -> None:
        if scope is None:
            return
        opened_at = _utc_now()
        for member_scope_key in self._scope_member_keys(scope):
            self.store.touch_scope(member_scope_key, opened_at)

    @staticmethod
    def _aggregate_scope_from_members(scopes: list[dict[str, Any]], *, catalog_path: str, trip_folder: str) -> dict[str, Any] | None:
        resolved_catalog_path = str(Path(catalog_path).resolve())
        resolved_trip_folder = str(Path(trip_folder).resolve())
        member_scopes = [
            scope for scope in scopes
            if str(scope.get("catalog_path") or "") == resolved_catalog_path
            and str(scope.get("trip_folder") or "").startswith(resolved_trip_folder.rstrip("/") + "/")
        ]
        if len(member_scopes) < 2:
            return None

        workflow_types = {str(scope.get("workflow_type") or WORKFLOW_DETECTOR_REVIEW) for scope in member_scopes}
        workflow_type = member_scopes[0].get("workflow_type") if len(workflow_types) == 1 else WORKFLOW_DETECTOR_REVIEW
        created_at = min(str(scope.get("created_at") or "") for scope in member_scopes)
        last_opened_values = [str(scope.get("last_opened_at") or "") for scope in member_scopes if scope.get("last_opened_at")]
        return {
            "scope_key": _aggregate_scope_key(resolved_catalog_path, resolved_trip_folder),
            "scope_keys": [str(scope["scope_key"]) for scope in member_scopes],
            "scope_kind": "aggregate",
            "scope_name": f"{member_scopes[0].get('catalog_name') or Path(resolved_catalog_path).stem} / {resolved_trip_folder}",
            "catalog_name": member_scopes[0].get("catalog_name") or Path(resolved_catalog_path).stem,
            "catalog_path": resolved_catalog_path,
            "trip_folder": resolved_trip_folder,
            "workflow_type": workflow_type,
            "created_at": created_at,
            "last_opened_at": max(last_opened_values) if last_opened_values else None,
            "status": "aggregate",
            "notes": f"Aggregates {len(member_scopes)} child scopes",
            "image_count": sum(int(scope.get("image_count") or 0) for scope in member_scopes),
            "candidate_count": sum(int(scope.get("candidate_count") or 0) for scope in member_scopes),
            "unreviewed_image_count": sum(int(scope.get("unreviewed_image_count") or 0) for scope in member_scopes),
            "unreviewed_count": sum(int(scope.get("unreviewed_count") or 0) for scope in member_scopes),
            "reviewed_count": sum(int(scope.get("reviewed_count") or 0) for scope in member_scopes),
            "member_scope_count": len(member_scopes),
        }

    @classmethod
    def _aggregate_scopes(cls, scopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        aggregates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

        for scope in scopes:
            trip_folder = str(scope.get("trip_folder") or "")
            catalog_path = str(scope.get("catalog_path") or "")
            if not trip_folder.startswith("/"):
                continue
            parent = str(Path(trip_folder).parent)
            if parent == trip_folder:
                continue
            groups.setdefault((catalog_path, parent), []).append(scope)

        for (catalog_path, parent), members in groups.items():
            if len(members) < 2 or (catalog_path, parent) in seen:
                continue
            aggregate = cls._aggregate_scope_from_members(scopes, catalog_path=catalog_path, trip_folder=parent)
            if aggregate is None:
                continue
            aggregates.append(aggregate)
            seen.add((catalog_path, parent))

        aggregates.sort(
            key=lambda scope: (
                str(scope.get("catalog_name") or ""),
                str(scope.get("trip_folder") or ""),
            )
        )
        return aggregates

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/review"}:
            self._handle_review_get(parsed)
            return
        if parsed.path == "/scopes":
            self._handle_scopes_get(parsed)
            return
        if parsed.path == "/summary":
            self._handle_summary_get(parsed)
            return
        if parsed.path.startswith("/preview/"):
            self._handle_preview(parsed.path.removeprefix("/preview/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/review":
            self._handle_review_post()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_review_get(self, parsed) -> None:
        params = parse_qs(parsed.query)
        scope_key = params.get("scope", [None])[0]
        scope = self._resolve_scope(scope_key)
        if scope is None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/scopes")
            self.end_headers()
            return
        self._touch_scope(scope)
        self._maybe_start_background_top_up(scope)
        candidate_id = params.get("id", [None])[0]
        error = params.get("error", [""])[0]
        prefill_selected_truth_label = params.get("selected_truth_label", [""])[0]
        prefill_label_input = params.get("label_input", [""])[0]
        prefill_stress = params.get("stress", ["0"])[0] == "1"
        prefill_notes = params.get("notes", [""])[0]
        stress_reason = params.get("stress_reason", [""])[0]

        member_scope_keys = self._scope_member_keys(scope)
        queue = ReviewQueue(self.store, QueueFilters(scope_keys=member_scope_keys, review_status="unreviewed"))
        if candidate_id is None:
            next_candidate = queue.next_candidate()
            if next_candidate is None:
                status = self._top_up_status()
                if (
                    not self._is_aggregate_scope(scope)
                    and self.topup_runner is not None
                    and self.topup_coordinator is not None
                ):
                    if not status["running"]:
                        self.topup_coordinator.ensure_running(self.store, self.topup_runner, scope)
                        status = self._top_up_status()
                    if status["running"] and status.get("scope_key") in {None, scope["scope_key"]}:
                        self._write_html("Queue Top-Up", self._render_top_up_pending(status, scope["scope_key"]))
                        return
                    next_candidate = queue.next_candidate()
                if status["last_error"]:
                    self._write_html("Top-Up Failed", self._render_top_up_failed(status["last_error"], scope["scope_key"]))
                    return
                if next_candidate is None:
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", f"/summary?scope={_q(scope['scope_key'])}")
                    self.end_headers()
                    return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/review?scope={_q(scope['scope_key'])}&id={next_candidate['id']}")
            self.end_headers()
            return

        candidate = queue.open_candidate(candidate_id)
        if candidate is None:
            self._write_html("Not Found", "<div class='panel'>Candidate not found.</div>")
            return

        image = self.store.get_image(candidate["image_id"]) or {}
        annotation = self.store.get_annotation(candidate_id)
        recent = self.store.recent_labels(limit=5)
        prev_candidate = self.store.previous_reviewed_candidate(candidate_id, scope_keys=member_scope_keys)
        burst_targets = self.store.burst_candidates(candidate_id, include_reviewed=False)
        burst_position = self.store.burst_position(candidate_id)
        queue_position = self.store.queue_position(candidate_id, scope_keys=member_scope_keys, review_status="unreviewed")
        unreviewed_candidates = self.store.count_candidates(scope_keys=member_scope_keys, review_status="unreviewed")
        unreviewed_images = self.store.count_candidate_images(scope_keys=member_scope_keys, review_status="unreviewed")
        suggestion = self._build_suggestion(candidate, image, scope)
        estimated_subject_box_size = self._estimated_subject_box_size(candidate, image)

        body = self._render_candidate(
            scope,
            candidate,
            image,
            annotation,
            recent,
            error,
            prev_candidate,
            len(burst_targets),
            burst_position,
            queue_position,
            unreviewed_images,
            unreviewed_candidates,
            suggestion,
            estimated_subject_box_size,
            self.suggestion_status,
            prefill_selected_truth_label,
            prefill_label_input,
            prefill_stress,
            prefill_notes,
            stress_reason,
        )
        self._write_html("Review Candidate", body)

    def _handle_summary_get(self, parsed) -> None:
        params = parse_qs(parsed.query)
        scope = self._resolve_scope(params.get("scope", [None])[0])
        if scope is None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/scopes")
            self.end_headers()
            return
        self._touch_scope(scope)
        self._maybe_start_background_top_up(scope)
        member_scope_keys = self._scope_member_keys(scope)
        queue = ReviewQueue(self.store, QueueFilters(scope_keys=member_scope_keys, review_status="unreviewed"))
        next_candidate = queue.next_candidate()
        summary = self.store.summary_counts(scope_keys=member_scope_keys)
        body = self._render_summary(scope, summary, next_candidate["id"] if next_candidate else None, self._top_up_status())
        self._write_html("Review Summary", body)

    def _handle_scopes_get(self, parsed) -> None:
        scopes = self.store.list_scopes()
        if self.topup_runner is not None and self.topup_coordinator is not None:
            total_unreviewed = self.store.count_candidates(review_status="unreviewed")
            status = self._top_up_status()
            only_legacy_scopes = bool(scopes) and all(scope["scope_key"] == "__legacy__" for scope in scopes)
            needs_discovery = self.topup_runner.needs_scope_discovery(scopes)
            if (
                total_unreviewed <= self.topup_low_watermark
                or only_legacy_scopes
                or not scopes
                or needs_discovery
            ) and not status["running"]:
                log.info(
                    "Scopes page triggered automatic discovery: total_unreviewed=%s watermark=%s only_legacy=%s needs_discovery=%s",
                    total_unreviewed,
                    self.topup_low_watermark,
                    only_legacy_scopes,
                    needs_discovery,
                )
                self.topup_coordinator.ensure_running(self.store, self.topup_runner, None)
                scopes = self.store.list_scopes()
        aggregate_scopes = self._aggregate_scopes(scopes)
        body = self._render_scopes(scopes, aggregate_scopes, self._top_up_status())
        self._write_html("Review Scopes", body)

    def _top_up_status(self) -> dict[str, Any]:
        if self.topup_coordinator is None:
            return {
                "running": False,
                "last_error": None,
                "last_images_scanned": 0,
                "last_candidates_created": 0,
            }
        return self.topup_coordinator.snapshot()

    def _maybe_start_background_top_up(self, scope: dict[str, Any] | None) -> None:
        if self.topup_runner is None or self.topup_coordinator is None:
            return
        if scope is None:
            return
        if self._is_aggregate_scope(scope):
            return
        unreviewed = self.store.count_candidates(scope_key=scope["scope_key"], review_status="unreviewed")
        status = self._top_up_status()
        if unreviewed <= self.topup_low_watermark and (not status["running"] or status.get("scope_key") == scope["scope_key"]):
            self.topup_coordinator.ensure_running(self.store, self.topup_runner, scope)

    def _handle_review_post(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = parse_qs(self.rfile.read(length).decode("utf-8"))
        scope_key = payload.get("scope_key", [""])[0]
        candidate_id = payload.get("candidate_id", [""])[0]
        action = payload.get("action", [""])[0]
        label_input = payload.get("label_input", [""])[0]
        selected_truth_label = payload.get("selected_truth_label", [""])[0]
        stress = payload.get("stress", ["0"])[0] == "1"
        notes = payload.get("notes", [""])[0]
        scope = self._resolve_scope(scope_key)

        if not candidate_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "candidate_id required")
            return

        try:
            if action not in self._allowed_actions(scope):
                raise ValueError(
                    f"Action '{action}' is not allowed for workflow "
                    f"'{self._workflow_type(scope)}'"
                )
            if action == "skip":
                self.store.mark_candidate_skipped(candidate_id)
            elif action in {"reject", "unsure", "not_a_bird", "bad_crop", "duplicate"}:
                self.store.upsert_annotation(
                    {
                        "candidate_id": candidate_id,
                        "annotation_status": action,
                        "stress": False,
                        "reject_sample": action == "reject",
                        "unsure": action == "unsure",
                        "not_a_bird": action == "not_a_bird",
                        "bad_crop": action == "bad_crop",
                        "duplicate_sample": action == "duplicate",
                        "notes": notes,
                        "annotated_at": _utc_now(),
                    }
                )
            elif action == "save":
                resolved = self._resolve_label(selected_truth_label, label_input)
                candidate = self.store.get_candidate(candidate_id) or {}
                image = self.store.get_image(candidate.get("image_id")) or {}
                suggestion = self._build_suggestion(candidate, image, scope)
                stress_reason = self._stress_reason(resolved.truth_label, suggestion)
                if self._allows_stress(scope) and stress_reason and not stress:
                    stress = True
                elif not self._allows_stress(scope):
                    stress = False
                row = self._build_labeled_row(candidate_id, resolved, label_input or selected_truth_label, stress, notes)
                self.store.upsert_annotation(row)
            elif action == "save_burst":
                resolved = self._resolve_label(selected_truth_label, label_input)
                candidate = self.store.get_candidate(candidate_id) or {}
                image = self.store.get_image(candidate.get("image_id")) or {}
                suggestion = self._build_suggestion(candidate, image, scope)
                stress_reason = self._stress_reason(resolved.truth_label, suggestion)
                if self._allows_stress(scope) and stress_reason and not stress:
                    stress = True
                elif not self._allows_stress(scope):
                    stress = False
                row = self._build_labeled_row(candidate_id, resolved, label_input or selected_truth_label, stress, notes)
                self.store.upsert_annotation(row)
                self.store.apply_annotation_to_burst(candidate_id, row)
            else:
                raise ValueError(f"Unsupported action '{action}'")
        except (UnknownLabelError, AmbiguousLabelError, ValueError) as e:
            self._redirect_review(
                candidate_id,
                scope_key=scope_key,
                error=str(e),
                selected_truth_label=selected_truth_label,
                label_input=label_input,
                stress=stress,
                notes=notes,
            )
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/review?scope={_q(scope_key)}")
        self.end_headers()

    def _resolve_label(self, selected_truth_label: str, label_input: str) -> ResolvedLabel:
        if selected_truth_label:
            return self.resolver.resolve_recent_label(selected_truth_label)
        return self.resolver.resolve_name(label_input)

    @staticmethod
    def _estimated_subject_box_size(candidate: dict[str, Any], image: dict[str, Any]) -> tuple[float, float] | None:
        image_path = image.get("source_image_path")
        if not image_path:
            return None
        try:
            return estimated_subject_box_size_for_candidate(image_path, candidate)
        except Exception:
            return None

    def _handle_preview(self, candidate_id: str) -> None:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        path = Path(candidate["preview_image_path"])
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        mime, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "image/jpeg")
        self.end_headers()
        with path.open("rb") as f:
            self.wfile.write(f.read())

    def _render_candidate(
        self,
        scope: dict[str, Any],
        candidate: dict[str, Any],
        image: dict[str, Any],
        annotation: dict[str, Any] | None,
        recent: list[dict[str, Any]],
        error: str,
        prev_candidate: dict[str, Any] | None,
        burst_target_count: int,
        burst_position: tuple[int, int] | None,
        queue_position: tuple[int, int] | None,
        unreviewed_images: int,
        unreviewed_candidates: int,
        suggestion: SuggestedLabel | None,
        estimated_subject_box_size: tuple[float, float] | None,
        suggestion_status: str | None,
        prefill_selected_truth_label: str,
        prefill_label_input: str,
        prefill_stress: bool,
        prefill_notes: str,
        stress_reason: str,
    ) -> str:
        is_run_hybrid_review = self._is_run_hybrid_review(scope)
        allow_stress = self._allows_stress(scope)
        allow_burst_apply = self._allows_burst_apply(scope)
        default_save_action = "save_burst" if allow_burst_apply and burst_target_count else "save"
        default_save_label = "Save + Apply To Burst" if default_save_action == "save_burst" else "Save Label"
        burst_label = ""
        if burst_position is not None:
            burst_label = f" ({burst_position[0]}/{burst_position[1]})"
        recent_forms = []
        for idx, row in enumerate(recent, start=1):
            recent_forms.append(
                f"""
                <form method="post" action="/review" data-recent-index="{idx}" data-truth-label="{html.escape(row['truth_label'])}">
                  <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
                  <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                  <input type="hidden" name="action" value="{default_save_action}">
                  <input type="hidden" name="selected_truth_label" value="{html.escape(row['truth_label'])}">
                  <input type="hidden" name="stress" value="0">
                  <button type="submit">({idx}) {html.escape(row['truth_common_name'])}</button>
                </form>
                """
            )

        error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
        notes = html.escape(prefill_notes or (annotation or {}).get("notes", ""))
        resolved_preview = ""
        if annotation and annotation["annotation_status"] == "labeled":
            resolved_preview = (
                f"<div class='resolved'><strong>Current:</strong> "
                f"{html.escape(annotation['truth_common_name'])} "
                f"({html.escape(annotation['truth_sci_name'])})"
                f"{' [stress]' if allow_stress and annotation['stress'] else ''}</div>"
            )
        selected_preview = ""
        if prefill_label_input:
            selected_preview = f"""
            <div class="resolved">
              <div><strong>Pending label</strong>: {html.escape(prefill_label_input)}</div>
            </div>
            """
        suggestion_html = ""
        if suggestion is not None:
            suggestion_html = f"""
            <div class="resolved">
              <div><strong>Classifier suggestion</strong>: {html.escape(suggestion.truth_common_name)} ({html.escape(suggestion.truth_sci_name)}) {suggestion.confidence:.1%}</div>
              <form method="post" action="/review" data-suggestion="true" data-truth-label="{html.escape(suggestion.truth_label)}" style="margin-top:8px;">
                <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
                <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                <input type="hidden" name="action" value="{default_save_action}">
                <input type="hidden" name="selected_truth_label" value="{html.escape(suggestion.truth_label)}">
                <input type="hidden" name="stress" value="0">
                <button type="submit">(0) Accept Suggestion</button>
              </form>
            </div>
            """
        elif suggestion_status:
            suggestion_html = f"""
            <div class="resolved">
              <div><strong>Classifier suggestion unavailable</strong></div>
              <div class="small">{html.escape(suggestion_status)}</div>
            </div>
            """
        stress_reason_html = ""
        if allow_stress and stress_reason:
            stress_reason_html = f"""
            <div class="resolved">
              <div><strong>Stress suggested</strong></div>
              <div class="small">{html.escape(stress_reason)}</div>
            </div>
            """
        ebird_reference = self._build_ebird_reference(
            annotation=annotation,
            suggestion=suggestion,
            selected_truth_label=prefill_selected_truth_label,
        )
        ebird_reference_html = ""
        if ebird_reference is not None:
            reference_image_html = ""
            if ebird_reference.preview_image_url:
                reference_image_html = (
                    f'<div>'
                    f'<img src="{html.escape(ebird_reference.preview_image_url)}" '
                    f'alt="{html.escape(ebird_reference.truth_common_name)} Macaulay reference photo" '
                    f'class="reference-thumb"></div>'
                )
            ebird_reference_html = f"""
            <div class="resolved reference-card">
              {reference_image_html or '<div class="small">Reference photo unavailable.</div>'}
              <div>
                <div><strong>eBird reference</strong>: {html.escape(ebird_reference.truth_common_name)} ({html.escape(ebird_reference.truth_sci_name)})</div>
                <div class="small" style="margin-top:8px">Remote eBird / Macaulay reference only. This tool does not store or export the media.</div>
                <div class="button-cluster">
                <a href="{html.escape(ebird_reference.species_url)}" target="_blank" rel="noopener"><button type="button">Open eBird Species Page</button></a>
                <a href="{html.escape(ebird_reference.macaulay_asset_url or ebird_reference.media_search_url)}" target="_blank" rel="noopener"><button type="button">Open Macaulay Reference</button></a>
                </div>
              </div>
            </div>
            """
        estimated_size_html = ""
        if estimated_subject_box_size is not None:
            estimated_size_html = (
                f"<div><strong>Estimated subject box size:</strong> "
                f"{estimated_subject_box_size[0]:.1f} cm × {estimated_subject_box_size[1]:.1f} cm</div>"
                "<div class='small'>Approximate, assuming the boxed subject is at the focus distance.</div>"
            )

        queue_position_value = html.escape(f"{queue_position[0]}/{queue_position[1]}" if queue_position else "n/a")
        queue_depth_value = html.escape(f"{unreviewed_candidates} candidates")
        burst_compact = html.escape(f"{burst_position[0]}/{burst_position[1]}") if burst_position else "n/a"
        source_path = html.escape(str(image.get("source_image_path", "")))
        source_path_obj = Path(str(image.get("source_image_path", ""))) if image.get("source_image_path") else None
        source_name = source_path_obj.name if source_path_obj else ""
        source_date = source_path_obj.parent.name if source_path_obj else ""
        scope_path = str(scope.get("trip_folder") or "")
        scope_leaf = Path(scope_path).name if scope_path else scope.get("scope_name", "")
        compact_scope = f"{scope.get('catalog_name') or ''} / {scope_leaf}".strip(" /")
        compact_source = " / ".join(part for part in [scope_leaf, source_date, source_name] if part)
        burst_help = (
            "This candidate has "
            + str(burst_target_count)
            + " other safe burst item(s), so label actions default to burst apply."
            if allow_burst_apply and burst_target_count
            else "No safe burst targets available. Burst apply only includes other unreviewed, single-detection candidates in the same burst group."
            if allow_burst_apply
            else "This workflow labels one image at a time. Save applies only to the current image."
        )
        shortcut_help = (
            "Type a common or scientific name, or use a recent label button below. Shortcuts: 0, 1-5, R, T, K, N, P, F. Label accepts default to burst apply when eligible."
            if not is_run_hybrid_review
            else "Type a common or scientific name, or use a recent label button below. Shortcuts: 0, 1-5, K, P, F."
        )
        stress_toggle_html = (
            f"""<label class="toggle"><input id="stress-toggle" type="checkbox" name="stress" value="1" {'checked' if prefill_stress else ''}> (T) mark as stress</label>"""
            if allow_stress
            else ""
        )
        other_outcomes_html = (
            f"""
                  <div class="actions">
                    <div><strong>Other outcomes</strong></div>
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'reject', '(R) Reject')}
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'unsure', 'Unsure')}
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'not_a_bird', '(N) Not a Bird')}
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'bad_crop', 'Bad Crop')}
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'duplicate', 'Duplicate')}
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'skip', '(K) Skip')}
                  </div>
            """
            if not is_run_hybrid_review
            else f"""
                  <div class="actions">
                    <div><strong>Skip</strong></div>
                    {self._outcome_button(scope['scope_key'], candidate['id'], 'skip', '(K) Skip')}
                  </div>
            """
        )

        return f"""
        <div class="page">
          <div class="app-shell">
            <div class="topbar panel">
              <h1>review_app</h1>
              <div class="scope-pill" title="{html.escape(scope.get('scope_name') or '')}">
                <span class="label">Scope</span>
                <span class="value">{html.escape(compact_scope)}</span>
              </div>
              <div class="topbar-actions">
                <div class="header-status">
                  <span class="status-badge">Queue {queue_position_value}</span>
                  <span class="status-badge">{queue_depth_value}</span>
                </div>
                <a href="/scopes"><button type="button">Scopes</button></a>
                <a href="/summary?scope={_q(scope['scope_key'])}"><button type="button">Summary</button></a>
              </div>
            </div>

            <div class="wrap">
              <div class="panel preview-panel">
                <div class="preview-frame">
                  <img class="candidate-preview" src="/preview/{html.escape(candidate['id'])}" alt="preview">
                </div>
                <div class="preview-controls">
                  <form method="post" action="/review" data-role="review-form">
                    <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
                    <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                    <input type="hidden" name="action" value="{default_save_action}">
                    <input id="selected-truth-label" type="hidden" name="selected_truth_label" value="{html.escape(prefill_selected_truth_label)}">
                    <div><strong>Manual label</strong></div>
                    <div class="manual-label-row">
                      <input class="manual-label-input" type="text" name="label_input" placeholder="Type common or scientific name" value="{html.escape(prefill_label_input)}">
                        <div class="form-actions">
                          <div class="form-actions-left">
                            <button class="primary" type="submit">{default_save_label}</button>
                            {f'<button type="submit" formaction="/review" formmethod="post" name="action" value="save">Save Current Only</button>' if allow_burst_apply and burst_target_count else ''}
                          </div>
                          {stress_toggle_html}
                        </div>
                    </div>
                    <div class="small">{shortcut_help}</div>
                  </form>
                  <div class="recent">
                    <div><strong>Recent labels</strong> <span class="small">(1-5)</span></div>
                    {''.join(recent_forms) or '<div class="small">No recent labels yet.</div>'}
                  </div>
                  {other_outcomes_html}
                </div>
              </div>
              <div class="panel">
                <div class="right-stack">
                  <div class="meta-strip" title="{source_path}">
                    <div class="source-compact">{html.escape(compact_source or str(image.get('source_image_path', '')))}</div>
                    <div class="burst-compact"><strong>Burst</strong> {burst_compact}</div>
                  </div>
                  {error_html}
                  {resolved_preview}
                  {selected_preview}
                  {suggestion_html}
                  {stress_reason_html}
                  <div>
                    {f'<a data-nav="prev" href="/review?scope={_q(scope["scope_key"])}&id={html.escape(prev_candidate["id"])}"><button type="button">(P) Previous Reviewed Candidate</button></a>' if prev_candidate else '<button type="button" disabled>(P) Previous Reviewed Candidate</button>'}
                  </div>
                  {ebird_reference_html}
                  <details class="details-meta">
                    <summary>
                      <div class="details-summary-main">
                        <span class="details-summary-title">Details</span>
                        <span class="details-summary-value"><code>{source_path}</code></span>
                      </div>
                      <div class="details-summary-burst"><strong>Burst</strong> {burst_compact}</div>
                    </summary>
                    <div class="details-body meta">
                      <div class="details-grid">
                        <div><strong>Scope:</strong> {html.escape(scope.get('scope_name', ''))}</div>
                        <div><strong>Candidate:</strong> <code>{html.escape(candidate['id'])}</code></div>
                        <div><strong>Captured:</strong> {html.escape(str(image.get('capture_datetime', '')))}</div>
                        <div><strong>Detector:</strong> {html.escape(str(candidate.get('detector_name', '')))} @ {candidate.get('detector_confidence', 0):.2f}</div>
                        <div><strong>Region:</strong> {html.escape(str(image.get('region_hint', '')))}</div>
                        <div><strong>Burst Group:</strong> {html.escape(str(image.get('burst_group_id', '')))}{html.escape(burst_label)}</div>
                        <div><strong>Queue Depth:</strong> {unreviewed_images} image(s), {unreviewed_candidates} candidate(s) yolo'd and waiting</div>
                        {estimated_size_html}
                        <div class="small">{html.escape(burst_help)}</div>
                      </div>
                    </div>
                  </details>
                  <form method="post" action="/review" style="margin-top:6px">
                    <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
                    <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                    <input type="hidden" name="action" value="save">
                    <input type="hidden" name="selected_truth_label" value="">
                    <div><strong>Notes</strong></div>
                    <input type="text" name="notes" value="{notes}" placeholder="Optional note">
                  </form>
                </div>
              </div>
            </div>
          </div>
        </div>
        """

    @staticmethod
    def _outcome_button(scope_key: str, candidate_id: str, action: str, label: str) -> str:
        return f"""
        <form method="post" action="/review" data-action="{html.escape(action)}">
          <input type="hidden" name="scope_key" value="{html.escape(scope_key)}">
          <input type="hidden" name="candidate_id" value="{html.escape(candidate_id)}">
          <input type="hidden" name="action" value="{html.escape(action)}">
          <button type="submit">{html.escape(label)}</button>
        </form>
        """

    @staticmethod
    def _build_labeled_row(
        candidate_id: str,
        resolved: ResolvedLabel,
        resolved_from_input: str,
        stress: bool,
        notes: str,
    ) -> dict[str, Any]:
        return {
            "candidate_id": candidate_id,
            "annotation_status": "labeled",
            "truth_common_name": resolved.truth_common_name,
            "truth_sci_name": resolved.truth_sci_name,
            "truth_label": resolved.truth_label,
            "taxon_class": resolved.taxon_class,
            "resolved_from_input": resolved_from_input,
            "stress": stress,
            "reject_sample": False,
            "unsure": False,
            "not_a_bird": False,
            "bad_crop": False,
            "duplicate_sample": False,
            "notes": notes,
            "annotated_at": _utc_now(),
        }

    @staticmethod
    def _render_complete() -> str:
        return """
        <div class="wrap">
          <div class="panel full">
            <h2>Review queue is empty</h2>
            <p>No unreviewed candidates remain in the current queue.</p>
          </div>
        </div>
        """

    @staticmethod
    def _render_summary(scope: dict[str, Any], summary: dict[str, Any], next_candidate_id: str | None, topup_status: dict[str, Any]) -> str:
        overview = summary["overview"]
        outcomes = summary["outcomes"]
        species = summary["species"]
        is_run_hybrid_review = ReviewAppHandler._is_run_hybrid_review(scope)

        overview_rows = "".join(
            f"<tr><td>{html.escape(label.replace('_', ' ').title())}</td><td>{count}</td></tr>"
            for label, count in (
                [
                    ("unreviewed", overview.get("unreviewed", 0)),
                    ("in_review", overview.get("in_review", 0)),
                    ("skipped", overview.get("skipped", 0)),
                    ("reviewed", overview.get("reviewed", 0)),
                    ("labeled", outcomes.get("labeled", 0)),
                ]
                if is_run_hybrid_review
                else [
                    ("unreviewed", overview.get("unreviewed", 0)),
                    ("in_review", overview.get("in_review", 0)),
                    ("skipped", overview.get("skipped", 0)),
                    ("reviewed", overview.get("reviewed", 0)),
                    ("labeled", outcomes.get("labeled", 0)),
                    ("reject", outcomes.get("reject", 0)),
                    ("unsure", outcomes.get("unsure", 0)),
                    ("not_a_bird", outcomes.get("not_a_bird", 0)),
                    ("bad_crop", outcomes.get("bad_crop", 0)),
                    ("duplicate", outcomes.get("duplicate", 0)),
                ]
            )
        )
        species_rows = "".join(
            (
                f"""
                <tr>
                  <td>{html.escape(row['truth_common_name'] or '')}</td>
                  <td>{html.escape(row['truth_sci_name'] or '')}</td>
                  <td>{row['total_count']}</td>
                </tr>
                """
                if is_run_hybrid_review
                else f"""
                <tr>
                  <td>{html.escape(row['truth_common_name'] or '')}</td>
                  <td>{html.escape(row['truth_sci_name'] or '')}</td>
                  <td>{row['normal_count']}</td>
                  <td>{row['stress_count']}</td>
                  <td>{row['total_count']}</td>
                </tr>
                """
            )
            for row in species
        )
        continue_button = (
            f'<a href="/review?scope={_q(scope["scope_key"])}&id={html.escape(next_candidate_id)}"><button class="primary" type="button">Resume Review</button></a>'
            if next_candidate_id
            else '<button class="primary" type="button" disabled>No Images Left To Review</button>'
        )
        topup_html = ""
        if topup_status["running"]:
            topup_html = """
            <div class="resolved">
              <div><strong>Queue top-up in progress</strong></div>
              <div class="small">The app is scanning the next extraction batch. Reload this page or return to /review shortly.</div>
            </div>
            """
        return f"""
        <div class="wrap">
          <div class="panel full">
            <div class="nav">
              {continue_button}
              <a href="/scopes"><button type="button">Switch Scope</button></a>
              <a href="/summary?scope={_q(scope['scope_key'])}"><button type="button">Refresh Summary</button></a>
            </div>
            <h2>Review Summary</h2>
            <div><strong>Scope:</strong> {html.escape(scope['scope_name'])}</div>
            <div class="small">Use this page to inspect progress at any time. It is also the default destination when no unreviewed candidates remain.</div>
            {topup_html}
          </div>
          <div class="panel">
            <h3>Queue And Outcome Counts</h3>
            <table>
              <thead><tr><th>Category</th><th>Count</th></tr></thead>
              <tbody>{overview_rows}</tbody>
            </table>
          </div>
          <div class="panel">
            <h3>Species Breakdown</h3>
            <table>
              <thead>
                <tr>
                  <th>Common Name</th>
                  <th>Scientific Name</th>
                  {'<th>Total</th>' if is_run_hybrid_review else '<th>Normal</th><th>Stress</th><th>Total</th>'}
                </tr>
              </thead>
              <tbody>{species_rows or f'<tr><td colspan="{3 if is_run_hybrid_review else 5}" class="small">No labeled species yet.</td></tr>'}</tbody>
            </table>
          </div>
        </div>
        """

    @staticmethod
    def _render_top_up_pending(status: dict[str, Any], scope_key: str) -> str:
        return f"""
        <div class="wrap">
          <div class="panel full">
            <h2>Fetching Next Review Batch</h2>
            <p>The current review queue is empty. The app is scanning the next extraction batch now.</p>
            <div class="resolved">
              <div><strong>Progress</strong></div>
              <div class="small">Images scanned in current top-up: {status['last_images_scanned']}</div>
              <div class="small">Candidates created in current top-up: {status['last_candidates_created']}</div>
            </div>
            <p><a href="/summary?scope={_q(scope_key)}"><button type="button">Open Summary</button></a></p>
            <script>
              setTimeout(function () {{
                window.location = '/review?scope={_q(scope_key)}';
              }}, 1500);
            </script>
          </div>
        </div>
        """

    @staticmethod
    def _render_top_up_failed(error: str, scope_key: str) -> str:
        return (
            "<div class='wrap'><div class='panel full'>"
            "<h2>Automatic queue top-up failed</h2>"
            f"<p>{html.escape(str(error))}</p>"
            f"<p><a href=\"/summary?scope={_q(scope_key)}\"><button type=\"button\">Go To Summary</button></a></p>"
            "</div></div>"
        )

    @staticmethod
    def _render_scope_table(scopes: list[dict[str, Any]], *, aggregate: bool = False) -> str:
        rows = "".join(
            f"""
            <tr>
              <td><strong>{html.escape(scope['scope_name'])}</strong><div class="small">{html.escape(scope['catalog_path'])}</div></td>
              <td>{html.escape(scope['trip_folder'])}{f'<div class="small">{int(scope.get("member_scope_count") or 0)} child scopes</div>' if aggregate else ''}</td>
              <td>{scope.get('unreviewed_count') or 0}</td>
              <td>{scope.get('reviewed_count') or 0}</td>
              <td>{scope.get('candidate_count') or 0}</td>
              <td><a href="/review?scope={_q(scope['scope_key'])}"><button type="button">Open</button></a></td>
            </tr>
            """
            for scope in scopes
        )
        return rows

    @classmethod
    def _render_scopes(
        cls,
        scopes: list[dict[str, Any]],
        aggregate_scopes: list[dict[str, Any]],
        topup_status: dict[str, Any],
    ) -> str:
        aggregate_rows = cls._render_scope_table(aggregate_scopes, aggregate=True)
        rows = cls._render_scope_table(scopes)
        status = ""
        if topup_status["running"]:
            status = "<div class='resolved'><div><strong>Background extraction running</strong></div></div>"
        return f"""
        <div class="wrap">
          <div class="panel full">
            <h2>Review Scopes</h2>
            <div class="small">Switch between persisted review scopes without restarting the app. Aggregate scopes combine sibling subfolders into one review queue.</div>
            {status}
          </div>
          <div class="panel full">
            <h3>Aggregate Scopes</h3>
            <table>
              <thead><tr><th>Scope</th><th>Parent Folder</th><th>Unreviewed</th><th>Reviewed</th><th>Candidates</th><th></th></tr></thead>
              <tbody>{aggregate_rows or '<tr><td colspan="6" class="small">No aggregate parent scopes available yet.</td></tr>'}</tbody>
            </table>
          </div>
          <div class="panel full">
            <h3>Persisted Scopes</h3>
            <table>
              <thead><tr><th>Scope</th><th>Trip Folder</th><th>Unreviewed</th><th>Reviewed</th><th>Candidates</th><th></th></tr></thead>
              <tbody>{rows or '<tr><td colspan="6" class="small">No scopes yet. If automatic top-up is configured, wait for the first batch to populate.</td></tr>'}</tbody>
            </table>
          </div>
        </div>
        """

    def _write_html(self, title: str, body: str) -> None:
        payload = _html_page(title, body)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _build_suggestion(
        self,
        candidate: dict[str, Any],
        image: dict[str, Any],
        scope: dict[str, Any] | None,
    ) -> SuggestedLabel | None:
        seeded = self.store.get_seed_suggestion(candidate["id"])
        if seeded is not None and seeded.get("best_truth_label"):
            return SuggestedLabel(
                truth_common_name=seeded.get("best_common_name") or "",
                truth_sci_name=seeded.get("best_sci_name") or "",
                truth_label=seeded["best_truth_label"],
                confidence=float(seeded.get("best_confidence") or 0.0),
            )
        if self.suggester is None:
            return None
        try:
            return self.suggester.suggest(
                image["source_image_path"],
                (
                    candidate["bbox_x1"],
                    candidate["bbox_y1"],
                    candidate["bbox_x2"],
                    candidate["bbox_y2"],
                ),
                catalog_path=scope.get("catalog_path") if scope else None,
            )
        except Exception:
            return None

    def _build_ebird_reference(
        self,
        *,
        annotation: dict[str, Any] | None,
        suggestion: SuggestedLabel | None,
        selected_truth_label: str,
    ):
        truth_common_name: str | None = None
        truth_sci_name: str | None = None

        if selected_truth_label and self.resolver is not None:
            try:
                resolved = self.resolver.resolve_recent_label(selected_truth_label)
            except UnknownLabelError:
                resolved = None
            if resolved is not None:
                truth_common_name = resolved.truth_common_name
                truth_sci_name = resolved.truth_sci_name

        if truth_common_name is None and annotation and annotation.get("annotation_status") == "labeled":
            truth_common_name = annotation.get("truth_common_name")
            truth_sci_name = annotation.get("truth_sci_name")

        if truth_common_name is None and suggestion is not None:
            truth_common_name = suggestion.truth_common_name
            truth_sci_name = suggestion.truth_sci_name

        if not truth_common_name or not truth_sci_name:
            return None

        try:
            return resolve_reference(
                truth_common_name=truth_common_name,
                truth_sci_name=truth_sci_name,
            )
        except Exception:
            return None

    @staticmethod
    def _stress_reason(resolved_truth_label: str, suggestion: SuggestedLabel | None) -> str | None:
        if suggestion is None:
            return None
        if resolved_truth_label != suggestion.truth_label:
            return (
                f"Label differs from classifier suggestion "
                f"({suggestion.truth_common_name}, {suggestion.confidence:.1%})."
            )
        if suggestion.confidence < STRESS_SUGGESTION_CONFIDENCE_THRESHOLD:
            return f"Classifier confidence is low ({suggestion.confidence:.1%})."
        return None

    def _redirect_review(
        self,
        candidate_id: str,
        *,
        scope_key: str,
        error: str = "",
        selected_truth_label: str = "",
        label_input: str = "",
        stress: bool = False,
        notes: str = "",
        stress_reason: str = "",
    ) -> None:
        from urllib.parse import urlencode

        params = {
            "scope": scope_key,
            "id": candidate_id,
            "error": error,
            "selected_truth_label": selected_truth_label,
            "label_input": label_input,
            "stress": "1" if stress else "0",
            "notes": notes,
            "stress_reason": stress_reason,
        }
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/review?{urlencode(params)}")
        self.end_headers()

    def _resolve_scope(self, scope_key: str | None) -> dict[str, Any] | None:
        scopes = self.store.list_scopes()
        if not scopes:
            return None
        if scope_key:
            aggregate_payload = _parse_aggregate_scope_key(scope_key)
            if aggregate_payload is not None:
                return self._aggregate_scope_from_members(
                    scopes,
                    catalog_path=aggregate_payload["catalog_path"],
                    trip_folder=aggregate_payload["trip_folder"],
                )
            return self.store.get_scope(scope_key)
        if len(scopes) == 1:
            return scopes[0]
        return None

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Keep the server quiet unless there is an actual failure.
        return


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _q(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the local annotation review UI.")
    p.add_argument("--db", required=True, help="Path to the review SQLite database")
    p.add_argument(
        "--labels-file",
        required=True,
        help="Path to a text file or JSONL manifest containing canonical labels",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--catalog", help="Optional Lightroom catalog path for automatic queue top-up")
    p.add_argument("--detector", help="Import path for a detector class or factory")
    p.add_argument("--detector-model", default=None, help="Optional detector model or weights path passed as model=...")
    p.add_argument("--preview-dir", help="Directory for generated preview JPEGs")
    p.add_argument("--batch-limit", type=int, default=100, help="Number of source images to scan per automatic top-up")
    p.add_argument("--folder", default=None, help="Optional folder filter for automatic top-up")
    p.add_argument("--scope-folder", default=None, help="Optional scope folder override used for review-scope naming")
    p.add_argument("--min-stars", type=int, default=None, help="Optional minimum Lightroom rating for automatic top-up")
    p.add_argument(
        "--formats",
        default="RAW,DNG,JPEG,JPG,TIFF,PSD",
        help="Comma-separated Lightroom file formats to scan during automatic top-up; accepts arbitrary Lightroom fileFormat values",
    )
    p.add_argument("--max-preview-dimension", type=int, default=2048)
    p.add_argument("--jpeg-quality", type=int, default=85)
    p.add_argument(
        "--topup-low-watermark",
        type=int,
        default=20,
        help="Start background queue refill when unreviewed candidates fall to this count or below",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    if args.verbose:
        for logger_name in (
            __name__,
            "src.catalog_extract",
            "src.review_store",
            "src.review_app",
        ):
            logging.getLogger(logger_name).setLevel(logging.DEBUG)
        for logger_name in (
            "PIL",
            "PIL.TiffImagePlugin",
            "exifread",
            "ultralytics",
        ):
            logging.getLogger(logger_name).setLevel(logging.WARNING)
    labels = load_label_inventory(args.labels_file)
    if not labels:
        raise SystemExit("No labels loaded from labels file")

    store = ReviewStore(args.db)
    resolver = LabelResolver(labels)
    try:
        suggester: ReviewSuggester | None = ReviewSuggester()
        suggestion_status: str | None = None
    except Exception as exc:
        suggester = None
        suggestion_status = f"Classifier stack is not available in this environment: {exc}"

    topup_runner: QueueTopUpRunner | None = None
    topup_coordinator = QueueTopUpCoordinator()
    if args.catalog or args.detector or args.preview_dir:
        if not (args.catalog and args.detector and args.preview_dir):
            raise SystemExit("--catalog, --detector, and --preview-dir are all required for automatic queue top-up")
        formats = {part.strip().upper() for part in args.formats.split(",") if part.strip()}
        topup_runner = QueueTopUpRunner(
            catalog=args.catalog,
            detector_import_path=args.detector,
            detector_model=args.detector_model,
            preview_dir=args.preview_dir,
            formats=formats,
            folder=args.folder,
            scope_folder=args.scope_folder,
            min_stars=args.min_stars,
            batch_limit=args.batch_limit,
            max_preview_dimension=args.max_preview_dimension,
            jpeg_quality=args.jpeg_quality,
            verbose=args.verbose,
        )

    class Handler(ReviewAppHandler):
        pass

    Handler.store = store
    Handler.resolver = resolver
    Handler.suggester = suggester
    Handler.suggestion_status = suggestion_status
    Handler.topup_runner = topup_runner
    Handler.topup_coordinator = topup_coordinator
    Handler.topup_low_watermark = args.topup_low_watermark

    if topup_runner is not None:
        scopes = store.list_scopes()
        total_unreviewed = store.count_candidates(review_status="unreviewed")
        only_legacy_scopes = bool(scopes) and all(scope["scope_key"] == "__legacy__" for scope in scopes)
        needs_discovery = topup_runner.needs_scope_discovery(scopes)
        if (
            total_unreviewed <= args.topup_low_watermark
            or only_legacy_scopes
            or not scopes
            or needs_discovery
        ):
            log.info(
                "Startup triggered automatic discovery: total_unreviewed=%s watermark=%s only_legacy=%s needs_discovery=%s",
                total_unreviewed,
                args.topup_low_watermark,
                only_legacy_scopes,
                needs_discovery,
            )
            topup_coordinator.ensure_running(store, topup_runner, None)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        print(f"Review UI: http://{args.host}:{args.port}/review")
        server.serve_forever()
    finally:
        store.close()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
