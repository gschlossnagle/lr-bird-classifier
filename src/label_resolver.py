"""
Common-name to canonical-label resolution for the annotation review workflow.

The resolver is intentionally fed an explicit label inventory rather than
inferring all possible labels from cached names alone. That keeps resolution
honest: we only resolve to labels the caller has actually made available.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .taxonomy import build_label_map, get_common_name, parse_label


@dataclass(frozen=True)
class ResolvedLabel:
    """A canonical species identity usable for annotation and scoring."""

    truth_common_name: str
    truth_sci_name: str
    truth_label: str
    taxon_class: str

    @property
    def canonical_common_name(self) -> str:
        """Return a display-stable common name."""
        return self.truth_common_name.title()


class AmbiguousLabelError(ValueError):
    """Raised when a free-text name matches multiple candidate labels."""

    def __init__(self, query: str, matches: list[ResolvedLabel]) -> None:
        self.query = query
        self.matches = matches
        super().__init__(f"Ambiguous label '{query}' matched {len(matches)} labels")


class UnknownLabelError(ValueError):
    """Raised when a free-text name matches no known label."""


class LabelResolver:
    """Resolve reviewer-entered common names into canonical labels."""

    def __init__(self, labels: Iterable[str]) -> None:
        labels = list(labels)
        label_map = build_label_map(
            {label: idx for idx, label in enumerate(labels)},
            fetch_missing=False,
        )
        self._resolved_by_label: dict[str, ResolvedLabel] = {}
        self._labels_by_normalized_name: dict[str, list[ResolvedLabel]] = {}

        for label in labels:
            parsed = parse_label(label)
            common_name = get_common_name(label, label_map)
            resolved = ResolvedLabel(
                truth_common_name=common_name.title() if common_name else parsed.get("sci_name", label),
                truth_sci_name=parsed.get("sci_name", label),
                truth_label=label,
                taxon_class=parsed.get("class_", ""),
            )
            self._resolved_by_label[label] = resolved

            for key in {
                self._normalize(resolved.truth_common_name),
                self._normalize(resolved.truth_sci_name),
                self._normalize_compact(resolved.truth_common_name),
                self._normalize_compact(resolved.truth_sci_name),
            }:
                if not key:
                    continue
                self._labels_by_normalized_name.setdefault(key, []).append(resolved)

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(text.strip().lower().split())

    @staticmethod
    def _normalize_compact(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", text.strip().lower())

    def resolve_recent_label(self, label: str) -> ResolvedLabel:
        """Resolve a stored canonical label directly."""
        try:
            return self._resolved_by_label[label]
        except KeyError as e:
            raise UnknownLabelError(f"Unknown canonical label '{label}'") from e

    def resolve_common_name(self, query: str) -> ResolvedLabel:
        """
        Resolve a free-text common/scientific name to exactly one canonical label.

        Raises:
            UnknownLabelError: no known label matches.
            AmbiguousLabelError: multiple labels match.
        """
        normalized = self._normalize(query)
        matches = self._labels_by_normalized_name.get(normalized, [])
        if not matches:
            matches = self._labels_by_normalized_name.get(self._normalize_compact(query), [])
        if not matches:
            raise UnknownLabelError(
                f"No label matches '{query}'. Fix the name or enter a scientific name."
            )
        if len(matches) > 1:
            raise AmbiguousLabelError(query, matches)
        return matches[0]

    def suggest(self, query: str, limit: int = 10) -> list[ResolvedLabel]:
        """Return likely matches for a partial free-text query."""
        normalized = self._normalize(query)
        if not normalized:
            return []

        exact = self._labels_by_normalized_name.get(normalized, [])
        if exact:
            return exact[:limit]

        compact = self._normalize_compact(query)
        exact_compact = self._labels_by_normalized_name.get(compact, [])
        if exact_compact:
            return exact_compact[:limit]

        suggestions: list[ResolvedLabel] = []
        seen: set[str] = set()
        for name, resolved_list in self._labels_by_normalized_name.items():
            if normalized in name or compact in name:
                for resolved in resolved_list:
                    if resolved.truth_label not in seen:
                        suggestions.append(resolved)
                        seen.add(resolved.truth_label)
                        if len(suggestions) >= limit:
                            return suggestions
        return suggestions
