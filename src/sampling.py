"""
Sampling utilities for benchmark construction.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any


def sample_manifest_rows(
    rows: list[dict[str, Any]],
    *,
    size: int,
    mode: str,
    seed: int | None = None,
    min_per_label: int = 1,
    max_per_label: int | None = None,
) -> list[dict[str, Any]]:
    """
    Sample benchmark rows while controlling species-frequency bias.

    Modes:
        natural: proportional to input row frequency
        balanced: round-robin by truth_label up to the requested size
        hybrid: floor per label, then weighted toward underrepresented labels
    """
    if mode not in {"natural", "balanced", "hybrid"}:
        raise ValueError(f"Unsupported sampling mode '{mode}'")
    if size < 0:
        raise ValueError("size must be non-negative")
    if size == 0 or not rows:
        return []

    rng = random.Random(seed)
    grouped = _group_by_truth_label(rows)
    capped = {label: items[: max_per_label] if max_per_label is not None else items[:] for label, items in grouped.items()}
    for items in capped.values():
        rng.shuffle(items)

    if mode == "natural":
        pool = [row for items in capped.values() for row in items]
        rng.shuffle(pool)
        return pool[: min(size, len(pool))]

    if mode == "balanced":
        return _round_robin_sample(capped, size)

    return _hybrid_sample(
        capped,
        size=size,
        rng=rng,
        min_per_label=min_per_label,
    )


def _group_by_truth_label(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["truth_label"]].append(row)
    return grouped


def _round_robin_sample(grouped: dict[str, list[dict[str, Any]]], size: int) -> list[dict[str, Any]]:
    labels = sorted(grouped)
    selected: list[dict[str, Any]] = []
    offsets = {label: 0 for label in labels}
    while len(selected) < size:
        made_progress = False
        for label in labels:
            idx = offsets[label]
            items = grouped[label]
            if idx < len(items) and len(selected) < size:
                selected.append(items[idx])
                offsets[label] += 1
                made_progress = True
        if not made_progress:
            break
    return selected


def _hybrid_sample(
    grouped: dict[str, list[dict[str, Any]]],
    *,
    size: int,
    rng: random.Random,
    min_per_label: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    remaining: dict[str, list[dict[str, Any]]] = {label: items[:] for label, items in grouped.items()}

    # Phase 1: guarantee a floor per label where possible.
    for label in sorted(remaining):
        take = min(min_per_label, len(remaining[label]), max(0, size - len(selected)))
        for _ in range(take):
            selected.append(remaining[label].pop())
        if len(selected) >= size:
            return selected

    # Phase 2: fill remaining quota with inverse-sqrt frequency weighting.
    while len(selected) < size:
        active = {label: items for label, items in remaining.items() if items}
        if not active:
            break
        labels = list(active.keys())
        weights = [1.0 / math.sqrt(len(active[label])) for label in labels]
        chosen_label = rng.choices(labels, weights=weights, k=1)[0]
        selected.append(active[chosen_label].pop())
    return selected
