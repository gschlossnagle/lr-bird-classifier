"""
eBird / Macaulay reference lookup helpers for the review UI.

This module does not download or store media bytes. It only:

- resolves canonical species identities to eBird species codes
- fetches one species-filtered media search page
- extracts the first visible Macaulay asset id from that HTML
- builds remote URLs for linking and display
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import logging
from pathlib import Path
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

SPECIES_CODES_PATH = Path(__file__).parent.parent / "data" / "ebird_species_codes.json"
REQUEST_TIMEOUT = 10
USER_AGENT = "lr-bird-classifier/1.0"

EBIRD_SPECIES_URL = "https://ebird.org/species/{species_code}"
MEDIA_SEARCH_URL = "https://media.ebird.org/catalog?mediaType=photo&taxonCode={species_code}&view=grid"
MACAULAY_ASSET_URL = "https://macaulaylibrary.org/asset/{asset_id}"
MACAULAY_ASSET_IMAGE_URL = "https://cdn.download.ams.birds.cornell.edu/api/v2/asset/{asset_id}/{size}"

ASSET_ID_RE = re.compile(r'data-asset-id="(\d+)"')
OG_IMAGE_RE = re.compile(r"https://cdn\.download\.ams\.birds\.cornell\.edu/api/v2/asset/(\d+)/1200")


@dataclass(frozen=True)
class EbirdReference:
    truth_common_name: str
    truth_sci_name: str
    species_code: str
    species_url: str
    media_search_url: str
    macaulay_asset_id: str | None = None

    @property
    def macaulay_asset_url(self) -> str | None:
        if self.macaulay_asset_id is None:
            return None
        return MACAULAY_ASSET_URL.format(asset_id=self.macaulay_asset_id)

    @property
    def preview_image_url(self) -> str | None:
        if self.macaulay_asset_id is None:
            return None
        return MACAULAY_ASSET_IMAGE_URL.format(asset_id=self.macaulay_asset_id, size=1200)


def _normalize_name(text: str) -> str:
    return " ".join(text.strip().lower().split())


@lru_cache(maxsize=1)
def _load_species_codes() -> tuple[dict[str, dict[str, str]], dict[str, list[dict[str, str]]]]:
    with SPECIES_CODES_PATH.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    by_sci_name: dict[str, dict[str, str]] = {}
    by_common_name: dict[str, list[dict[str, str]]] = {}
    for sci_name, record in payload.items():
        if sci_name.startswith("__"):
            continue
        by_sci_name[sci_name] = record
        normalized_common = _normalize_name(record["primary_com_name"])
        by_common_name.setdefault(normalized_common, []).append(record | {"sci_name": sci_name})
    return by_sci_name, by_common_name


def lookup_species_code(*, truth_sci_name: str, truth_common_name: str) -> Optional[str]:
    by_sci_name, by_common_name = _load_species_codes()
    record = by_sci_name.get(truth_sci_name)
    if record is not None:
        return record["species_code"]

    common_matches = by_common_name.get(_normalize_name(truth_common_name), [])
    if len(common_matches) == 1:
        return common_matches[0]["species_code"]

    return None


def parse_media_search_asset_id(html_text: str) -> str | None:
    match = ASSET_ID_RE.search(html_text)
    if match is not None:
        return match.group(1)
    match = OG_IMAGE_RE.search(html_text)
    if match is not None:
        return match.group(1)
    return None


@lru_cache(maxsize=256)
def lookup_reference_asset_id(species_code: str) -> str | None:
    url = MEDIA_SEARCH_URL.format(species_code=species_code)
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except Exception as exc:
        log.warning("Failed to fetch eBird media search for %s: %s", species_code, exc)
        return None
    return parse_media_search_asset_id(response.text)


def resolve_reference(*, truth_sci_name: str, truth_common_name: str) -> EbirdReference | None:
    species_code = lookup_species_code(
        truth_sci_name=truth_sci_name,
        truth_common_name=truth_common_name,
    )
    if species_code is None:
        return None
    return EbirdReference(
        truth_common_name=truth_common_name,
        truth_sci_name=truth_sci_name,
        species_code=species_code,
        species_url=EBIRD_SPECIES_URL.format(species_code=species_code),
        media_search_url=MEDIA_SEARCH_URL.format(species_code=species_code),
        macaulay_asset_id=lookup_reference_asset_id(species_code),
    )
