from __future__ import annotations

import unittest

from src.sampling import sample_manifest_rows


def _make_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(6):
        rows.append({"sample_id": f"a{i}", "truth_label": "species_a"})
    for i in range(3):
        rows.append({"sample_id": f"b{i}", "truth_label": "species_b"})
    for i in range(2):
        rows.append({"sample_id": f"c{i}", "truth_label": "species_c"})
    return rows


class SamplingTest(unittest.TestCase):
    def test_natural_sampling_respects_size(self) -> None:
        rows = _make_rows()
        sampled = sample_manifest_rows(rows, size=4, mode="natural", seed=1)
        self.assertEqual(len(sampled), 4)

    def test_balanced_sampling_spreads_labels(self) -> None:
        rows = _make_rows()
        sampled = sample_manifest_rows(rows, size=6, mode="balanced", seed=1)
        labels = [row["truth_label"] for row in sampled]
        self.assertGreaterEqual(labels.count("species_a"), 2)
        self.assertGreaterEqual(labels.count("species_b"), 2)
        self.assertGreaterEqual(labels.count("species_c"), 2)

    def test_hybrid_sampling_guarantees_floor_when_possible(self) -> None:
        rows = _make_rows()
        sampled = sample_manifest_rows(rows, size=6, mode="hybrid", seed=1, min_per_label=1)
        labels = {row["truth_label"] for row in sampled}
        self.assertEqual(labels, {"species_a", "species_b", "species_c"})

    def test_max_per_label_cap_applies(self) -> None:
        rows = _make_rows()
        sampled = sample_manifest_rows(
            rows,
            size=10,
            mode="natural",
            seed=1,
            max_per_label=2,
        )
        labels = [row["truth_label"] for row in sampled]
        self.assertLessEqual(labels.count("species_a"), 2)


if __name__ == "__main__":
    unittest.main()
