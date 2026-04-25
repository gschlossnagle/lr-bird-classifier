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
from .extract_candidates import _load_object, _scope_key
from .label_resolver import AmbiguousLabelError, LabelResolver, ResolvedLabel, UnknownLabelError
from .review_queue import QueueFilters, ReviewQueue
from .review_assets import BoxedPreviewProvider
from .review_suggester import ReviewSuggester, SuggestedLabel
from .review_store import ReviewStore

STRESS_SUGGESTION_CONFIDENCE_THRESHOLD = 0.35
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
        min_stars: int | None,
        batch_limit: int,
        max_preview_dimension: int,
        jpeg_quality: int,
    ) -> None:
        detector_factory = _load_object(detector_import_path)
        detector = detector_factory()
        provider = BoxedPreviewProvider(
            preview_dir,
            max_dimension=max_preview_dimension,
            jpeg_quality=jpeg_quality,
        )
        self.extractor = CatalogExtractor(None, detector, provider)  # store injected per run
        self.catalog = catalog
        self.formats = formats
        self.folder = folder
        self.min_stars = min_stars
        self.batch_limit = batch_limit
        self.scope_key = _scope_key(
            catalog=catalog,
            folder=folder,
            min_stars=min_stars,
            formats=formats,
        )

    def top_up(self, store: ReviewStore) -> tuple[int, int]:
        from datetime import UTC, datetime

        created_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        start_after_id: int | None = None
        cursor = store.get_extraction_cursor(self.scope_key)
        if cursor is not None:
            start_after_id = int(cursor)

        self.extractor.store = store
        images_scanned, candidates_created, last_image_id = self.extractor.extract(
            self.catalog,
            formats=self.formats,
            folder_filter=self.folder,
            min_rating=self.min_stars,
            start_after_id=start_after_id,
            limit=self.batch_limit,
            created_at=created_at,
        )
        if last_image_id is not None:
            store.set_extraction_cursor(self.scope_key, str(last_image_id), created_at)
        return images_scanned, candidates_created


class QueueTopUpError(RuntimeError):
    pass


class QueueTopUpCoordinator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._last_error: str | None = None
        self._last_images_scanned = 0
        self._last_candidates_created = 0

    def ensure_running(self, store: ReviewStore, runner: QueueTopUpRunner) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = None
            self._last_images_scanned = 0
            self._last_candidates_created = 0

        thread = threading.Thread(
            target=self._run,
            args=(store, runner),
            daemon=True,
            name="review-topup",
        )
        thread.start()
        return True

    def _run(self, store: ReviewStore, runner: QueueTopUpRunner) -> None:
        images_total = 0
        candidates_total = 0
        try:
            while True:
                images_scanned, candidates_created = runner.top_up(store)
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
            return

        with self._lock:
            self._running = False
            self._last_error = None
            self._last_images_scanned = images_total
            self._last_candidates_created = candidates_total

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "last_error": self._last_error,
                "last_images_scanned": self._last_images_scanned,
                "last_candidates_created": self._last_candidates_created,
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


