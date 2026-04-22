from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.catalog_extract import CatalogExtractor, Detection
from src.catalog import CatalogImage
from src.review_store import ReviewStore


class FakeDetector:
    name = "fake-detector"

    def detect(self, image_path: Path) -> list[Detection]:
        return [
            Detection(
                detected_class="bird",
                confidence=0.91,
                x1=1,
                y1=2,
                x2=101,
                y2=202,
                area_fraction=0.12,
            )
        ]


class FakePreviewProvider:
    def __init__(self, root: Path) -> None:
        self.root = root

    def build_preview(self, image_path: Path, detection: Detection, candidate_id: str) -> Path:
        out = self.root / f"{candidate_id}.jpg"
        out.write_bytes(b"preview")
        return out


class FakeCatalog:
    def __init__(self, images: list[CatalogImage]) -> None:
        self.images = images

    def __enter__(self) -> "FakeCatalog":
        return self

    def __exit__(self, *_) -> None:
        return None

    def get_images(self, **_) -> list[CatalogImage]:
        return self.images


class CatalogExtractorTest(unittest.TestCase):
    def test_extract_populates_store(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        db_path = tmpdir / "review.sqlite"
        raw_path = tmpdir / "source.ARW"
        raw_path.write_bytes(b"raw")

        store = ReviewStore(db_path)
        self.addCleanup(store.close)

        image = CatalogImage(
            id_local=123,
            file_path=str(raw_path.resolve()),
            file_format="RAW",
            base_name="source",
            extension="ARW",
            rating=4.0,
            color_label=None,
            capture_time="2026-04-22T17:00:00Z",
        )
        fake_catalog = FakeCatalog([image])
        extractor = CatalogExtractor(
            store,
            FakeDetector(),
            FakePreviewProvider(tmpdir),
        )

        with patch("src.catalog_extract.LightroomCatalog.open", return_value=fake_catalog):
            images_inserted, candidates_inserted = extractor.extract(
                "/tmp/does-not-matter.lrcat",
                created_at="2026-04-22T17:05:00Z",
            )

        self.assertEqual(images_inserted, 1)
        self.assertEqual(candidates_inserted, 1)
        candidates = store.list_candidates(review_status="unreviewed")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["detector_name"], "fake-detector")

    def test_assign_burst_groups_for_close_capture_times(self) -> None:
        images = [
            CatalogImage(
                id_local=1,
                file_path="/tmp/a/file1.ARW",
                file_format="RAW",
                base_name="file1",
                extension="ARW",
                rating=None,
                color_label=None,
                capture_time="2026-04-22T17:00:00Z",
            ),
            CatalogImage(
                id_local=2,
                file_path="/tmp/a/file2.ARW",
                file_format="RAW",
                base_name="file2",
                extension="ARW",
                rating=None,
                color_label=None,
                capture_time="2026-04-22T17:00:00.500000Z",
            ),
            CatalogImage(
                id_local=3,
                file_path="/tmp/a/file3.ARW",
                file_format="RAW",
                base_name="file3",
                extension="ARW",
                rating=None,
                color_label=None,
                capture_time="2026-04-22T17:00:03Z",
            ),
        ]
        groups = CatalogExtractor._assign_burst_groups(images)
        self.assertEqual(groups[1], groups[2])
        self.assertNotIn(3, groups)

    def test_extract_opens_catalog_read_only(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmpdir = Path(tmp.name)
        db_path = tmpdir / "review.sqlite"
        raw_path = tmpdir / "source.ARW"
        raw_path.write_bytes(b"raw")

        store = ReviewStore(db_path)
        self.addCleanup(store.close)

        image = CatalogImage(
            id_local=123,
            file_path=str(raw_path.resolve()),
            file_format="RAW",
            base_name="source",
            extension="ARW",
            rating=4.0,
            color_label=None,
            capture_time="2026-04-22T17:00:00Z",
        )
        fake_catalog = FakeCatalog([image])
        extractor = CatalogExtractor(
            store,
            FakeDetector(),
            FakePreviewProvider(tmpdir),
        )

        with patch("src.catalog_extract.LightroomCatalog.open", return_value=fake_catalog) as mocked_open:
            extractor.extract(
                "/tmp/readonly-check.lrcat",
                created_at="2026-04-22T17:05:00Z",
            )

        mocked_open.assert_called_once_with(
            "/tmp/readonly-check.lrcat",
            readonly=True,
            backup=False,
        )


if __name__ == "__main__":
    unittest.main()
