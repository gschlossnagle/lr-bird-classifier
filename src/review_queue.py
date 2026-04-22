"""
Queue helpers for the annotation review workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .review_store import ReviewStore


@dataclass(frozen=True)
class QueueFilters:
    """Subset of filters needed for v1 queue navigation."""

    review_status: str | None = "unreviewed"
    burst_group_id: str | None = None


class ReviewQueue:
    """High-level queue navigation over candidates stored in ReviewStore."""

    def __init__(self, store: ReviewStore, filters: QueueFilters | None = None) -> None:
        self.store = store
        self.filters = filters or QueueFilters()

    def list_candidates(self) -> list[dict[str, Any]]:
        return self.store.list_candidates(
            review_status=self.filters.review_status,
            burst_group_id=self.filters.burst_group_id,
        )

    def next_candidate(self, current_candidate_id: str | None = None) -> dict[str, Any] | None:
        candidates = self.list_candidates()
        if not candidates:
            return None
        if current_candidate_id is None:
            return candidates[0]
        for idx, candidate in enumerate(candidates):
            if candidate["id"] == current_candidate_id:
                return candidates[idx + 1] if idx + 1 < len(candidates) else None
        return None

    def previous_candidate(self, current_candidate_id: str) -> dict[str, Any] | None:
        candidates = self.list_candidates()
        for idx, candidate in enumerate(candidates):
            if candidate["id"] == current_candidate_id:
                return candidates[idx - 1] if idx > 0 else None
        return None

    def open_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            return None
        if candidate["review_status"] in {"unreviewed", "skipped", "reviewed"}:
            self.store.mark_candidate_in_review(candidate_id)
            candidate = self.store.get_candidate(candidate_id)
        return candidate
