"""
User-level configuration for lr-bird-classifier.

Reads ~/.lrbc-config (JSON) and exposes the values as a plain dict.
All keys are optional; missing keys return None so callers can treat
them identically to "flag not supplied on the CLI".

Supported keys
--------------
catalog        str   — default path to the .lrcat file
region         str   — default region hint (e.g. "us_northeast", "US")
formats        str   — default comma-separated format list (e.g. "RAW,DNG")
model          str   — default model in "network/tag" form
min_confidence float — default minimum confidence threshold (0–1)

Example ~/.lrbc-config
----------------------
{
    "catalog": "/Volumes/Photos/My Catalog.lrcat",
    "region": "us_northeast",
    "formats": "RAW,DNG",
    "model": "rope_vit_reg4_b14/capi-inat21",
    "min_confidence": 0.25
}
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

CONFIG_PATH = Path.home() / ".lrbc-config"

_KNOWN_KEYS = {"catalog", "region", "formats", "model", "min_confidence"}

log = logging.getLogger(__name__)


def load() -> dict[str, Any]:
    """
    Load ~/.lrbc-config and return its contents as a dict.

    Returns an empty dict if the file does not exist.
    Logs a warning (but does not raise) on parse errors or unknown keys.
    """
    if not CONFIG_PATH.exists():
        return {}

    try:
        data = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        log.warning(f"Could not parse {CONFIG_PATH}: {e} — ignoring config")
        return {}

    if not isinstance(data, dict):
        log.warning(f"{CONFIG_PATH} must be a JSON object — ignoring config")
        return {}

    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        log.warning(f"{CONFIG_PATH}: unknown key(s): {', '.join(sorted(unknown))}")

    return {k: v for k, v in data.items() if k in _KNOWN_KEYS}
