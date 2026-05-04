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
from time import monotonic
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
from .subject_size_estimate import estimated_subject_box_size_for_candidate, load_subject_size_metadata

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
    :root {{
      --bg: #0E0F11;
      --panel: #16181B;
      --panel2: #1C1F23;
      --rule: #26292E;
      --rule2: #1F2226;
      --ink: #F2EFE9;
      --ink2: #C9C4B9;
      --ink3: #8A857A;
      --ink4: #5C5852;
      --accent: #F0B45A;
      --accent-soft: rgba(240, 180, 90, 0.16);
      --accent-ink: #F7D7A3;
      --danger: #C76464;
      --danger-ink: #E6A2A2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, -apple-system, BlinkMacSystemFont, sans-serif;
    }}
    .page {{ min-height: 100vh; padding: 0; }}
    .app-shell {{ min-height: 100vh; display: flex; flex-direction: column; }}
    .wrap {{ flex: 1; min-height: 0; display: grid; grid-template-columns: 84px minmax(0, 1fr); }}
    .topbar {{
      height: 42px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 8px 16px;
      background: var(--panel);
      border-bottom: 1px solid var(--rule);
    }}
    .topbar-left, .topbar-right {{ display: flex; align-items: center; gap: 14px; min-width: 0; }}
    .topbar h1 {{ margin: 0; font-size: 14px; font-weight: 600; letter-spacing: -0.2px; }}
    .divider {{ width: 1px; height: 14px; background: var(--rule); flex: 0 0 auto; }}
    .topbar-stanza {{ min-width: 0; }}
    .topbar-value {{
      font-size: 13px;
      color: var(--ink2);
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .mono-label {{
      font-family: "SFMono-Regular", ui-monospace, monospace;
      font-size: 10px;
      letter-spacing: 0.7px;
      text-transform: uppercase;
      color: var(--ink3);
    }}
    .mono-small {{
      font-family: "SFMono-Regular", ui-monospace, monospace;
      font-size: 11px;
      color: var(--ink3);
    }}
    .progress-numbers {{
      font-size: 12px;
      color: var(--ink2);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .progress-numbers strong {{ color: var(--ink); font-weight: 600; }}
    .progress-track {{ width: 160px; height: 4px; background: var(--rule); border-radius: 99px; overflow: hidden; }}
    .progress-fill {{ height: 100%; background: var(--accent); }}
    .topbar-actions {{ display: flex; align-items: center; gap: 8px; }}
    .btn, button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
      padding: 5px 10px;
      border: 1px solid var(--rule);
      border-radius: 7px;
      background: transparent;
      color: var(--ink2);
      font-size: 12.5px;
      cursor: pointer;
      white-space: nowrap;
    }}
    .btn:hover, button:hover {{ border-color: #373c43; color: var(--ink); }}
    .btn-accent, button.primary {{
      background: var(--accent);
      color: #1A1408;
      border-color: var(--accent);
      font-weight: 600;
    }}
    .btn-danger {{ color: var(--danger-ink); border-color: rgba(199,100,100,0.45); }}
    .keycap {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 18px;
      height: 18px;
      padding: 0 5px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(255,255,255,0.06);
      color: var(--ink2);
      font-family: "SFMono-Regular", ui-monospace, monospace;
      font-size: 10.5px;
      font-weight: 500;
      letter-spacing: 0.3px;
    }}
    .keycap-accent {{ background: var(--accent-soft); border-color: rgba(240,180,90,0.5); color: var(--accent-ink); }}
    .keycap-danger {{ background: rgba(199,100,100,0.12); border-color: rgba(199,100,100,0.45); color: var(--danger-ink); }}
    .rail {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      padding: 12px 8px;
      background: var(--panel);
      border-right: 1px solid var(--rule);
      min-height: 0;
    }}
    .rail-spacer {{ height: 8px; }}
    .rail-fill {{ flex: 1; }}
    .rail-tile {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 4px;
      width: 100%;
      padding: 8px 4px;
      border: 1px solid var(--rule);
      border-radius: 8px;
      background: transparent;
      color: var(--ink2);
      text-align: center;
    }}
    .rail-tile.accept {{ background: var(--accent-soft); border-color: rgba(240,180,90,0.45); color: var(--accent-ink); }}
    .rail-tile.danger {{ color: var(--danger-ink); }}
    .rail-tile-label {{ font-size: 11px; line-height: 1.15; }}
    .review-body {{
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 2.1fr) 320px;
      gap: 12px;
      padding: 12px;
      align-items: stretch;
    }}
    .photo-column, .inspector-column {{ min-height: 0; display: flex; flex-direction: column; }}
    .photo-column {{ gap: 8px; }}
    .inspector-column {{ gap: 8px; }}
    .stage-header {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
    .stage-header-controls {{ display: flex; gap: 6px; }}
    .photo-frame {{
      flex: 1;
      min-height: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #000;
      border: 1px solid var(--rule);
      overflow: hidden;
    }}
    img {{ max-width: 100%; display: block; }}
    .candidate-preview {{ width: 100%; height: 100%; object-fit: contain; max-height: calc(100vh - 305px); }}
    .classifier-banner {{
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 10px 12px;
      background: var(--panel);
      border: 1px solid var(--rule);
      border-left: 2px solid var(--accent);
      border-radius: 8px;
    }}
    .classifier-status {{ display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }}
    .status-dot {{
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 0 3px rgba(240,180,90,0.18);
    }}
    .classifier-main {{ flex: 1; min-width: 0; }}
    .classifier-name {{ font-size: 17px; font-weight: 600; letter-spacing: -0.2px; color: var(--ink); }}
    .classifier-sci {{ margin-top: 2px; font-size: 13px; color: var(--ink3); font-style: italic; }}
    .classifier-fallback {{ font-size: 13px; color: var(--ink2); }}
    .classifier-meta {{ margin-top: 4px; display: flex; align-items: center; gap: 8px; }}
    .classifier-confidence {{ max-width: 280px; flex: 1; height: 3px; background: var(--rule); border-radius: 99px; overflow: hidden; }}
    .classifier-confidence-fill {{ height: 100%; background: var(--accent); }}
    .classifier-confidence-value {{
      font-family: "SFMono-Regular", ui-monospace, monospace;
      font-size: 11.5px;
      color: var(--accent-ink);
      font-weight: 500;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--rule);
      border-radius: 8px;
      padding: 8px;
    }}
    .full {{ grid-column: 1 / -1; }}
    .inspector-card {{ display: flex; flex-direction: column; gap: 8px; }}
    .inspector-header {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .reference-thumb {{
      width: 100%;
      height: 180px;
      border: 1px solid var(--rule);
      border-radius: 4px;
      overflow: hidden;
      background: linear-gradient(135deg, rgba(255,255,255,0.03) 25%, transparent 25%, transparent 50%, rgba(255,255,255,0.03) 50%, rgba(255,255,255,0.03) 75%, transparent 75%, transparent);
      background-size: 12px 12px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .reference-thumb img {{ width: 100%; height: 100%; object-fit: cover; }}
    .reference-actions {{ display: flex; gap: 6px; }}
    .reference-actions > * {{ flex: 1; }}
    .details-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }}
    .detail-item {{ display: grid; gap: 2px; min-width: 0; }}
    .detail-value {{
      font-family: "SFMono-Regular", ui-monospace, monospace;
      font-size: 11.5px;
      color: var(--ink);
      font-variant-numeric: tabular-nums;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .details-divider {{ border-top: 1px solid var(--rule); padding-top: 8px; margin-top: 4px; }}
    .details-row {{ display: flex; justify-content: space-between; gap: 10px; font-family: "SFMono-Regular", ui-monospace, monospace; font-size: 11px; color: var(--ink3); }}
    .details-row span:last-child {{ text-align: right; }}
    .path-text {{ font-family: "SFMono-Regular", ui-monospace, monospace; font-size: 10.5px; color: var(--ink3); word-break: break-all; }}
    .copyable-path {{ cursor: pointer; transition: color 120ms ease; }}
    .copyable-path:hover {{ color: var(--ink); }}
    .note-input, .manual-label-input {{
      width: 100%;
      padding: 8px 10px;
      border-radius: 6px;
      border: 1px solid var(--rule);
      background: var(--panel2);
      color: var(--ink);
      font-size: 13px;
      outline: none;
    }}
    .note-input::placeholder, .manual-label-input::placeholder {{ color: var(--ink4); }}
    .bottom-bar {{
      height: 56px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      background: var(--panel);
      border-top: 1px solid var(--rule);
      flex-wrap: nowrap;
    }}
    .manual-label-wrap {{ width: 220px; flex: 0 0 220px; }}
    .recent-strip {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }}
    .recent-btn {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 5px 10px;
      border-radius: 7px;
      border: 1px solid var(--rule);
      background: transparent;
      color: var(--ink2);
      font-size: 12.5px;
      cursor: pointer;
      max-width: 220px;
    }}
    .recent-btn .label-text {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .stress-chip {{
      margin-left: auto;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 5px 10px 5px 8px;
      border: 1px solid var(--rule);
      border-radius: 6px;
      color: var(--ink2);
      white-space: nowrap;
    }}
    .stress-chip input {{ margin: 0; accent-color: var(--accent); }}
    .small {{ font-size: 12px; color: var(--ink3); }}
    .resolved, .error {{
      padding: 10px 12px;
      border-radius: 8px;
      border: 1px solid var(--rule);
      background: var(--panel2);
    }}
    .error {{ color: #f0b1b1; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
    th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--rule); vertical-align: top; }}
    th {{ font-size: 13px; color: var(--ink3); }}
    a {{ color: inherit; }}
    code {{ font-family: "SFMono-Regular", ui-monospace, monospace; }}
    @media (max-width: 1200px) {{
      .review-body {{ grid-template-columns: minmax(0, 1fr); }}
      .inspector-column {{ width: auto; }}
      .bottom-bar {{ height: auto; flex-wrap: wrap; }}
      .manual-label-wrap {{ width: 100%; flex-basis: 100%; }}
      .stress-chip {{ margin-left: 0; }}
    }}
  </style>
</head>
<body>
{body}
<script>
document.addEventListener('DOMContentLoaded', function () {{
  const textInput = document.querySelector('input[name="label_input"]');
  const reviewForm = document.getElementById('review-form');
  const selectedTruthLabel = document.getElementById('selected-truth-label');
  const actionInput = document.getElementById('review-action');
  const noteInput = document.getElementById('note-input');
  const copyablePaths = document.querySelectorAll('[data-copy-path]');

  function syncNotes(form) {{
    if (!form || !noteInput) {{
      return;
    }}
    const noteField = form.querySelector('input[name="notes"]');
    if (noteField) {{
      noteField.value = noteInput.value;
    }}
  }}

  function submitResolvedLabel(truthLabel, actionValue) {{
    if (!reviewForm || !selectedTruthLabel || !actionInput) {{
      return false;
    }}
    selectedTruthLabel.value = truthLabel || '';
    actionInput.value = actionValue || 'save';
    if (actionValue === 'save' && textInput && truthLabel) {{
      textInput.value = '';
    }}
    syncNotes(reviewForm);
    reviewForm.submit();
    return true;
  }}

  document.querySelectorAll('button[data-resolved-label]').forEach(function (button) {{
    button.addEventListener('click', function (event) {{
      event.preventDefault();
      submitResolvedLabel(button.getAttribute('data-truth-label') || '', button.getAttribute('data-submit-action') || 'save');
    }});
  }});

  document.querySelectorAll('form').forEach(function (form) {{
    form.addEventListener('submit', function () {{
      syncNotes(form);
    }});
  }});

  copyablePaths.forEach(function (node) {{
    node.addEventListener('click', function () {{
      const pathValue = node.getAttribute('data-copy-path') || '';
      const labelValue = node.getAttribute('data-copy-label') || (node.textContent || '');
      if (!pathValue || !navigator.clipboard || !navigator.clipboard.writeText) {{
        return;
      }}
      navigator.clipboard.writeText(pathValue).then(function () {{
        node.textContent = 'Copied path';
        window.setTimeout(function () {{
          node.textContent = labelValue;
        }}, 900);
      }}).catch(function () {{
        return;
      }});
    }});
  }});

  document.addEventListener('keydown', function (event) {{
    const active = document.activeElement;
    const activeTag = (active && active.tagName || '').toLowerCase();
    const activeType = (active && active.type || '').toLowerCase();
    const isTextInput = activeTag === 'textarea' || (activeTag === 'input' && !['hidden', 'checkbox', 'button', 'submit'].includes(activeType));

    if (/^[1-5]$/.test(event.key) && !isTextInput) {{
      const button = document.querySelector('button[data-recent-index="' + event.key + '"]');
      if (button) {{
        event.preventDefault();
        button.click();
        return;
      }}
    }}

    if (event.key === '0' && !isTextInput) {{
      const button = document.querySelector('button[data-suggestion="true"]');
      if (button) {{
        event.preventDefault();
        button.click();
        return;
      }}
    }}

    const actionKeys = {{
      'r': 'reject',
      'u': 'unsure',
      'n': 'not_a_bird',
      'c': 'bad_crop',
      'd': 'duplicate',
      'k': 'skip'
    }};
    const mappedAction = actionKeys[(event.key || '').toLowerCase()];
    if (!isTextInput && mappedAction) {{
      const form = document.querySelector('form[data-action="' + mappedAction + '"]');
      if (form) {{
        event.preventDefault();
        syncNotes(form);
        form.submit();
        return;
      }}
    }}

    if (!isTextInput && (event.key === 's' || event.key === 'S')) {{
      const stress = document.getElementById('stress-toggle');
      if (stress) {{
        event.preventDefault();
        stress.checked = !stress.checked;
        return;
      }}
    }}

    if (!isTextInput && (event.key === 't' || event.key === 'T') && textInput) {{
      event.preventDefault();
      textInput.focus();
      textInput.select();
      return;
    }}

    if (!isTextInput && (event.key === 'p' || event.key === 'P')) {{
      const link = document.querySelector('[data-nav="prev"]');
      if (link) {{
        event.preventDefault();
        window.location = link.getAttribute('href');
      }}
    }}
  }});
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
    _session_metrics_lock = threading.Lock()
    _session_metrics: dict[str, dict[str, Any]] = {}
    _draft_notes_lock = threading.Lock()
    _draft_notes: dict[str, str] = {}

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

    @classmethod
    def _record_completed_action(cls, scope_key: str) -> None:
        now = monotonic()
        with cls._session_metrics_lock:
            metric = cls._session_metrics.setdefault(
                scope_key,
                {
                    "last_action_at": None,
                    "interval_total": 0.0,
                    "interval_count": 0,
                },
            )
            last_action = metric.get("last_action_at")
            if last_action is not None:
                metric["interval_total"] += max(0.0, now - float(last_action))
                metric["interval_count"] += 1
            metric["last_action_at"] = now

    @classmethod
    def _tempo_display(cls, scope_key: str) -> str:
        with cls._session_metrics_lock:
            metric = cls._session_metrics.get(scope_key)
            if not metric or int(metric.get("interval_count") or 0) <= 0:
                return "--.-s/img"
            avg = float(metric["interval_total"]) / float(metric["interval_count"])
        return f"{avg:.1f}s/img"

    @classmethod
    def _get_draft_note(cls, candidate_id: str) -> str:
        with cls._draft_notes_lock:
            return cls._draft_notes.get(candidate_id, "")

    @classmethod
    def _set_draft_note(cls, candidate_id: str, note: str) -> None:
        with cls._draft_notes_lock:
            if note:
                cls._draft_notes[candidate_id] = note
            else:
                cls._draft_notes.pop(candidate_id, None)

    @classmethod
    def _clear_draft_note(cls, candidate_id: str) -> None:
        with cls._draft_notes_lock:
            cls._draft_notes.pop(candidate_id, None)

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
        if not prefill_notes:
            prefill_notes = self._get_draft_note(candidate_id) or ((annotation or {}).get("notes") or "")
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
            if action == "note":
                annotation = self.store.get_annotation(candidate_id)
                if annotation is not None:
                    row = dict(annotation)
                    row["candidate_id"] = candidate_id
                    row["notes"] = notes
                    row["annotated_at"] = row.get("annotated_at") or _utc_now()
                    self.store.upsert_annotation(row)
                self._set_draft_note(candidate_id, notes)
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/review?scope={_q(scope_key)}&id={html.escape(candidate_id)}")
                self.end_headers()
                return
            if action not in self._allowed_actions(scope):
                raise ValueError(
                    f"Action '{action}' is not allowed for workflow "
                    f"'{self._workflow_type(scope)}'"
                )
            if action == "skip":
                self.store.mark_candidate_skipped(candidate_id)
                self._set_draft_note(candidate_id, notes)
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
                self._clear_draft_note(candidate_id)
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
                self._clear_draft_note(candidate_id)
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
                self._clear_draft_note(candidate_id)
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

        if action in {"save", "save_burst", "skip", "reject", "unsure", "not_a_bird", "bad_crop", "duplicate"}:
            self._record_completed_action(scope_key)

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
        allow_stress = self._allows_stress(scope)
        allow_burst_apply = self._allows_burst_apply(scope)
        default_save_action = "save_burst" if allow_burst_apply and burst_target_count else "save"
        source_path = str(image.get("source_image_path", ""))
        source_path_obj = Path(source_path) if source_path else None
        scope_path = str(scope.get("trip_folder") or "")
        scope_leaf = Path(scope_path).name if scope_path else str(scope.get("scope_name") or "")
        compact_scope = f"{scope.get('catalog_name') or ''} / {scope_leaf}".strip(" /")
        source_display_path = source_path or (str(source_path_obj) if source_path_obj else "")
        tempo = self._tempo_display(scope["scope_key"])
        progress = self._progress_metrics(queue_position)
        notes = prefill_notes or (annotation or {}).get("notes", "")

        context = {
            "scope": scope,
            "candidate": candidate,
            "image": image,
            "annotation": annotation,
            "recent": recent,
            "error": error,
            "prev_candidate": prev_candidate,
            "burst_target_count": burst_target_count,
            "burst_position": burst_position,
            "queue_position": queue_position,
            "unreviewed_images": unreviewed_images,
            "unreviewed_candidates": unreviewed_candidates,
            "suggestion": suggestion,
            "estimated_subject_box_size": estimated_subject_box_size,
            "suggestion_status": suggestion_status,
            "prefill_selected_truth_label": prefill_selected_truth_label,
            "prefill_label_input": prefill_label_input,
            "prefill_stress": prefill_stress,
            "prefill_notes": notes,
            "stress_reason": stress_reason,
            "default_save_action": default_save_action,
            "allow_stress": allow_stress,
            "allow_burst_apply": allow_burst_apply,
            "compact_scope": compact_scope,
            "source_display_path": source_display_path,
            "tempo": tempo,
            "progress": progress,
            "details_rows": self._build_details_rows(
                scope=scope,
                candidate=candidate,
                image=image,
                queue_position=queue_position,
                unreviewed_images=unreviewed_images,
                unreviewed_candidates=unreviewed_candidates,
                burst_position=burst_position,
                estimated_subject_box_size=estimated_subject_box_size,
            ),
            "ebird_reference": self._build_ebird_reference(
                annotation=annotation,
                suggestion=suggestion,
                selected_truth_label=prefill_selected_truth_label,
            ),
        }

        return f"""
        <div class="page">
          <div class="app-shell">
            {self._render_top_bar(context)}
            <div class="wrap">
              {self._render_outcome_rail(context)}
              <div class="review-body">
                <div class="photo-column">
                  {self._render_photo_stage(context)}
                  {self._render_classifier_banner(context)}
                </div>
                <div class="inspector-column">
                  {self._render_inspector(context)}
                </div>
              </div>
            </div>
            {self._render_bottom_bar(context)}
          </div>
        </div>
        """

    @staticmethod
    def _workflow_presentation(scope: dict[str, Any]) -> dict[str, Any]:
        if ReviewAppHandler._is_run_hybrid_review(scope):
            return {
                "app_name": "review_run",
                "shortcut_copy": "Shortcuts: 0 accept suggestion, 1-5 recent labels, T text input, K skip, P previous.",
                "rail_actions": [
                    {
                        "action": "save",
                        "label": "Accept",
                        "key": "0",
                        "variant": "accept",
                        "mode": "suggestion",
                    },
                    {
                        "action": "skip",
                        "label": "Skip",
                        "key": "K",
                        "variant": "neutral",
                    },
                ],
            }
        return {
            "app_name": "review_app",
            "shortcut_copy": "Shortcuts: 0 accept suggestion, 1-5 recent labels, T text input, S stress, R reject, U unsure, N not a bird, C bad crop, D duplicate, K skip, P previous.",
            "rail_actions": [
                {
                    "action": "save",
                    "label": "Accept",
                    "key": "0",
                    "variant": "accept",
                    "mode": "suggestion",
                },
                {"action": "reject", "label": "Reject", "key": "R", "variant": "danger"},
                {"action": "unsure", "label": "Unsure", "key": "U", "variant": "neutral"},
                {"action": "not_a_bird", "label": "Not A Bird", "key": "N", "variant": "danger"},
                {"action": "bad_crop", "label": "Bad Crop", "key": "C", "variant": "neutral"},
                {"action": "duplicate", "label": "Duplicate", "key": "D", "variant": "neutral"},
                {"action": "skip", "label": "Skip", "key": "K", "variant": "neutral"},
            ],
        }

    @staticmethod
    def _progress_metrics(queue_position: tuple[int, int] | None) -> dict[str, Any]:
        if not queue_position or queue_position[1] <= 0:
            return {"current": 0, "total": 0, "fraction": 0.0, "label": "0 / 0"}
        current, total = queue_position
        current = max(1, int(current))
        total = max(current, int(total))
        return {
            "current": current,
            "total": total,
            "fraction": current / total,
            "label": f"{current} / {total}",
        }

    def _render_top_bar(self, context: dict[str, Any]) -> str:
        scope = context["scope"]
        progress = context["progress"]
        source_path = str(context["image"].get("source_image_path") or "")
        backlog_images = int(context["unreviewed_images"] or 0)
        backlog_candidates = int(context["unreviewed_candidates"] or 0)
        return f"""
        <div class="topbar">
          <div class="topbar-left">
            <h1>{html.escape(self._workflow_presentation(scope)["app_name"])}</h1>
            <div class="divider"></div>
            <div class="topbar-stanza">
              <div class="mono-label">Scope</div>
              <div class="topbar-value">{html.escape(context["compact_scope"] or str(scope.get("scope_name") or ""))}</div>
            </div>
            <div class="topbar-stanza copyable-path" title="Click to copy full path" data-copy-path="{html.escape(source_path)}" data-copy-label="{html.escape(context['source_display_path'] or 'Unknown file')}">
              <div class="mono-label">Image</div>
              <div class="topbar-value">{html.escape(context["source_display_path"] or "Unknown file")}</div>
            </div>
          </div>
          <div class="topbar-right">
            <div class="topbar-stanza">
              <div class="mono-label">Queue</div>
              <div class="progress-numbers"><strong>{html.escape(progress["label"])}</strong> image queue</div>
            </div>
            <div class="topbar-stanza">
              <div class="mono-label">Backlog</div>
              <div class="progress-numbers"><strong>{backlog_images}</strong> images / <strong>{backlog_candidates}</strong> candidates</div>
            </div>
            <div class="progress-track"><div class="progress-fill" style="width:{progress['fraction'] * 100:.1f}%"></div></div>
            <div class="topbar-stanza">
              <div class="mono-label">Tempo</div>
              <div class="progress-numbers">{html.escape(context["tempo"])}</div>
            </div>
            <div class="topbar-actions">
              <a class="btn" href="/scopes">Scopes</a>
              <a class="btn" href="/summary?scope={_q(scope['scope_key'])}">Summary</a>
            </div>
          </div>
        </div>
        """

    def _render_outcome_rail(self, context: dict[str, Any]) -> str:
        scope = context["scope"]
        suggestion = context["suggestion"]
        default_save_action = context["default_save_action"]
        rail = []
        for item in self._workflow_presentation(scope)["rail_actions"]:
            label = item["label"]
            key = item["key"]
            variant = item["variant"]
            mode = item.get("mode")
            if mode == "suggestion" and suggestion is None:
                rail.append(
                    f"""
                    <div class="rail-tile accept" aria-disabled="true">
                      <span class="keycap keycap-accent">{html.escape(key)}</span>
                      <span class="rail-tile-label">{html.escape(label)}</span>
                    </div>
                    """
                )
                continue
            if mode == "suggestion":
                rail.append(
                    f"""
                    <form method="post" action="/review" data-suggestion="true" class="rail-form">
                      <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
                      <input type="hidden" name="candidate_id" value="{html.escape(context['candidate']['id'])}">
                      <input type="hidden" name="action" value="{html.escape(default_save_action)}">
                      <input type="hidden" name="selected_truth_label" value="{html.escape(suggestion.truth_label)}">
                      <input type="hidden" name="stress" value="0">
                      <input type="hidden" name="notes" value="">
                      <button type="submit" class="rail-tile accept" data-suggestion="true" data-resolved-label="1" data-truth-label="{html.escape(suggestion.truth_label)}" data-submit-action="{html.escape(default_save_action)}">
                        <span class="keycap keycap-accent">{html.escape(key)}</span>
                        <span class="rail-tile-label">{html.escape(label)}</span>
                      </button>
                    </form>
                    """
                )
                continue
            rail.append(
                self._outcome_button(
                    scope["scope_key"],
                    context["candidate"]["id"],
                    item["action"],
                    item["label"],
                    key_hint=key,
                    variant=variant,
                )
            )

        previous_link = (
            f'<a class="rail-tile" data-nav="prev" href="/review?scope={_q(scope["scope_key"])}&id={html.escape(context["prev_candidate"]["id"])}">'
            f'<span class="keycap">P</span><span class="rail-tile-label">Previous</span></a>'
            if context["prev_candidate"]
            else '<div class="rail-tile" aria-disabled="true"><span class="keycap">P</span><span class="rail-tile-label">Previous</span></div>'
        )
        return f"""
        <div class="rail">
          {''.join(rail)}
          <div class="rail-fill"></div>
          {previous_link}
        </div>
        """

    def _render_photo_stage(self, context: dict[str, Any]) -> str:
        candidate = context["candidate"]
        return f"""
        <div class="panel" style="flex:1; display:flex; flex-direction:column; min-height:0;">
          <div class="stage-header">
            <div>
              <div class="mono-label">Photo Stage</div>
              <div class="small">{html.escape(self._workflow_presentation(context["scope"])["shortcut_copy"])}</div>
            </div>
            <div class="stage-header-controls">
              <button type="button">Fit</button>
              <button type="button">100%</button>
            </div>
          </div>
          <div class="photo-frame">
            <img class="candidate-preview" src="/preview/{html.escape(candidate['id'])}" alt="candidate preview">
          </div>
        </div>
        """

    def _render_classifier_banner(self, context: dict[str, Any]) -> str:
        suggestion = context["suggestion"]
        suggestion_status = context["suggestion_status"]
        scope = context["scope"]
        default_save_action = context["default_save_action"]
        annotation = context["annotation"]
        selected = context["prefill_label_input"]
        stress_reason = context["stress_reason"]

        current_html = ""
        if annotation and annotation.get("annotation_status") == "labeled":
            current_html = (
                f'<div class="mono-small">Current: '
                f'{html.escape(annotation.get("truth_common_name") or "")}'
                f'{(" [stress]" if context["allow_stress"] and annotation.get("stress") else "")}</div>'
            )
        elif selected:
            current_html = f'<div class="mono-small">Pending text: {html.escape(selected)}</div>'

        if suggestion is None:
            details = html.escape(suggestion_status or "No classifier suggestion is available for this candidate.")
            extra = (
                f'<div class="mono-small">Stress hint: {html.escape(stress_reason)}</div>'
                if context["allow_stress"] and stress_reason
                else ""
            )
            return f"""
            <div class="classifier-banner">
              <div class="classifier-status"><span class="status-dot"></span><span class="mono-label">Classifier</span></div>
              <div class="classifier-main">
                <div class="classifier-fallback">Suggestion unavailable</div>
                <div class="mono-small">{details}</div>
                {current_html}
                {extra}
              </div>
            </div>
            """

        accept_label = "Accept Suggestion"
        if self._allows_burst_apply(scope) and context["burst_target_count"]:
            accept_label = "Accept + Burst"
        stress_html = (
            f'<div class="mono-small">Stress suggested: {html.escape(stress_reason)}</div>'
            if context["allow_stress"] and stress_reason
            else ""
        )
        return f"""
        <div class="classifier-banner">
          <div class="classifier-status">
            <span class="status-dot"></span>
            <span class="keycap keycap-accent">0</span>
          </div>
          <div class="classifier-main">
            <div class="classifier-name">{html.escape(suggestion.truth_common_name)}</div>
            <div class="classifier-sci">{html.escape(suggestion.truth_sci_name)}</div>
            <div class="classifier-meta">
              <div class="classifier-confidence"><div class="classifier-confidence-fill" style="width:{suggestion.confidence * 100:.1f}%"></div></div>
              <div class="classifier-confidence-value">{suggestion.confidence:.0%}</div>
            </div>
            {current_html}
            {stress_html}
          </div>
          <form method="post" action="/review" data-suggestion="true">
            <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
            <input type="hidden" name="candidate_id" value="{html.escape(context['candidate']['id'])}">
            <input type="hidden" name="action" value="{html.escape(default_save_action)}">
            <input type="hidden" name="selected_truth_label" value="{html.escape(suggestion.truth_label)}">
            <input type="hidden" name="stress" value="0">
            <input type="hidden" name="notes" value="">
            <button type="submit" class="primary" data-suggestion="true" data-resolved-label="1" data-truth-label="{html.escape(suggestion.truth_label)}" data-submit-action="{html.escape(default_save_action)}">{html.escape(accept_label)}</button>
          </form>
        </div>
        """

    def _render_inspector(self, context: dict[str, Any]) -> str:
        ebird_reference = context["ebird_reference"]
        error = context["error"]
        details_rows = context["details_rows"]
        notes = html.escape(context["prefill_notes"])
        source_path = str(context["image"].get("source_image_path") or "")

        error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
        reference_html = ""
        if ebird_reference is not None:
            thumb = (
                f'<div class="reference-thumb"><img src="{html.escape(ebird_reference.preview_image_url)}" alt="{html.escape(ebird_reference.truth_common_name)} reference"></div>'
                if ebird_reference.preview_image_url
                else '<div class="reference-thumb"><div class="small">Reference photo unavailable.</div></div>'
            )
            reference_html = f"""
            <div class="panel inspector-card">
              <div class="inspector-header">
                <div>
                  <div class="mono-label">eBird Reference</div>
                  <div>{html.escape(ebird_reference.truth_common_name)}</div>
                </div>
              </div>
              {thumb}
              <div class="small"><em>{html.escape(ebird_reference.truth_sci_name)}</em></div>
              <div class="reference-actions">
                <a class="btn" href="{html.escape(ebird_reference.species_url)}" target="_blank" rel="noopener">Open eBird</a>
                <a class="btn" href="{html.escape(ebird_reference.macaulay_asset_url or ebird_reference.media_search_url)}" target="_blank" rel="noopener">Open Macaulay</a>
              </div>
            </div>
            """

        details_html = "".join(
            f"""
            <div class="detail-item">
              <div class="mono-label">{html.escape(label)}</div>
              <div class="detail-value">{html.escape(value)}</div>
            </div>
            """
            for label, value in details_rows
        ) or '<div class="small">No extra metadata available for this image.</div>'

        note_form = f"""
        <div class="panel inspector-card">
          <div class="mono-label">Notes</div>
          <form method="post" action="/review">
            <input type="hidden" name="scope_key" value="{html.escape(context['scope']['scope_key'])}">
            <input type="hidden" name="candidate_id" value="{html.escape(context['candidate']['id'])}">
            <input type="hidden" name="action" value="note">
            <input type="hidden" name="selected_truth_label" value="">
            <input type="hidden" name="notes" value="">
            <input id="note-input" class="note-input" type="text" value="{notes}" placeholder="Optional note">
            <div class="reference-actions" style="margin-top:8px;">
              <button type="submit">Save Note</button>
            </div>
          </form>
          <div class="path-text copyable-path" title="Click to copy full path" data-copy-path="{html.escape(source_path)}" data-copy-label="{html.escape(source_path)}">{html.escape(source_path)}</div>
        </div>
        """

        return f"""
        {error_html}
        {reference_html}
        <div class="panel inspector-card">
          <div class="inspector-header">
            <div>
              <div class="mono-label">Details</div>
              <div class="small">Available image metadata only.</div>
            </div>
          </div>
          <div class="details-grid">{details_html}</div>
        </div>
        {note_form}
        """

    def _render_bottom_bar(self, context: dict[str, Any]) -> str:
        scope = context["scope"]
        recent = context["recent"]
        default_save_action = context["default_save_action"]
        prefill_label_input = context["prefill_label_input"]
        stress_checked = "checked" if context["prefill_stress"] else ""
        recent_html = []
        for idx, row in enumerate(recent, start=1):
            recent_html.append(
                f"""
                <form method="post" action="/review">
                  <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
                  <input type="hidden" name="candidate_id" value="{html.escape(context['candidate']['id'])}">
                  <input type="hidden" name="action" value="{html.escape(default_save_action)}">
                  <input type="hidden" name="selected_truth_label" value="{html.escape(row['truth_label'])}">
                  <input type="hidden" name="stress" value="0">
                  <input type="hidden" name="notes" value="">
                  <button type="submit" class="recent-btn" data-recent-index="{idx}">
                    <span class="keycap">{idx}</span>
                    <span class="label-text">{html.escape(row['truth_common_name'])}</span>
                  </button>
                </form>
                """
            )

        stress_chip = (
            f"""
            <label class="stress-chip">
              <input id="stress-toggle" type="checkbox" name="stress" value="1" {stress_checked}>
              <span class="keycap">S</span>
              <span>Stress</span>
            </label>
            """
            if context["allow_stress"]
            else ""
        )

        return f"""
        <div class="bottom-bar">
          <form id="review-form" method="post" action="/review" style="display:flex; align-items:center; gap:12px; flex:1; min-width:0;">
            <input type="hidden" name="scope_key" value="{html.escape(scope['scope_key'])}">
            <input type="hidden" name="candidate_id" value="{html.escape(context['candidate']['id'])}">
            <input id="review-action" type="hidden" name="action" value="{html.escape(default_save_action)}">
            <input id="selected-truth-label" type="hidden" name="selected_truth_label" value="{html.escape(context['prefill_selected_truth_label'])}">
            <input type="hidden" name="notes" value="">
            <div class="manual-label-wrap">
              <input class="manual-label-input" type="text" name="label_input" value="{html.escape(prefill_label_input)}" placeholder="Type common or scientific name (T)">
            </div>
            <button class="primary" type="submit">Save Label</button>
            <div class="recent-strip">
              {''.join(recent_html) or '<div class="small">No recent labels yet.</div>'}
            </div>
            {stress_chip}
          </form>
        </div>
        """

    @staticmethod
    def _build_details_rows(
        *,
        scope: dict[str, Any],
        candidate: dict[str, Any],
        image: dict[str, Any],
        queue_position: tuple[int, int] | None,
        unreviewed_images: int,
        unreviewed_candidates: int,
        burst_position: tuple[int, int] | None,
        estimated_subject_box_size: tuple[float, float] | None,
    ) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        image_path = image.get("source_image_path")
        metadata: dict[str, Any] = {}
        if image_path:
            try:
                metadata = load_subject_size_metadata(image_path)
            except Exception:
                metadata = {}

        def add(label: str, value: Any) -> None:
            if value is None:
                return
            text = str(value).strip()
            if text:
                rows.append((label, text))

        add("Scope", scope.get("scope_name"))
        add("Candidate", candidate.get("id"))
        add("Captured", image.get("capture_datetime"))
        add("Lens", image.get("lens_model"))
        focal_length = image.get("focal_length")
        if focal_length not in {None, ""}:
            add("Focal Length", f"{float(focal_length):.0f} mm")
        add("Rating", image.get("rating"))
        add("Region", image.get("region_hint"))
        if queue_position:
            add("Queue", f"{queue_position[0]} / {queue_position[1]}")
        add("Unreviewed", f"{unreviewed_images} image(s), {unreviewed_candidates} candidate(s)")
        if burst_position:
            add("Burst", f"{burst_position[0]} / {burst_position[1]}")
        add("Burst Group", image.get("burst_group_id"))
        add("Detector", candidate.get("detector_name"))
        if candidate.get("detector_confidence") is not None:
            add("Confidence", f"{float(candidate['detector_confidence']):.0%}")
        if metadata.get("focus_distance_m") is not None:
            add("Focus Dist.", f"{float(metadata['focus_distance_m']):.2f} m")
        if metadata.get("focal_length_35mm_mm") is not None:
            add("35mm Eq.", f"{float(metadata['focal_length_35mm_mm']):.0f} mm")
        if estimated_subject_box_size is not None:
            add(
                "Box Size",
                f"{estimated_subject_box_size[0]:.1f} cm × {estimated_subject_box_size[1]:.1f} cm",
            )
        return rows

    @staticmethod
    def _outcome_button(
        scope_key: str,
        candidate_id: str,
        action: str,
        label: str,
        *,
        key_hint: str | None = None,
        variant: str = "neutral",
    ) -> str:
        variant_class = " accept" if variant == "accept" else " danger" if variant == "danger" else ""
        keycap_class = " keycap-danger" if variant == "danger" else " keycap-accent" if variant == "accept" else ""
        keycap = f'<span class="keycap{keycap_class}">{html.escape(key_hint)}</span>' if key_hint else ""
        return f"""
        <form method="post" action="/review" data-action="{html.escape(action)}" class="rail-form">
          <input type="hidden" name="scope_key" value="{html.escape(scope_key)}">
          <input type="hidden" name="candidate_id" value="{html.escape(candidate_id)}">
          <input type="hidden" name="action" value="{html.escape(action)}">
          <input type="hidden" name="notes" value="">
          <button type="submit" class="rail-tile{variant_class}">{keycap}<span class="rail-tile-label">{html.escape(label)}</span></button>
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
