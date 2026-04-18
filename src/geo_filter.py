"""
Geo-based species filtering.

Filters classifier predictions to species that actually occur in the
photo's geographic region, eliminating implausible results like
"Australasian Darter" for a North American photo.

Region resolution priority:
  1. GPS coordinates from photo EXIF (auto-detected from catalog)
  2. --region CLI hint (country code or region name)
  3. Default: north_america
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .classifier import Prediction

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data" / "region_species"

# iNaturalist place IDs for broad regions
# Verified against https://api.inaturalist.org/v1/places/{id}
REGION_PLACE_IDS: dict[str, int] = {
    "north_america":   97394,   # North America (continent)
    "central_america": 143141,  # Central America
    "south_america":   97389,   # South America
    "europe":          97391,   # Europe
    "africa":          97392,   # Africa
    "asia":            97395,   # Asia
    "oceania":         97393,   # Oceania
}

# ISO 3166-1 alpha-2 country code → region name
COUNTRY_TO_REGION: dict[str, str] = {
    # North America
    "US": "north_america", "CA": "north_america", "GL": "north_america",
    "PM": "north_america",
    # Central America & Caribbean
    "MX": "central_america", "GT": "central_america", "BZ": "central_america",
    "HN": "central_america", "SV": "central_america", "NI": "central_america",
    "CR": "central_america", "PA": "central_america", "CU": "central_america",
    "JM": "central_america", "HT": "central_america", "DO": "central_america",
    "PR": "central_america", "TT": "central_america",
    # South America
    "CO": "south_america", "VE": "south_america", "GY": "south_america",
    "SR": "south_america", "BR": "south_america", "EC": "south_america",
    "PE": "south_america", "BO": "south_america", "PY": "south_america",
    "CL": "south_america", "AR": "south_america", "UY": "south_america",
    # Europe
    "GB": "europe", "IE": "europe", "FR": "europe", "ES": "europe",
    "PT": "europe", "DE": "europe", "NL": "europe", "BE": "europe",
    "LU": "europe", "CH": "europe", "AT": "europe", "IT": "europe",
    "GR": "europe", "DK": "europe", "SE": "europe", "NO": "europe",
    "FI": "europe", "IS": "europe", "PL": "europe", "CZ": "europe",
    "SK": "europe", "HU": "europe", "RO": "europe", "BG": "europe",
    "HR": "europe", "SI": "europe", "RS": "europe", "BA": "europe",
    "ME": "europe", "AL": "europe", "MK": "europe", "UA": "europe",
    "BY": "europe", "LT": "europe", "LV": "europe", "EE": "europe",
    "MD": "europe", "RU": "europe",
    # Africa
    "MA": "africa", "DZ": "africa", "TN": "africa", "LY": "africa",
    "EG": "africa", "SD": "africa", "ET": "africa", "KE": "africa",
    "TZ": "africa", "UG": "africa", "RW": "africa", "NG": "africa",
    "GH": "africa", "SN": "africa", "ZA": "africa", "ZW": "africa",
    "ZM": "africa", "MZ": "africa", "MG": "africa", "AO": "africa",
    "CM": "africa", "CI": "africa", "ML": "africa", "NE": "africa",
    # Asia
    "IN": "asia", "CN": "asia", "JP": "asia", "KR": "asia", "TH": "asia",
    "VN": "asia", "ID": "asia", "PH": "asia", "MY": "asia", "SG": "asia",
    "MM": "asia", "KH": "asia", "LA": "asia", "BD": "asia", "PK": "asia",
    "AF": "asia", "IR": "asia", "IQ": "asia", "SA": "asia", "AE": "asia",
    "IL": "asia", "TR": "asia", "KZ": "asia", "UZ": "asia", "MN": "asia",
    "TW": "asia", "HK": "asia", "NP": "asia", "LK": "asia",
    # Oceania
    "AU": "oceania", "NZ": "oceania", "PG": "oceania", "FJ": "oceania",
    "SB": "oceania", "VU": "oceania", "WS": "oceania", "TO": "oceania",
}


class GeoFilter:
    """
    Filters Prediction objects to species known to occur in a region.

    If no species list exists for the region, the filter is inactive and
    all predictions pass through unchanged (graceful degradation).
    """

    def __init__(self, region: str, data_dir: Path = DATA_DIR) -> None:
        self.region = region
        self._whitelist: Optional[set[str]] = None
        self._load(data_dir)

    def _load(self, data_dir: Path) -> None:
        path = data_dir / f"{self.region}.json"
        if not path.exists():
            log.warning(
                f"No species list for region '{self.region}' at {path}. "
                f"Run: python -m src.build_region_lists {self.region}"
            )
            return
        try:
            data = json.loads(path.read_text())
            self._whitelist = set(data["labels"])
            log.info(
                f"Geo filter: {self.region} ({len(self._whitelist)} species)"
            )
        except Exception as e:
            log.warning(f"Failed to load region species list {path}: {e}")

    @property
    def active(self) -> bool:
        """True if a species whitelist was loaded successfully."""
        return self._whitelist is not None

    def filter(self, predictions: list["Prediction"]) -> list["Prediction"]:
        """
        Return only predictions whose species occur in this region.
        If the filter is inactive (no list loaded), returns all predictions.
        """
        if self._whitelist is None:
            return predictions
        return [p for p in predictions if p.label in self._whitelist]


def resolve_region_from_coords(lat: float, lon: float) -> str:
    """
    Reverse-geocode (lat, lon) to a region string using offline lookup.
    Falls back to 'north_america' if reverse_geocoder is unavailable.
    """
    try:
        import reverse_geocoder
        results = reverse_geocoder.search([(lat, lon)], verbose=False)
        cc = results[0].get("cc", "")
        region = COUNTRY_TO_REGION.get(cc)
        if region:
            log.debug(f"GPS ({lat:.4f}, {lon:.4f}) → country={cc} → region={region}")
            return region
        # Unknown country: use lowercase country code — may have a custom list
        log.debug(f"GPS ({lat:.4f}, {lon:.4f}) → country={cc} (unmapped, using as region)")
        return cc.lower() if cc else "north_america"
    except ImportError:
        log.warning("reverse_geocoder not installed; defaulting to north_america")
        return "north_america"
    except Exception as e:
        log.warning(f"Reverse geocode failed: {e}; defaulting to north_america")
        return "north_america"


def normalize_region(region: str) -> str:
    """
    Normalize a user-supplied region string to a canonical region name.
    Accepts region names (north_america), country codes (US), or
    country codes in any case (us, Us).
    """
    # Try as-is (e.g. 'north_america', 'europe')
    if region.lower() in REGION_PLACE_IDS:
        return region.lower()
    # Try as ISO country code → region
    cc = region.upper()
    if cc in COUNTRY_TO_REGION:
        return COUNTRY_TO_REGION[cc]
    # Fall through: return lowercased as-is (custom region name)
    return region.lower()
