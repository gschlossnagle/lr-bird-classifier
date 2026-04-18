"""
Build per-region species whitelists from the iNaturalist API.

Fetches all bird species observed in a region and matches them against
the iNat21 model's class labels. Run once per region; results are cached
in data/region_species/{region}.json.

Usage:
    python -m src.build_region_lists north_america
    python -m src.build_region_lists europe
    python -m src.build_region_lists us_northeast
    python -m src.build_region_lists md           # individual US state
    python -m src.build_region_lists US           # ISO country code
    python -m src.build_region_lists --all        # build all standard regions + all US states
    python -m src.build_region_lists --place-id 6803 --output australia
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .geo_filter import REGION_PLACE_IDS, normalize_region, US_STATE_ABBREV
from .taxonomy import get_synonyms, parse_label

log = logging.getLogger(__name__)

INAT_API = "https://api.inaturalist.org/v1"
AVES_TAXON_ID = 3          # iNaturalist taxon ID for Birds (Aves)
PER_PAGE = 500
RATE_LIMIT_DELAY = 1.0     # seconds between API pages
DATA_DIR = Path(__file__).parent.parent / "data" / "region_species"

# Named regions built first by --all (continents + US sub-regions).
# After these, --all also builds individual US state whitelists (all 50 states + DC).
STANDARD_REGIONS = [
    "north_america", "central_america", "south_america",
    "europe", "africa", "asia", "oceania",
    "canada",
    "alaska", "hawaii",
    "us_pacific", "us_mountain", "us_southwest",
    "us_midwest", "us_southeast", "us_northeast",
]


def fetch_bird_species_for_place(place_id: int) -> set[str]:
    """
    Fetch all bird species observed in a place from iNaturalist.
    Returns a set of 'Genus species' scientific name strings.
    """
    sci_names: set[str] = set()
    page = 1

    while True:
        log.info(f"  Fetching page {page} (place_id={place_id})...")
        try:
            resp = requests.get(
                f"{INAT_API}/observations/species_counts",
                params={
                    "place_id": place_id,
                    "taxon_id": AVES_TAXON_ID,
                    "quality_grade": "research",
                    "per_page": PER_PAGE,
                    "page": page,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.error(f"API request failed: {e}")
            break

        results = data.get("results", [])
        total = data.get("total_results", 0)

        for entry in results:
            taxon = entry.get("taxon", {})
            name = taxon.get("name", "")
            rank = taxon.get("rank", "")
            if rank == "species" and name:
                sci_names.add(name)

        log.info(f"    Got {len(results)} species (total so far: {len(sci_names)} / {total})")

        if len(results) < PER_PAGE or (page * PER_PAGE) >= total:
            break
        page += 1
        time.sleep(RATE_LIMIT_DELAY)

    return sci_names


def fetch_bird_species_for_region(place_ids: int | list[int]) -> set[str]:
    """
    Fetch all bird species for a region that may span multiple iNat places.

    When *place_ids* is a list, fetches each place separately and returns the
    union of all species — necessary for multi-state US sub-regions which have
    no single iNat place.
    """
    if isinstance(place_ids, int):
        return fetch_bird_species_for_place(place_ids)

    combined: set[str] = set()
    for i, pid in enumerate(place_ids):
        log.info(f"  Place {i + 1}/{len(place_ids)} (id={pid})...")
        combined |= fetch_bird_species_for_place(pid)
        if i < len(place_ids) - 1:
            time.sleep(RATE_LIMIT_DELAY)
    return combined


def match_to_labels(
    sci_names: set[str],
    class_to_idx: dict[str, int],
) -> list[str]:
    """
    Match 'Genus species' strings to iNat21 model labels.
    Labels encode genus+species as the last two underscore-separated parts.

    Uses a synonym map to handle cases where the iNat21 model uses older
    taxonomy names (e.g. Phalacrocorax auritus) that iNat has since
    reclassified (e.g. Nannopterum auritum).
    """
    # Primary index: old model sci name → label
    label_by_sci: dict[str, str] = {}
    for label in class_to_idx:
        parsed = parse_label(label)
        if parsed.get("is_bird"):
            label_by_sci[parsed["sci_name"]] = label

    # Secondary index: current iNat name → label (for reclassified species)
    synonyms = get_synonyms()   # old_name → current_name
    current_to_label: dict[str, str] = {}
    for old_name, current_name in synonyms.items():
        if old_name in label_by_sci:
            current_to_label[current_name] = label_by_sci[old_name]

    synonym_hits = 0
    matched: list[str] = []
    for s in sci_names:
        if s in label_by_sci:
            matched.append(label_by_sci[s])
        elif s in current_to_label:
            matched.append(current_to_label[s])
            synonym_hits += 1

    log.info(
        f"  Matched {len(matched)} / {len(sci_names)} iNat species to model labels"
        + (f" ({synonym_hits} via taxonomy synonyms)" if synonym_hits else "")
    )
    return sorted(matched)


def lookup_place_id(country_or_region: str) -> int | None:
    """Look up a place_id for a country name via iNaturalist autocomplete."""
    try:
        resp = requests.get(
            f"{INAT_API}/places/autocomplete",
            params={"q": country_or_region, "per_page": 5},
            timeout=10,
        )
        resp.raise_for_status()
        for place in resp.json().get("results", []):
            if place.get("place_type") == 12:   # 12 = Country in iNat
                log.info(f"  Found place: {place['display_name']} (id={place['id']})")
                return place["id"]
    except Exception as e:
        log.warning(f"Place lookup failed: {e}")
    return None


def build_region(region: str, place_id: int | list[int], class_to_idx: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{region}.json"

    place_desc = place_id if isinstance(place_id, int) else f"[{len(place_id)} places]"
    log.info(f"Building species list for '{region}' (place_id={place_desc})...")
    sci_names = fetch_bird_species_for_region(place_id)
    labels = match_to_labels(sci_names, class_to_idx)

    data = {
        "region": region,
        "place_id": place_id,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_inat_species": len(sci_names),
        "matched_labels": len(labels),
        "labels": labels,
    }
    out_path.write_text(json.dumps(data, indent=2))
    log.info(f"  Saved {len(labels)} labels → {out_path}")


def load_model_classes() -> dict[str, int]:
    """Load class_to_idx from the default classifier model."""
    import torch
    from birder.common import fs_ops
    log.info("Loading model class list...")
    _, (class_to_idx, *_) = fs_ops.load_model(
        torch.device("cpu"),
        "rope_vit_reg4_b14",
        tag="capi-inat21",
        inference=True,
    )
    return class_to_idx


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build per-region bird species whitelists from iNaturalist."
    )
    parser.add_argument(
        "region",
        nargs="?",
        help="Region name (north_america, europe, ...) or ISO country code (US, GB, ...)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Build lists for all standard regions",
    )
    parser.add_argument(
        "--place-id",
        type=int,
        default=None,
        help="Override iNaturalist place_id (use with --output)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output filename stem when using --place-id (e.g. 'australia')",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    class_to_idx = load_model_classes()

    if args.all:
        # Phase 1: continent-level and US sub-region whitelists
        for region in STANDARD_REGIONS:
            place_id = REGION_PLACE_IDS[region]
            build_region(region, place_id, class_to_idx)
            time.sleep(2.0)
        # Phase 2: individual US state whitelists (all 50 states + DC)
        log.info("Building individual US state whitelists...")
        for abbrev in sorted(US_STATE_ABBREV):
            code = abbrev.lower()
            place_id = REGION_PLACE_IDS[code]
            build_region(code, place_id, class_to_idx)
            time.sleep(2.0)
        return 0

    if args.place_id:
        output = args.output or f"place_{args.place_id}"
        build_region(output, args.place_id, class_to_idx)
        return 0

    if not args.region:
        parser.print_help()
        return 1

    # Normalize region name or country code
    region = normalize_region(args.region)
    place_id: int | list[int] | None = REGION_PLACE_IDS.get(region)

    if place_id is None:
        # Try iNat place autocomplete (single place lookup, returns int)
        log.info(f"No known place_id for '{region}', trying iNat autocomplete...")
        resolved = lookup_place_id(args.region)
        if resolved is None:
            log.error(
                f"Could not find a place_id for '{args.region}'. "
                f"Try --place-id <id> with an explicit iNaturalist place ID."
            )
            return 1
        place_id = resolved

    build_region(region, place_id, class_to_idx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
