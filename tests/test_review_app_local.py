from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.review_app import load_label_inventory


class ReviewAppTest(unittest.TestCase):
    def test_load_label_inventory_from_plain_text(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "labels.txt"
        path.write_text("label_a\nlabel_b\nlabel_a\n", encoding="utf-8")
        self.assertEqual(load_label_inventory(path), ["label_a", "label_b"])

    def test_load_label_inventory_from_jsonl(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "labels.jsonl"
        path.write_text(
            '{"truth_label":"label_a"}\n{"truth_label":"label_b"}\n',
            encoding="utf-8",
        )
        self.assertEqual(load_label_inventory(path), ["label_a", "label_b"])


if __name__ == "__main__":
    unittest.main()