def _html_page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; background: #f4f1ea; color: #1b1b1b; }}
    .wrap {{ display: grid; grid-template-columns: 1.7fr 1fr; gap: 20px; padding: 20px; }}
    .panel {{ background: #fffdf9; border: 1px solid #ddd4c5; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    .preview-panel {{ display: flex; flex-direction: column; }}
    .preview-frame {{ display: flex; justify-content: center; align-items: flex-start; min-height: 0; }}
    img {{ max-width: 100%; max-height: 82vh; width: auto; height: auto; display: block; border-radius: 8px; }}
    .meta {{ font-size: 14px; line-height: 1.5; }}
    .meta code {{ font-size: 12px; }}
    .error {{ color: #8b1e1e; font-weight: 600; }}
    .recent form, .actions form {{ display: inline-block; margin: 4px 4px 0 0; }}
    button {{ border: 1px solid #aa9f8f; background: #f7f2ea; border-radius: 8px; padding: 8px 12px; cursor: pointer; }}
    button.primary {{ background: #184e3b; color: white; border-color: #184e3b; }}
    input[type=text] {{ width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #c9bfaf; box-sizing: border-box; }}
    label.toggle {{ display: inline-flex; align-items: center; gap: 8px; margin: 10px 0; }}
    .small {{ font-size: 12px; color: #5e564a; }}
    .resolved {{ background: #f7f2ea; border-radius: 8px; padding: 10px; margin-top: 10px; }}
    .full {{ grid-column: 1 / -1; }}
    .nav {{ display: flex; gap: 10px; margin-bottom: 14px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #ddd4c5; vertical-align: top; }}
    th {{ font-size: 13px; color: #5e564a; }}
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
    const form = document.querySelector('form[data-recent-index="1"]');
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/review"}:
            self._handle_review_get(parsed)
            return
        if parsed.path == "/summary":
            self._handle_summary_get()
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
        self._maybe_start_background_top_up()
        params = parse_qs(parsed.query)
        candidate_id = params.get("id", [None])[0]
        error = params.get("error", [""])[0]
        prefill_selected_truth_label = params.get("selected_truth_label", [""])[0]
        prefill_label_input = params.get("label_input", [""])[0]
        prefill_stress = params.get("stress", ["0"])[0] == "1"
        prefill_notes = params.get("notes", [""])[0]
        stress_reason = params.get("stress_reason", [""])[0]

        queue = ReviewQueue(self.store, QueueFilters(review_status="unreviewed"))
        if candidate_id is None:
            next_candidate = queue.next_candidate()
            if next_candidate is None:
                status = self._top_up_status()
                if self.topup_runner is not None and self.topup_coordinator is not None:
                    if not status["running"]:
                        self.topup_coordinator.ensure_running(self.store, self.topup_runner)
                        status = self._top_up_status()
                    if status["running"]:
                        self._write_html("Queue Top-Up", self._render_top_up_pending(status))
                        return
                    next_candidate = queue.next_candidate()
                if status["last_error"]:
                    self._write_html("Top-Up Failed", self._render_top_up_failed(status["last_error"]))
                    return
                if next_candidate is None:
                    self.send_response(HTTPStatus.SEE_OTHER)
                    self.send_header("Location", "/summary")
                    self.end_headers()
                    return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/review?id={next_candidate['id']}")
            self.end_headers()
            return

        candidate = queue.open_candidate(candidate_id)
        if candidate is None:
            self._write_html("Not Found", "<div class='panel'>Candidate not found.</div>")
            return

        image = self.store.get_image(candidate["image_id"]) or {}
        annotation = self.store.get_annotation(candidate_id)
        recent = self.store.recent_labels(limit=5)
        prev_candidate = self.store.previous_reviewed_candidate(candidate_id)
        burst_targets = self.store.burst_candidates(candidate_id, include_reviewed=False)
        burst_position = self.store.burst_position(candidate_id)
        queue_position = self.store.queue_position(candidate_id, review_status="unreviewed")
        unreviewed_candidates = self.store.count_candidates(review_status="unreviewed")
        unreviewed_images = self.store.count_candidate_images(review_status="unreviewed")
        suggestion = self._build_suggestion(candidate, image)

        body = self._render_candidate(
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
            self.suggestion_status,
            prefill_selected_truth_label,
            prefill_label_input,
            prefill_stress,
            prefill_notes,
            stress_reason,
        )
        self._write_html("Review Candidate", body)

    def _handle_summary_get(self) -> None:
        self._maybe_start_background_top_up()
        queue = ReviewQueue(self.store, QueueFilters(review_status="unreviewed"))
        next_candidate = queue.next_candidate()
        summary = self.store.summary_counts()
        body = self._render_summary(summary, next_candidate["id"] if next_candidate else None, self._top_up_status())
        self._write_html("Review Summary", body)

    def _top_up_status(self) -> dict[str, Any]:
        if self.topup_coordinator is None:
            return {
                "running": False,
                "last_error": None,
                "last_images_scanned": 0,
                "last_candidates_created": 0,
            }
        return self.topup_coordinator.snapshot()

    def _maybe_start_background_top_up(self) -> None:
        if self.topup_runner is None or self.topup_coordinator is None:
            return
        unreviewed = self.store.count_candidates(review_status="unreviewed")
        if unreviewed <= self.topup_low_watermark:
            self.topup_coordinator.ensure_running(self.store, self.topup_runner)

    def _handle_review_post(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = parse_qs(self.rfile.read(length).decode("utf-8"))
        candidate_id = payload.get("candidate_id", [""])[0]
        action = payload.get("action", [""])[0]
        label_input = payload.get("label_input", [""])[0]
        selected_truth_label = payload.get("selected_truth_label", [""])[0]
        stress = payload.get("stress", ["0"])[0] == "1"
        notes = payload.get("notes", [""])[0]

        if not candidate_id:
            self.send_error(HTTPStatus.BAD_REQUEST, "candidate_id required")
            return

        try:
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
                suggestion = self._build_suggestion(candidate, image)
                stress_reason = self._stress_reason(resolved.truth_label, suggestion)
                if stress_reason and not stress:
                    stress = True
                row = self._build_labeled_row(candidate_id, resolved, label_input or selected_truth_label, stress, notes)
                self.store.upsert_annotation(row)
            elif action == "save_burst":
                resolved = self._resolve_label(selected_truth_label, label_input)
                candidate = self.store.get_candidate(candidate_id) or {}
                image = self.store.get_image(candidate.get("image_id")) or {}
                suggestion = self._build_suggestion(candidate, image)
                stress_reason = self._stress_reason(resolved.truth_label, suggestion)
                if stress_reason and not stress:
                    stress = True
                row = self._build_labeled_row(candidate_id, resolved, label_input or selected_truth_label, stress, notes)
                self.store.upsert_annotation(row)
                self.store.apply_annotation_to_burst(candidate_id, row)
            else:
                raise ValueError(f"Unsupported action '{action}'")
        except (UnknownLabelError, AmbiguousLabelError, ValueError) as e:
            self._redirect_review(
                candidate_id,
                error=str(e),
                selected_truth_label=selected_truth_label,
                label_input=label_input,
                stress=stress,
                notes=notes,
            )
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/review")
        self.end_headers()

    def _resolve_label(self, selected_truth_label: str, label_input: str) -> ResolvedLabel:
        if selected_truth_label:
            return self.resolver.resolve_recent_label(selected_truth_label)
        return self.resolver.resolve_common_name(label_input)

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
        suggestion_status: str | None,
        prefill_selected_truth_label: str,
        prefill_label_input: str,
        prefill_stress: bool,
        prefill_notes: str,
        stress_reason: str,
    ) -> str:
        default_save_action = "save_burst" if burst_target_count else "save"
        default_save_label = "Save + Apply To Burst" if burst_target_count else "Save Label"
        burst_label = ""
        if burst_position is not None:
            burst_label = f" ({burst_position[0]}/{burst_position[1]})"
        recent_forms = []
        for idx, row in enumerate(recent, start=1):
            recent_forms.append(
                f"""
                <form method="post" action="/review" data-recent-index="{idx}" data-truth-label="{html.escape(row['truth_label'])}">
                  <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                  <input type="hidden" name="action" value="{default_save_action}">
                  <input type="hidden" name="selected_truth_label" value="{html.escape(row['truth_label'])}">
                  <input type="hidden" name="stress" value="0">
                  <button type="submit">{idx}. {html.escape(row['truth_common_name'])}</button>
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
                f"{' [stress]' if annotation['stress'] else ''}</div>"
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
                <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                <input type="hidden" name="action" value="{default_save_action}">
                <input type="hidden" name="selected_truth_label" value="{html.escape(suggestion.truth_label)}">
                <input type="hidden" name="stress" value="0">
                <button type="submit">0. Accept Suggestion</button>
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
        if stress_reason:
            stress_reason_html = f"""
            <div class="resolved">
              <div><strong>Stress suggested</strong></div>
              <div class="small">{html.escape(stress_reason)}</div>
            </div>
            """

        return f"""
        <div class="wrap">
          <div class="panel preview-panel">
            <div class="nav">
              <a href="/summary"><button type="button">Summary</button></a>
            </div>
            <div class="preview-frame">
              <img src="/preview/{html.escape(candidate['id'])}" alt="preview">
            </div>
          </div>
          <div class="panel">
            <div class="nav">
              <a href="/summary"><button type="button">Summary</button></a>
            </div>
            <div class="meta">
              <div><strong>Candidate:</strong> <code>{html.escape(candidate['id'])}</code></div>
              <div><strong>Source:</strong> <code>{html.escape(str(image.get('source_image_path', '')))}</code></div>
              <div><strong>Captured:</strong> {html.escape(str(image.get('capture_datetime', '')))}</div>
              <div><strong>Detector:</strong> {html.escape(str(candidate.get('detector_name', '')))} @ {candidate.get('detector_confidence', 0):.2f}</div>
              <div><strong>Region:</strong> {html.escape(str(image.get('region_hint', '')))}</div>
              <div><strong>Burst:</strong> {html.escape(str(image.get('burst_group_id', '')))}{html.escape(burst_label)}</div>
              <div><strong>Queue Position:</strong> {html.escape(f'{queue_position[0]}/{queue_position[1]}' if queue_position else 'n/a')}</div>
              <div><strong>Queue Depth:</strong> {unreviewed_images} image(s), {unreviewed_candidates} candidate(s) yolo'd and waiting</div>
              <div class="small">{'This candidate has ' + str(burst_target_count) + ' other safe burst item(s), so label actions default to burst apply.' if burst_target_count else 'No safe burst targets available. Burst apply only includes other unreviewed, single-detection candidates in the same burst group.'}</div>
            </div>
            {error_html}
            {resolved_preview}
            {selected_preview}
            {suggestion_html}
            {stress_reason_html}
            <div style="margin: 10px 0 16px 0;">
              {f'<a data-nav="prev" href="/review?id={html.escape(prev_candidate["id"])}"><button type="button">Previous Reviewed Candidate</button></a>' if prev_candidate else '<button type="button" disabled>Previous Reviewed Candidate</button>'}
            </div>
            <form method="post" action="/review" data-role="review-form">
              <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
              <input type="hidden" name="action" value="{default_save_action}">
              <input id="selected-truth-label" type="hidden" name="selected_truth_label" value="{html.escape(prefill_selected_truth_label)}">
              <input type="text" name="label_input" placeholder="Type common name" value="{html.escape(prefill_label_input)}">
              <label class="toggle"><input id="stress-toggle" type="checkbox" name="stress" value="1" {'checked' if prefill_stress else ''}> mark as stress</label>
              <div class="small">Type a common name or use a recent label button below. Shortcuts: 0, 1-5, R, T, K, P, F. Label accepts default to burst apply when eligible.</div>
              <div style="margin-top:10px">
                <button class="primary" type="submit">{default_save_label}</button>
                {f'<button type="submit" formaction="/review" formmethod="post" name="action" value="save">Save Current Only</button>' if burst_target_count else ''}
              </div>
            </form>
            <div class="recent" style="margin-top:14px">
              <div><strong>Recent labels</strong></div>
              {''.join(recent_forms) or '<div class="small">No recent labels yet.</div>'}
            </div>
            <div class="actions" style="margin-top:16px">
              <div><strong>Other outcomes</strong></div>
              {self._outcome_button(candidate['id'], 'reject', 'Reject')}
              {self._outcome_button(candidate['id'], 'unsure', 'Unsure')}
              {self._outcome_button(candidate['id'], 'not_a_bird', 'Not a Bird')}
              {self._outcome_button(candidate['id'], 'bad_crop', 'Bad Crop')}
              {self._outcome_button(candidate['id'], 'duplicate', 'Duplicate')}
              {self._outcome_button(candidate['id'], 'skip', 'Skip')}
            </div>
            <form method="post" action="/review" style="margin-top:16px">
              <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
              <input type="hidden" name="action" value="save">
              <input type="hidden" name="selected_truth_label" value="">
              <div><strong>Notes</strong></div>
              <input type="text" name="notes" value="{notes}" placeholder="Optional note">
            </form>
          </div>
        </div>
        """

    @staticmethod
    def _outcome_button(candidate_id: str, action: str, label: str) -> str:
        return f"""
        <form method="post" action="/review" data-action="{html.escape(action)}">
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
    def _render_summary(summary: dict[str, Any], next_candidate_id: str | None, topup_status: dict[str, Any]) -> str:
        overview = summary["overview"]
        outcomes = summary["outcomes"]
        species = summary["species"]

        overview_rows = "".join(
            f"<tr><td>{html.escape(label.replace('_', ' ').title())}</td><td>{count}</td></tr>"
            for label, count in [
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
        species_rows = "".join(
            f"""
            <tr>
              <td>{html.escape(row['truth_common_name'] or '')}</td>
              <td>{html.escape(row['truth_sci_name'] or '')}</td>
              <td>{row['normal_count']}</td>
              <td>{row['stress_count']}</td>
              <td>{row['total_count']}</td>
            </tr>
            """
            for row in species
        )
        continue_button = (
            f'<a href="/review?id={html.escape(next_candidate_id)}"><button class="primary" type="button">Resume Review</button></a>'
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
              <a href="/summary"><button type="button">Refresh Summary</button></a>
            </div>
            <h2>Review Summary</h2>
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
                  <th>Normal</th>
                  <th>Stress</th>
                  <th>Total</th>
                </tr>
              </thead>
              <tbody>{species_rows or '<tr><td colspan="5" class="small">No labeled species yet.</td></tr>'}</tbody>
            </table>
          </div>
        </div>
        """

    @staticmethod
    def _render_top_up_pending(status: dict[str, Any]) -> str:
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
            <p><a href="/summary"><button type="button">Open Summary</button></a></p>
            <script>
              setTimeout(function () {{
                window.location = '/review';
              }}, 1500);
            </script>
          </div>
        </div>
        """

    @staticmethod
    def _render_top_up_failed(error: str) -> str:
        return (
            "<div class='wrap'><div class='panel full'>"
            "<h2>Automatic queue top-up failed</h2>"
            f"<p>{html.escape(str(error))}</p>"
            "<p><a href=\"/summary\"><button type=\"button\">Go To Summary</button></a></p>"
            "</div></div>"
        )

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
    ) -> SuggestedLabel | None:
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
        error: str = "",
        selected_truth_label: str = "",
        label_input: str = "",
        stress: bool = False,
        notes: str = "",
        stress_reason: str = "",
    ) -> None:
        from urllib.parse import urlencode

        params = {
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

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Keep the server quiet unless there is an actual failure.
        return


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    p.add_argument("--catalog", help="Optional Lightroom catalog path for automatic queue top-up")
    p.add_argument("--detector", help="Import path for a detector class or factory")
    p.add_argument("--preview-dir", help="Directory for generated preview JPEGs")
    p.add_argument("--batch-limit", type=int, default=100, help="Number of source images to scan per automatic top-up")
    p.add_argument("--folder", default=None, help="Optional folder filter for automatic top-up")
    p.add_argument("--min-stars", type=int, default=None, help="Optional minimum Lightroom rating for automatic top-up")
    p.add_argument(
        "--formats",
        default="RAW,DNG,JPEG,TIFF",
        help="Comma-separated Lightroom file formats to scan during automatic top-up",
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
    )
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
            preview_dir=args.preview_dir,
            formats=formats,
            folder=args.folder,
            min_stars=args.min_stars,
            batch_limit=args.batch_limit,
            max_preview_dimension=args.max_preview_dimension,
            jpeg_quality=args.jpeg_quality,
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

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        print(f"Review UI: http://{args.host}:{args.port}/review")
        server.serve_forever()
    finally:
        store.close()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
