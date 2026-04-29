from __future__ import annotations

import unittest
from unittest.mock import patch

from src.ebird_reference import (
    lookup_reference_asset_id,
    lookup_species_code,
    parse_media_search_asset_id,
    resolve_reference,
)


SAMPLE_MEDIA_HTML = """
<html>
  <head>
    <meta property="og:image" content="https://cdn.download.ams.birds.cornell.edu/api/v2/asset/320005481/1200">
  </head>
  <body>
    <ol class="ResultsGrid-grid">
      <li class="ResultsGrid-card"><div data-asset-id="655991320"></div></li>
      <li class="ResultsGrid-card"><div data-asset-id="655991319"></div></li>
    </ol>
  </body>
</html>
"""


class EbirdReferenceTest(unittest.TestCase):
    def test_lookup_species_code_exact_scientific_name(self) -> None:
        self.assertEqual(
            lookup_species_code(
                truth_sci_name="Platalea ajaja",
                truth_common_name="Roseate Spoonbill",
            ),
            "rosspo1",
        )

    def test_lookup_species_code_falls_back_to_common_name(self) -> None:
        self.assertEqual(
            lookup_species_code(
                truth_sci_name="Not A Real Bird",
                truth_common_name="Roseate Spoonbill",
            ),
            "rosspo1",
        )

    def test_parse_media_search_asset_id_prefers_first_card(self) -> None:
        self.assertEqual(parse_media_search_asset_id(SAMPLE_MEDIA_HTML), "655991320")

    def test_lookup_reference_asset_id_parses_media_search_html(self) -> None:
        class FakeResponse:
            text = SAMPLE_MEDIA_HTML

            def raise_for_status(self) -> None:
                return None

        lookup_reference_asset_id.cache_clear()
        with patch("src.ebird_reference.requests.get", return_value=FakeResponse()):
            self.assertEqual(lookup_reference_asset_id("comeid"), "655991320")

    def test_resolve_reference_builds_urls(self) -> None:
        with patch("src.ebird_reference.lookup_reference_asset_id", return_value="655991320"):
            reference = resolve_reference(
                truth_sci_name="Somateria mollissima",
                truth_common_name="Common Eider",
            )
        self.assertIsNotNone(reference)
        assert reference is not None
        self.assertEqual(reference.species_code, "comeid")
        self.assertEqual(reference.species_url, "https://ebird.org/species/comeid")
        self.assertEqual(
            reference.media_search_url,
            "https://media.ebird.org/catalog?mediaType=photo&taxonCode=comeid&view=grid",
        )
        self.assertEqual(reference.macaulay_asset_url, "https://macaulaylibrary.org/asset/655991320")
        self.assertEqual(
            reference.preview_image_url,
            "https://cdn.download.ams.birds.cornell.edu/api/v2/asset/655991320/1200",
        )


if __name__ == "__main__":
    unittest.main()
