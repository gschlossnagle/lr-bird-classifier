"""
iNat21 class label → common name mapping.

Class labels in the iNat21 birder models look like:
  04402_Animalia_Chordata_Aves_Pelecaniformes_Threskiornithidae_Platalea_ajaja

This module parses those labels and resolves common names via the
iNaturalist API, caching results in data/taxonomy_cache.json.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

CACHE_PATH = Path(__file__).parent.parent / "data" / "taxonomy_cache.json"
INAT_API_URL = "https://api.inaturalist.org/v1/taxa"
RATE_LIMIT_DELAY = 0.5   # seconds between API calls to be polite
REQUEST_TIMEOUT = 10     # seconds


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------

def parse_label(label: str) -> dict:
    """
    Parse an iNat21 class label into its components.

    Returns a dict with keys:
      inat_id, kingdom, phylum, class_, order, family, genus, species,
      sci_name (e.g. "Platalea ajaja"), is_bird
    """
    parts = label.split("_")
    # Format: {id}_{kingdom}_{phylum}_{class}_{order}_{family}_{genus}_{species}
    if len(parts) < 8:
        return {"sci_name": label, "is_bird": False}

    return {
        "inat_id": parts[0],
        "kingdom": parts[1],
        "phylum": parts[2],
        "class_": parts[3],
        "order": parts[4],
        "family": parts[5],
        "genus": parts[6],
        "species": parts[7],
        "sci_name": f"{parts[6]} {parts[7]}",
        "is_bird": parts[3] == "Aves",
    }


# ---------------------------------------------------------------------------
# iNaturalist API lookup
# ---------------------------------------------------------------------------

def _fetch_common_name(sci_name: str) -> Optional[str]:
    """Query iNaturalist API for the preferred common name of a species."""
    try:
        resp = requests.get(
            INAT_API_URL,
            params={"q": sci_name, "rank": "species", "per_page": 1},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("preferred_common_name")
    except Exception as e:
        log.warning(f"iNat API lookup failed for '{sci_name}': {e}")
    return None


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _load_cache() -> dict[str, str]:
    if CACHE_PATH.exists():
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_label_map(
    class_to_idx: dict[str, int],
    *,
    birds_only: bool = False,
    fetch_missing: bool = True,
) -> dict[str, str]:
    """
    Build a mapping from iNat21 class label → display name (common or scientific).

    Args:
        class_to_idx: The class_to_idx dict from a birder model.
        birds_only: If True, only include Aves classes.
        fetch_missing: If True, query iNaturalist API for any labels not in cache.

    Returns:
        Dict mapping each label to its best available display name.
    """
    cache = _load_cache()
    label_map: dict[str, str] = {}
    to_fetch: list[tuple[str, str]] = []  # (label, sci_name)

    for label in class_to_idx:
        parsed = parse_label(label)
        if birds_only and not parsed.get("is_bird"):
            continue

        sci_name = parsed.get("sci_name", label)
        if sci_name in cache:
            label_map[label] = cache[sci_name] or sci_name
        else:
            to_fetch.append((label, sci_name))
            label_map[label] = sci_name  # placeholder until fetched

    if fetch_missing and to_fetch:
        log.info(f"Fetching common names for {len(to_fetch)} species from iNaturalist...")
        for i, (label, sci_name) in enumerate(to_fetch):
            common = _fetch_common_name(sci_name)
            cache[sci_name] = common or ""
            label_map[label] = common or sci_name
            if (i + 1) % 50 == 0:
                log.info(f"  {i + 1}/{len(to_fetch)} fetched, saving cache...")
                _save_cache(cache)
            time.sleep(RATE_LIMIT_DELAY)
        _save_cache(cache)
        log.info("Done fetching common names.")

    return label_map


def get_common_name(label: str, label_map: Optional[dict[str, str]] = None) -> str:
    """
    Get the display name for a single iNat21 label.

    If label_map is not provided, falls back to parsing the scientific name
    from the label itself (no API call).
    """
    if label_map and label in label_map:
        return label_map[label]
    parsed = parse_label(label)
    return parsed.get("sci_name", label)
