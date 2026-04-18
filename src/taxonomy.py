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
SYNONYMS_CACHE_PATH = Path(__file__).parent.parent / "data" / "taxonomy_synonyms.json"
ORDER_CACHE_PATH = Path(__file__).parent.parent / "data" / "taxonomy_order_names.json"
INAT_API_URL = "https://api.inaturalist.org/v1/taxa"
RATE_LIMIT_DELAY = 0.5   # seconds between API calls to be polite
REQUEST_TIMEOUT = 10     # seconds
AVES_TAXON_ID = 3        # iNaturalist taxon ID for class Aves (Birds)


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

def _fetch_taxon(sci_name: str) -> tuple[Optional[str], Optional[str]]:
    """
    Query iNaturalist API for a species.

    Returns:
        (common_name, current_sci_name) — either may be None on failure.
        current_sci_name differs from sci_name when iNat has reclassified the
        species (e.g. Phalacrocorax auritus → Nannopterum auritum).
    """
    try:
        resp = requests.get(
            INAT_API_URL,
            params={"q": sci_name, "rank": "species", "per_page": 1},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            common = results[0].get("preferred_common_name")
            current = results[0].get("name")   # current accepted sci name
            return common, current
    except Exception as e:
        log.warning(f"iNat API lookup failed for '{sci_name}': {e}")
    return None, None


def _fetch_common_name(sci_name: str) -> Optional[str]:
    """Query iNaturalist API for the preferred common name of a species."""
    common, _ = _fetch_taxon(sci_name)
    return common


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


def _load_synonyms() -> dict[str, str]:
    """Load old_sci_name → current_inat_sci_name synonym map."""
    if SYNONYMS_CACHE_PATH.exists():
        with open(SYNONYMS_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_synonyms(synonyms: dict[str, str]) -> None:
    SYNONYMS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SYNONYMS_CACHE_PATH, "w") as f:
        json.dump(synonyms, f, indent=2, sort_keys=True)


def get_synonyms() -> dict[str, str]:
    """Return the cached old→current sci name synonym map."""
    return _load_synonyms()


# ---------------------------------------------------------------------------
# Order common name cache
# ---------------------------------------------------------------------------

def _load_order_cache() -> dict[str, str]:
    if ORDER_CACHE_PATH.exists():
        with open(ORDER_CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_order_cache(cache: dict[str, str]) -> None:
    ORDER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ORDER_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def _fetch_order_common_name(order: str) -> Optional[str]:
    """Query iNaturalist API for the preferred common name of a taxonomic order."""
    try:
        resp = requests.get(
            INAT_API_URL,
            params={"q": order, "rank": "order", "per_page": 1},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results:
            return results[0].get("preferred_common_name")
    except Exception as e:
        log.warning(f"iNat order lookup failed for '{order}': {e}")
    return None


def hyphenate_common_name(name: str) -> str:
    """
    Convert a multi-part common name to a hyphenated keyword tag.

    Strips parenthetical notes, splits on commas and conjunctions, then
    joins each segment with hyphens (replacing internal spaces too).

    Examples:
        "Loons"                              → "Loons"
        "Hawks, Eagles, and Kites"           → "Hawks-Eagles-Kites"
        "Shorebirds and Allies"              → "Shorebirds-Allies"
        "Nightjars and Allies"               → "Nightjars-Allies"
        "Pigeons and Doves"                  → "Pigeons-Doves"
        "New World Warblers"                 → "New-World-Warblers"
    """
    import re
    # Drop parenthetical notes like "(Owls)" or "(part)"
    name = re.sub(r"\s*\([^)]*\)", "", name).strip()
    # Split on commas and inline conjunctions surrounded by spaces
    parts = re.split(r",\s*|\s+(?:and|&)\s+", name)
    # Strip leading "and"/"&" from segments that survive comma-splitting
    # e.g. ["Hawks", "Eagles", "and Kites"] → ["Hawks", "Eagles", "Kites"]
    leading_conj = re.compile(r"^(?:and|&)\s+", re.IGNORECASE)
    cleaned = []
    for p in parts:
        p = leading_conj.sub("", p.strip())
        if p:
            # Replace internal spaces with hyphens (e.g. "New World" → "New-World")
            cleaned.append(p.replace(" ", "-"))
    return "-".join(cleaned)


def get_order_display_name(order: str) -> str:
    """
    Return the display name for a bird order as a hyphenated tag.

    Looks up the iNaturalist preferred common name for the order, applies
    hyphenation, and caches the result in data/taxonomy_order_names.json.
    Falls back to the scientific order name if no common name is found.

    Examples:
        "Gaviiformes"    → "Loons"
        "Accipitriformes"→ "Hawks-Eagles-Kites"
        "Passeriformes"  → "Perching-Birds"  (or whatever iNat returns)
    """
    cache = _load_order_cache()
    if order in cache:
        return cache[order]

    log.debug(f"Fetching common name for order '{order}' from iNat...")
    common = _fetch_order_common_name(order)
    if common:
        display = hyphenate_common_name(common)
        log.info(f"Order: {order} → '{display}' (from '{common}')")
    else:
        display = order   # fallback: use scientific order name
        log.warning(f"No common name found for order '{order}'; using scientific name")

    cache[order] = display
    _save_order_cache(cache)
    return display


# ---------------------------------------------------------------------------
# Bulk bird name fetch
# ---------------------------------------------------------------------------

def fetch_all_bird_common_names(
    class_to_idx: Optional[dict[str, int]] = None,
) -> None:
    """
    Populate the taxonomy cache with common names for all bird species.

    Uses a paginated bulk query against the iNaturalist Aves taxon rather
    than per-species lookups, cutting ~10 000 individual API calls down to
    ~20 pages.  Run once via ``Classifier.fetch_common_names()``; subsequent
    calls only refresh entries that are missing or were previously empty.

    The cache (data/taxonomy_cache.json) is written incrementally so an
    interrupted run can be resumed without re-fetching already-cached pages.
    Also records taxonomy synonyms for species iNat has reclassified.

    Args:
        class_to_idx: Optional model class→index dict.  When provided, any
            bird species present in the model but absent from the bulk Aves
            results (iNat API caps pagination at 10 000 results) will be
            fetched individually as a fallback.
    """
    cache = _load_cache()
    synonyms = _load_synonyms()

    page = 1
    per_page = 500
    total_fetched = 0

    log.info("Bulk-fetching bird common names from iNaturalist (class Aves)…")

    while True:
        log.info(f"  Page {page} …")
        try:
            resp = requests.get(
                INAT_API_URL,
                params={
                    "taxon_id": AVES_TAXON_ID,
                    "rank": "species",
                    "per_page": per_page,
                    "page": page,
                    "order_by": "id",
                },
                timeout=30,
            )
            # iNat caps paginated results at 10 000 (500 × 20 pages).
            # Requesting page 21+ returns 403 — treat as a normal end-of-results.
            if resp.status_code == 403:
                log.info(f"  Reached iNat pagination limit at page {page} (10 000 results cap) — switching to fallback.")
                break
            resp.raise_for_status()
            data = resp.json()
            taxa = data.get("results", [])
            api_total = data.get("total_results", 0)
        except Exception as e:
            log.error(f"  Bulk taxa fetch failed on page {page}: {e}")
            break

        for taxon in taxa:
            sci_name = taxon.get("name", "")
            if not sci_name:
                continue
            cache[sci_name] = taxon.get("preferred_common_name") or ""
            total_fetched += 1

        log.info(f"    Got {len(taxa)} species (page {page}, {total_fetched}/{api_total} so far)")
        _save_cache(cache)

        if len(taxa) < per_page or (page * per_page) >= api_total:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    # --- Fallback: per-species lookup for model labels not found in bulk fetch ---
    # The iNat API hard-caps paginated results at 10 000.  Any bird label in the
    # model whose sci_name is still absent from the cache after the bulk pass gets
    # a targeted individual lookup here.
    if class_to_idx is not None:
        missing = [
            parsed["sci_name"]
            for label in class_to_idx
            for parsed in [parse_label(label)]
            if parsed.get("is_bird") and parsed["sci_name"] not in cache
        ]
        if missing:
            log.info(f"  Fallback: fetching {len(missing)} model species not in bulk results…")
            for i, sci in enumerate(missing):
                common, current = _fetch_taxon(sci)
                cache[sci] = common or ""
                if current and current != sci:
                    synonyms[sci] = current
                if (i + 1) % 20 == 0:
                    _save_cache(cache)
                    _save_synonyms(synonyms)
                time.sleep(RATE_LIMIT_DELAY)

    _save_cache(cache)
    _save_synonyms(synonyms)
    log.info(
        f"Done. {sum(1 for v in cache.values() if v)} species with common names "
        f"({total_fetched} from bulk Aves fetch)."
    )


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
        synonyms = _load_synonyms()
        log.info(f"Fetching common names for {len(to_fetch)} species from iNaturalist...")
        for i, (label, sci_name) in enumerate(to_fetch):
            common, current = _fetch_taxon(sci_name)
            cache[sci_name] = common or ""
            label_map[label] = common or sci_name
            # Record synonym if iNat has reclassified this species
            if current and current != sci_name:
                synonyms[sci_name] = current
                log.debug(f"Synonym: {sci_name} → {current}")
            if (i + 1) % 50 == 0:
                log.info(f"  {i + 1}/{len(to_fetch)} fetched, saving cache...")
                _save_cache(cache)
                _save_synonyms(synonyms)
            time.sleep(RATE_LIMIT_DELAY)
        _save_cache(cache)
        _save_synonyms(synonyms)
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


def get_group_tag(common_name: str) -> str:
    """
    Derive a broad group tag from a common name by taking the last
    space-separated word.

    Examples:
        "Bald Eagle"               → "Eagle"
        "Double-Crested Cormorant" → "Cormorant"
        "Barn Swallow"             → "Swallow"
        "Roseate Spoonbill"        → "Spoonbill"
        "Great Blue Heron"         → "Heron"
        "Northern Cardinal"        → "Cardinal"
    """
    if not common_name:
        return common_name
    return common_name.rsplit(" ", 1)[-1]
