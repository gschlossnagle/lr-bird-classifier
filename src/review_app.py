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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .label_resolver import AmbiguousLabelError, LabelResolver, ResolvedLabel, UnknownLabelError
from .review_queue import QueueFilters, ReviewQueue
from .review_suggester import ReviewSuggester, SuggestedLabel
from .review_store import ReviewStore


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
    img {{ max-width: 100%; height: auto; display: block; border-radius: 8px; }}
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
  </style>
</head>
<body>
{body}
<script>
document.addEventListener('keydown', function (event) {{
  const tag = (event.target && event.target.tagName || '').toLowerCase();
  const inText = tag === 'input' || tag === 'textarea';
  const textInput = document.querySelector('input[name="label_input"]');
  const textFieldEmpty = textInput && textInput.value === '';

  if (/^[1-5]$/.test(event.key) && (!inText || (textInput && document.activeElement === textInput && textFieldEmpty))) {{
    const form = document.querySelector('form[data-recent-index="' + event.key + '"]');
    if (form) {{
      event.preventDefault();
      form.submit();
      return;
    }}
  }}

  if ((event.key === 'r' || event.key === 'R') && !event.metaKey && !event.ctrlKey && (!inText || (textInput && document.activeElement === textInput && textFieldEmpty))) {{
    const form = document.querySelector('form[data-recent-index="1"]');
    if (form) {{
      event.preventDefault();
      form.submit();
      return;
    }}
  }}

  if (event.key === '0' && (!inText || (textInput && document.activeElement === textInput && textFieldEmpty))) {{
    const form = document.querySelector('form[data-suggestion="true"]');
    if (form) {{
      event.preventDefault();
      form.submit();
      return;
    }}
  }}

  if ((event.key === 't' || event.key === 'T') && !inText) {{
    const stress = document.getElementById('stress-toggle');
    if (stress) {{
      event.preventDefault();
      stress.checked = !stress.checked;
    }}
  }}

  if (!inText && (event.key === 'k' || event.key === 'K')) {{
    const form = document.querySelector('form[data-action="skip"]');
    if (form) {{
      event.preventDefault();
      form.submit();
    }}
  }}

  if (!inText && (event.key === 'f' || event.key === 'F') && textInput) {{
    event.preventDefault();
    textInput.focus();
    textInput.select();
  }}

  if (!inText && (event.key === 'p' || event.key === 'P')) {{
    const link = document.querySelector('a[data-nav="prev"]');
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/review"}:
            self._handle_review_get(parsed)
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
        candidate_id = params.get("id", [None])[0]
        error = params.get("error", [""])[0]

        queue = ReviewQueue(self.store, QueueFilters(review_status="unreviewed"))
        if candidate_id is None:
            next_candidate = queue.next_candidate()
            if next_candidate is None:
                self._write_html("Review Complete", self._render_complete())
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
        suggestion = self._build_suggestion(candidate, image)

        body = self._render_candidate(
            candidate,
            image,
            annotation,
            recent,
            error,
            prev_candidate,
            len(burst_targets),
            suggestion,
        )
        self._write_html("Review Candidate", body)

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
                row = self._build_labeled_row(candidate_id, resolved, label_input or selected_truth_label, stress, notes)
                self.store.upsert_annotation(row)
            elif action == "save_burst":
                resolved = self._resolve_label(selected_truth_label, label_input)
                row = self._build_labeled_row(candidate_id, resolved, label_input or selected_truth_label, stress, notes)
                self.store.upsert_annotation(row)
                self.store.apply_annotation_to_burst(candidate_id, row)
            else:
                raise ValueError(f"Unsupported action '{action}'")
        except (UnknownLabelError, AmbiguousLabelError, ValueError) as e:
            location = f"/review?id={candidate_id}&error={html.escape(str(e))}"
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.end_headers()
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
        suggestion: SuggestedLabel | None,
    ) -> str:
        recent_forms = []
        for idx, row in enumerate(recent, start=1):
            recent_forms.append(
                f"""
                <form method="post" action="/review" data-recent-index="{idx}">
                  <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                  <input type="hidden" name="action" value="save">
                  <input type="hidden" name="selected_truth_label" value="{html.escape(row['truth_label'])}">
                  <input type="hidden" name="stress" value="0">
                  <button type="submit">{idx}. {html.escape(row['truth_common_name'])}</button>
                </form>
                """
            )

        error_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
        notes = html.escape((annotation or {}).get("notes", ""))
        resolved_preview = ""
        if annotation and annotation["annotation_status"] == "labeled":
            resolved_preview = (
                f"<div class='resolved'><strong>Current:</strong> "
                f"{html.escape(annotation['truth_common_name'])} "
                f"({html.escape(annotation['truth_sci_name'])})"
                f"{' [stress]' if annotation['stress'] else ''}</div>"
            )
        suggestion_html = ""
        if suggestion is not None:
            suggestion_html = f"""
            <div class="resolved">
              <div><strong>Classifier suggestion</strong>: {html.escape(suggestion.truth_common_name)} ({html.escape(suggestion.truth_sci_name)}) {suggestion.confidence:.1%}</div>
              <form method="post" action="/review" data-suggestion="true" style="margin-top:8px;">
                <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
                <input type="hidden" name="action" value="save">
                <input type="hidden" name="selected_truth_label" value="{html.escape(suggestion.truth_label)}">
                <input type="hidden" name="stress" value="0">
                <button type="submit">0. Accept Suggestion</button>
              </form>
            </div>
            """

        return f"""
        <div class="wrap">
          <div class="panel">
            <img src="/preview/{html.escape(candidate['id'])}" alt="preview">
          </div>
          <div class="panel">
            <div class="meta">
              <div><strong>Candidate:</strong> <code>{html.escape(candidate['id'])}</code></div>
              <div><strong>Source:</strong> <code>{html.escape(str(image.get('source_image_path', '')))}</code></div>
              <div><strong>Captured:</strong> {html.escape(str(image.get('capture_datetime', '')))}</div>
              <div><strong>Detector:</strong> {html.escape(str(candidate.get('detector_name', '')))} @ {candidate.get('detector_confidence', 0):.2f}</div>
              <div><strong>Region:</strong> {html.escape(str(image.get('region_hint', '')))}</div>
              <div><strong>Burst:</strong> {html.escape(str(image.get('burst_group_id', '')))}</div>
              <div class="small">{'This candidate has ' + str(burst_target_count) + ' other safe burst item(s).' if burst_target_count else 'No safe burst targets available.'}</div>
            </div>
            {error_html}
            {resolved_preview}
            {suggestion_html}
            <div style="margin: 10px 0 16px 0;">
              {f'<a data-nav="prev" href="/review?id={html.escape(prev_candidate["id"])}">Previous Reviewed Candidate</a>' if prev_candidate else '<span class="small">No previous reviewed candidate</span>'}
            </div>
            <form method="post" action="/review">
              <input type="hidden" name="candidate_id" value="{html.escape(candidate['id'])}">
              <input type="hidden" name="action" value="save">
              <input type="text" name="label_input" placeholder="Type common name">
              <label class="toggle"><input id="stress-toggle" type="checkbox" name="stress" value="1"> mark as stress</label>
              <div class="small">Type a common name or use a recent label button below. Shortcuts: 0, 1-5, R, T, K, P, F.</div>
              <div style="margin-top:10px">
                <button class="primary" type="submit">Save Label</button>
                {f'<button type="submit" formaction="/review" formmethod="post" name="action" value="save_burst">Save + Apply To Burst</button>' if burst_target_count else ''}
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
    return p.parse_args()


def main() -> int:
    args = parse_args()
    labels = load_label_inventory(args.labels_file)
    if not labels:
        raise SystemExit("No labels loaded from labels file")

    store = ReviewStore(args.db)
    resolver = LabelResolver(labels)
    try:
        suggester: ReviewSuggester | None = ReviewSuggester()
    except Exception:
        suggester = None

    class Handler(ReviewAppHandler):
        pass

    Handler.store = store
    Handler.resolver = resolver
    Handler.suggester = suggester

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        print(f"Review UI: http://{args.host}:{args.port}/review")
        server.serve_forever()
    finally:
        store.close()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
