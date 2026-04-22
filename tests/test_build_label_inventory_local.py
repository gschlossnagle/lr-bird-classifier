from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.build_label_inventory import collect_labels


class BuildLabelInventoryTest(unittest.TestCase):
    def test_collect_labels_unions_region_files(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        a = tmpdir / "a.json"
        b = tmpdir / "b.json"
        a.write_text(json.dumps({"labels": ["x", "y"]}), encoding="utf-8")
        b.write_text(json.dumps({"labels": ["y", "z"]}), encoding="utf-8")
        self.assertEqual(collect_labels([a, b]), ["x", "y", "z"])


if __name__ == "__main__":
    unittest.main()
