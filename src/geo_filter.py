"""
Geo-based species filtering.

Filters classifier predictions to species that actually occur in the
photo's geographic region, eliminating implausible results like
"Australasian Darter" for a North American photo.

Region resolution priority:
  1. GPS coordinates from photo EXIF (auto-detected from catalog)
  2. --region CLI hint (country code, US state abbreviation, or region name)
  3. Default: north_america

Supported region names
----------------------
any                   No filtering — all model species pass through
north_america         All of North America
central_america       Central America & Caribbean
south_america         South America
europe                Europe
africa                Africa
asia                  Asia
oceania               Oceania
canada                All of Canada

US sub-regions (auto-selected from GPS; also usable as --region hints):
  alaska              Alaska
  hawaii              Hawaii
  us_pacific          CA, OR, WA
  us_mountain         ID, MT, WY, NV, UT, CO
  us_southwest        AZ, NM, TX, OK
  us_midwest          ND, SD, MN, NE, KS, IA, MO, WI, IL, MI, IN, OH, KY
  us_southeast        FL, GA, SC, NC, AL, MS, LA, TN, AR
  us_northeast        ME, NH, VT, MA, RI, CT, NY, NJ, PA, MD, DE, VA, WV, DC

Individual US states (build a state-specific whitelist for maximum precision):
  Use the two-letter postal abbreviation, e.g. --region MD, --region FL
  Falls back to the parent sub-region list if no state-specific list exists.
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

# ---------------------------------------------------------------------------
# iNaturalist place IDs
# ---------------------------------------------------------------------------

# US state name (as returned by reverse_geocoder admin1) → iNat place ID
US_STATE_PLACE_IDS: dict[str, int] = {
    "Alabama": 19, "Alaska": 6, "Arizona": 40, "Arkansas": 36,
    "California": 14, "Colorado": 34, "Connecticut": 49,
    "Delaware": 4, "District of Columbia": 5, "Florida": 7539,
    "Georgia": 23, "Hawaii": 11, "Idaho": 22, "Illinois": 35,
    "Indiana": 20, "Iowa": 24, "Kansas": 25, "Kentucky": 26,
    "Louisiana": 27, "Maine": 17, "Maryland": 39, "Massachusetts": 2,
    "Michigan": 29, "Minnesota": 38, "Mississippi": 37, "Missouri": 28,
    "Montana": 16, "Nebraska": 3, "Nevada": 50, "New Hampshire": 41,
    "New Jersey": 51, "New Mexico": 9, "New York": 48,
    "North Carolina": 30, "North Dakota": 13, "Ohio": 31, "Oklahoma": 12,
    "Oregon": 10, "Pennsylvania": 42, "Rhode Island": 8,
    "South Carolina": 43, "South Dakota": 44, "Tennessee": 45,
    "Texas": 18, "Utah": 52, "Vermont": 47, "Virginia": 7,
    "Washington": 46, "West Virginia": 33, "Wisconsin": 32, "Wyoming": 15,
}

# US two-letter postal code → full state name
US_STATE_ABBREV: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
    "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}

# US state name → sub-region (for GPS-based auto-detection)
US_STATE_TO_SUBREGION: dict[str, str] = {
    "Alaska": "alaska",
    "Hawaii": "hawaii",
    # Pacific
    "California": "us_pacific", "Oregon": "us_pacific", "Washington": "us_pacific",
    # Mountain
    "Idaho": "us_mountain", "Montana": "us_mountain", "Wyoming": "us_mountain",
    "Nevada": "us_mountain", "Utah": "us_mountain", "Colorado": "us_mountain",
    # Southwest
    "Arizona": "us_southwest", "New Mexico": "us_southwest",
    "Texas": "us_southwest", "Oklahoma": "us_southwest",
    # Midwest
    "North Dakota": "us_midwest", "South Dakota": "us_midwest",
    "Minnesota": "us_midwest", "Nebraska": "us_midwest", "Kansas": "us_midwest",
    "Iowa": "us_midwest", "Missouri": "us_midwest", "Wisconsin": "us_midwest",
    "Illinois": "us_midwest", "Michigan": "us_midwest", "Indiana": "us_midwest",
    "Ohio": "us_midwest", "Kentucky": "us_midwest",
    # Southeast
    "Florida": "us_southeast", "Georgia": "us_southeast",
    "South Carolina": "us_southeast", "North Carolina": "us_southeast",
    "Alabama": "us_southeast", "Mississippi": "us_southeast",
    "Louisiana": "us_southeast", "Tennessee": "us_southeast",
    "Arkansas": "us_southeast",
    # Northeast
    "Maine": "us_northeast", "New Hampshire": "us_northeast",
    "Vermont": "us_northeast", "Massachusetts": "us_northeast",
    "Rhode Island": "us_northeast", "Connecticut": "us_northeast",
    "New York": "us_northeast", "New Jersey": "us_northeast",
    "Pennsylvania": "us_northeast", "Maryland": "us_northeast",
    "Delaware": "us_northeast", "Virginia": "us_northeast",
    "West Virginia": "us_northeast", "District of Columbia": "us_northeast",
}

# Lowercase US state code → parent sub-region (for GeoFilter fallback)
_STATE_CODE_TO_SUBREGION: dict[str, str] = {
    code.lower(): US_STATE_TO_SUBREGION[name]
    for code, name in US_STATE_ABBREV.items()
}

# iNaturalist place IDs for built-in regions.
# Values may be a single int or a list of ints (unioned when building whitelists).
REGION_PLACE_IDS: dict[str, int | list[int]] = {
    # Continents / macro-regions
    "north_america":   97394,
    "central_america": 143141,
    "south_america":   97389,
    "europe":          97391,
    "africa":          97392,
    "asia":            97395,
    "oceania":         97393,
    "canada":          6712,

    # US sub-regions (union of constituent state place IDs)
    "alaska":       6,
    "hawaii":       11,
    "us_pacific":   [14, 10, 46],           # CA, OR, WA
    "us_mountain":  [22, 16, 15, 50, 52, 34],  # ID, MT, WY, NV, UT, CO
    "us_southwest": [40, 9, 18, 12],        # AZ, NM, TX, OK
    "us_midwest":   [13, 44, 38, 3, 25, 24, 28, 32, 35, 29, 20, 31, 26],
    #                ND, SD, MN, NE, KS, IA, MO, WI, IL, MI, IN, OH, KY
    "us_southeast": [7539, 23, 43, 30, 19, 37, 27, 45, 36],
    #                FL,   GA, SC, NC, AL, MS, LA, TN, AR
    "us_northeast": [17, 41, 47, 2, 8, 49, 48, 51, 42, 39, 4, 7, 33, 5],
    #                ME, NH, VT,MA,RI, CT, NY, NJ, PA, MD,DE,VA, WV,DC

    # Individual US states (lowercase two-letter postal codes)
    **{code.lower(): US_STATE_PLACE_IDS[name] for code, name in US_STATE_ABBREV.items()},
}

# ISO 3166-1 alpha-2 country code → region name (non-US/CA countries)
COUNTRY_TO_REGION: dict[str, str] = {
    # North America (non-US/CA handled by state detection below)
    "US": "north_america", "CA": "canada", "GL": "north_america",
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

    Special values:
      'any'  — filter is always inactive; all predictions pass through.

    If no species list exists for the region (and no fallback applies),
    the filter is inactive and all predictions pass through (graceful degradation).
    """

    def __init__(self, region: str, data_dir: Path = DATA_DIR) -> None:
        self.region = region
        self._whitelist: Optional[set[str]] = None
        if region != "any":
            self._load(data_dir)

    def _load(self, data_dir: Path) -> None:
        path = data_dir / f"{self.region}.json"

        # For US state codes, fall back to the parent sub-region if needed
        fallback_region: Optional[str] = None
        if not path.exists():
            fallback_region = _STATE_CODE_TO_SUBREGION.get(self.region)
            if fallback_region:
                fallback_path = data_dir / f"{fallback_region}.json"
                if fallback_path.exists():
                    log.info(
                        f"No state-specific list for '{self.region}'; "
                        f"falling back to '{fallback_region}'"
                    )
                    path = fallback_path

        if not path.exists():
            log.warning(
                f"No species list for region '{self.region}' at {path}. "
                f"Run: python -m src.build_region_lists {self.region}"
            )
            return

        try:
            data = json.loads(path.read_text())
            self._whitelist = set(data["labels"])
            loaded_region = data.get("region", self.region)
            log.info(
                f"Geo filter: {loaded_region} ({len(self._whitelist)} species)"
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

    For US coordinates: resolves to the state-level sub-region
    (e.g. us_northeast, us_pacific) using the state name from reverse_geocoder.
    For other countries: falls back to the broad continental region.
    """
    try:
        import reverse_geocoder
        results = reverse_geocoder.search([(lat, lon)], verbose=False)
        cc = results[0].get("cc", "")
        admin1 = results[0].get("admin1", "")  # state or province name

        if cc == "US":
            sub = US_STATE_TO_SUBREGION.get(admin1)
            if sub:
                log.debug(
                    f"GPS ({lat:.4f}, {lon:.4f}) → {admin1}, US → {sub}"
                )
                return sub
            log.debug(
                f"GPS ({lat:.4f}, {lon:.4f}) → {admin1}, US (unmapped state) → north_america"
            )
            return "north_america"

        if cc == "CA":
            log.debug(f"GPS ({lat:.4f}, {lon:.4f}) → {admin1}, CA → canada")
            return "canada"

        region = COUNTRY_TO_REGION.get(cc)
        if region:
            log.debug(f"GPS ({lat:.4f}, {lon:.4f}) → country={cc} → region={region}")
            return region

        log.debug(
            f"GPS ({lat:.4f}, {lon:.4f}) → country={cc} (unmapped, using as region)"
        )
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

    Accepts:
      - Named regions:  'north_america', 'us_southeast', 'alaska', …
      - US state codes: 'MD', 'FL', 'CA', … (case-insensitive)
      - ISO country codes for non-US countries: 'GB', 'AU', …
      - 'any': disables geo filtering
    """
    if region.lower() == "any":
        return "any"

    # Named region (e.g. 'north_america', 'us_pacific')
    if region.lower() in REGION_PLACE_IDS:
        return region.lower()

    upper = region.upper()

    # US state abbreviation (e.g. 'MD' → 'md', found in REGION_PLACE_IDS)
    if upper in US_STATE_ABBREV:
        return upper.lower()

    # ISO country code → broad region
    if upper in COUNTRY_TO_REGION:
        return COUNTRY_TO_REGION[upper]

    # Fall through: return lowercased as-is (custom region name / --place-id output)
    return region.lower()
