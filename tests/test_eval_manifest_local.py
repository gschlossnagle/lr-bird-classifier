from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.eval_manifest import load_manifest, write_manifest


class EvalManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def test_write_and_load_manifest(self) -> None:
        path = self.tmpdir / "manifest.jsonl"
        rows = [
            {
                "sample_id": "catalog-realworld-000001",
                "source_type": "catalog_real_world",
                "image_path": str((self.tmpdir / "image.jpg").resolve()),
                "truth_label": "00419_Animalia_Chordata_Aves_Accipitriformes_Accipitridae_Haliaeetus_leucocephalus",
                "truth_sci_name": "Haliaeetus leucocephalus",
                "truth_common_name": "Bald Eagle",
                "taxon_class": "Aves",
                "multi_label": False,
                "secondary_truth_labels": [],
            }
        ]
        write_manifest(path, rows)
        loaded = load_manifest(path)
        self.assertEqual(loaded, rows)

    def test_invalid_manifest_line_raises(self) -> None:
        path = self.tmpdir / "manifest.jsonl"
        path.write_text('{"sample_id": "broken"}\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_manifest(path)


if __name__ == "__main__":
    unittest.main()
