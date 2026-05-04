"""
Helpers for working with locally cached eBird taxonomy data.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent.parent / "data" / "ebird_species_codes.json"
EBIRD_LABEL_PREFIX = "ebird:species:"


def _normalize_sci_name_token(sci_name: str) -> str:
    return "_".join(sci_name.strip().split())


def build_ebird_label(*, species_code: str, sci_name: str) -> str:
    return f"{EBIRD_LABEL_PREFIX}{species_code}:{_normalize_sci_name_token(sci_name)}"


def is_ebird_label(label: str) -> bool:
    return label.startswith(EBIRD_LABEL_PREFIX)


@lru_cache(maxsize=1)
def load_ebird_species() -> dict[str, dict[str, str]]:
    data = json.loads(DATA_PATH.read_text())
    result: dict[str, dict[str, str]] = {}
    for sci_name, payload in data.items():
        if payload.get("category") != "species":
            continue
        species_code = payload.get("species_code")
        common_name = payload.get("primary_com_name")
        if not species_code or not common_name:
            continue
        result[sci_name] = {
            "sci_name": sci_name,
            "common_name": common_name,
            "species_code": species_code,
        }
    return result


@lru_cache(maxsize=1)
def load_ebird_by_label() -> dict[str, dict[str, str]]:
    return {
        build_ebird_label(species_code=row["species_code"], sci_name=row["sci_name"]): row
        for row in load_ebird_species().values()
    }

