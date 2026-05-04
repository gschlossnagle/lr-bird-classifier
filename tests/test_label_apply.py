from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from src.label_apply import (
    SpeciesLabel,
    apply_catalog_species_label,
    existing_flat_tags_from_log,
    flat_keywords_for_label,
    write_sidecar_species_labels,
)


class ApplyCatalogSpeciesLabelTest(unittest.TestCase):
    def test_apply_catalog_species_label_tags_expected_hierarchy(self) -> None:
        cat = Mock()
        cat.ensure_bird_species_keyword.return_value = 11
        cat.ensure_species_keyword.return_value = (21, 22)
        cat.ensure_scientific_keywords.return_value = (31, 32, 33)
        cat.ensure_confidence_keyword.return_value = 41
        cat.ensure_manually_classed_keyword.return_value = 51
        cat.tag_image.side_effect = [True, False, False, False, False, False, False, False]

        label = SpeciesLabel(
            common_name="Bald Eagle",
            sci_name="Haliaeetus leucocephalus",
            order="Accipitriformes",
            family="Accipitridae",
            order_display="Hawks-Eagles-Kites-Allies",
        )

        newly_tagged = apply_catalog_species_label(
            cat,
            123,
            label,
            confidence_band_name="Very High",
            manual=True,
        )

        self.assertTrue(newly_tagged)
        cat.ensure_bird_species_keyword.assert_called_once_with("Bald Eagle")
        cat.ensure_species_keyword.assert_called_once_with(
            "Hawks-Eagles-Kites-Allies",
            "Bald Eagle",
        )
        cat.ensure_scientific_keywords.assert_called_once_with(
            "Accipitriformes",
            "Accipitridae",
            "Haliaeetus leucocephalus",
        )
        cat.ensure_confidence_keyword.assert_called_once_with("Very High")
        cat.ensure_manually_classed_keyword.assert_called_once_with()
        self.assertEqual(
            cat.tag_image.call_args_list,
            [
                call(123, 11),
                call(123, 22),
                call(123, 21),
                call(123, 31),
                call(123, 32),
                call(123, 33),
                call(123, 41),
                call(123, 51),
            ],
        )

    def test_flat_keywords_for_label_preserves_unique_order(self) -> None:
        label = SpeciesLabel(
            common_name="Bald Eagle",
            sci_name="Haliaeetus leucocephalus",
            order="Accipitriformes",
            family="Accipitridae",
            order_display="Accipitriformes",
        )
        self.assertEqual(
            flat_keywords_for_label(label),
            [
                "Bald Eagle",
                "Accipitriformes",
                "Accipitridae",
                "Haliaeetus leucocephalus",
            ],
        )

    def test_existing_flat_tags_from_log_dedupes_and_preserves_order(self) -> None:
        clf_log = Mock()
        clf_log.get_all_rows.return_value = [
            {
                "image_id": 123,
                "common_name": "Bald Eagle",
                "sci_name": "Haliaeetus leucocephalus",
                "label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
            },
            {
                "image_id": 123,
                "common_name": "Bald Eagle",
                "sci_name": "Haliaeetus leucocephalus",
                "label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
            },
            {
                "image_id": 999,
                "common_name": "Osprey",
                "sci_name": "Pandion haliaetus",
                "label": "99999_Animalia_Chordata_Aves_Accipitriformes_Pandionidae_Pandion_haliaetus",
            },
        ]
        self.assertEqual(
            existing_flat_tags_from_log(clf_log, 123),
            [
                "Bald Eagle",
                "Hawks-Eagles-Kites-Allies",
                "Accipitriformes",
                "Accipitridae",
                "Haliaeetus leucocephalus",
            ],
        )


class WriteSidecarSpeciesLabelsTest(unittest.TestCase):
    @patch("src.label_apply.write_bird_keywords")
    @patch("src.label_apply.clean_xmp_keywords")
    def test_replace_existing_cleans_before_write(self, mocked_clean, mocked_write) -> None:
        mocked_write.return_value = True
        labels = [
            SpeciesLabel(
                common_name="Bald Eagle",
                sci_name="Haliaeetus leucocephalus",
                order="Accipitriformes",
                family="Accipitridae",
                order_display="Hawks-Eagles-Kites-Allies",
            ),
            SpeciesLabel(
                common_name="Osprey",
                sci_name="Pandion haliaetus",
                order="Accipitriformes",
                family="Pandionidae",
                order_display="Hawks-Eagles-Kites-Allies",
            ),
        ]

        ok = write_sidecar_species_labels(
            Path("/tmp/test.ARW"),
            labels,
            replace_existing=True,
            flat_to_remove=["old-label"],
        )

        self.assertTrue(ok)
        mocked_clean.assert_called_once_with(Path("/tmp/test.ARW"), ["old-label"])
        self.assertEqual(mocked_write.call_count, 2)
