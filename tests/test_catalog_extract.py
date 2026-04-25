from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from src.catalog_extract import CatalogExtractor, Detection, SequenceMetadata
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


class FakeDominantMultiDetector:
    name = "fake-detector"

    def detect(self, image_path: Path) -> list[Detection]:
        return [
            Detection(
                detected_class="bird",
                confidence=0.91,
                x1=1,
                y1=2,
                x2=201,
                y2=302,
                area_fraction=0.18,
            ),
            Detection(
                detected_class="bird",
                confidence=0.50,
                x1=210,
                y1=20,
                x2=250,
                y2=80,
                area_fraction=0.009,
            ),
        ]


class FakePreviewProvider:
    def __init__(self, root: Path) -> None:
        self.root = root

    def build_preview(self, image_path: Path, detection: Detection, candidate_id: str) -> Path:
        out = self.root / f"{candidate_id}.jpg"
        out.write_bytes(b"preview")
        return out


class FakePreloadedDetector(FakeDetector):
    def detect_image(self, image, image_path: Path) -> list[Detection]:
        return self.detect(image_path)


class FakePreloadedPreviewProvider(FakePreviewProvider):
    def build_preview_from_image(
        self,
        image,
        image_path: Path,
        detection: Detection,
        candidate_id: str,
        *,
        source_size: tuple[int, int] | None = None,
    ) -> Path:
        return self.build_preview(image_path, detection, candidate_id)


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
            images_inserted, candidates_inserted, last_image_id = extractor.extract(
                "/tmp/does-not-matter.lrcat",
                created_at="2026-04-22T17:05:00Z",
            )

        self.assertEqual(images_inserted, 1)
        self.assertEqual(candidates_inserted, 1)
        self.assertEqual(last_image_id, 123)
        candidates = store.list_candidates(review_status="unreviewed")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["detector_name"], "fake-detector")

    def test_extract_passes_start_after_id_to_catalog(self) -> None:
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
        extractor = CatalogExtractor(store, FakeDetector(), FakePreviewProvider(tmpdir))

        with patch("src.catalog_extract.LightroomCatalog.open", return_value=fake_catalog):
            with patch.object(fake_catalog, "get_images", wraps=fake_catalog.get_images) as mocked_get_images:
                extractor.extract(
                    "/tmp/does-not-matter.lrcat",
                    start_after_id=99,
                    created_at="2026-04-22T17:05:00Z",
                )

        mocked_get_images.assert_called_once()
        self.assertEqual(mocked_get_images.call_args.kwargs["start_after_id"], 99)

    def test_extract_loads_image_once_when_detector_and_preview_support_preloaded_image(self) -> None:
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
        extractor = CatalogExtractor(store, FakePreloadedDetector(), FakePreloadedPreviewProvider(tmpdir))

        with patch("src.catalog_extract.LightroomCatalog.open", return_value=fake_catalog):
            with patch("src.catalog_extract.load_image", return_value=Image.new("RGB", (3000, 2000))) as mocked_load_image:
                extractor.extract(
                    "/tmp/does-not-matter.lrcat",
                    created_at="2026-04-22T17:05:00Z",
                )

        mocked_load_image.assert_called_once()

    def test_extract_marks_only_dominant_detection_as_burst_safe(self) -> None:
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
            FakeDominantMultiDetector(),
            FakePreviewProvider(tmpdir),
        )

        with patch("src.catalog_extract.LightroomCatalog.open", return_value=fake_catalog):
            extractor.extract(
                "/tmp/does-not-matter.lrcat",
                created_at="2026-04-22T17:05:00Z",
            )

        candidates = store.list_candidates(review_status="unreviewed")
        self.assertEqual(len(candidates), 2)
        flags = {candidate["id"]: candidate["safe_single_subject_burst"] for candidate in candidates}
        self.assertEqual(flags["img123_det001"], 1)
        self.assertEqual(flags["img123_det002"], 0)

    def test_burst_safe_detection_flags_rejects_similar_sized_detections(self) -> None:
        detections = [
            Detection(
                detected_class="bird",
                confidence=0.91,
                x1=1,
                y1=2,
                x2=201,
                y2=302,
                area_fraction=0.18,
            ),
            Detection(
                detected_class="bird",
                confidence=0.50,
                x1=210,
                y1=20,
                x2=350,
                y2=200,
                area_fraction=0.16,
            ),
        ]

        self.assertEqual(
            CatalogExtractor._burst_safe_detection_flags(detections),
            [False, False],
        )

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

    def test_assign_burst_groups_prefers_sequence_metadata(self) -> None:
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
                capture_time="2026-04-22T17:00:05Z",
            ),
            CatalogImage(
                id_local=3,
                file_path="/tmp/a/file3.ARW",
                file_format="RAW",
                base_name="file3",
                extension="ARW",
                rating=None,
                color_label=None,
                capture_time="2026-04-22T17:00:10Z",
            ),
        ]
        metadata = {
            1: SequenceMetadata(make="SONY", sequence_image_number=1, sequence_file_number=1, sequence_length="Continuous"),
            2: SequenceMetadata(make="SONY", sequence_image_number=2, sequence_file_number=2, sequence_length="Continuous"),
            3: SequenceMetadata(make="SONY", sequence_image_number=3, sequence_file_number=3, sequence_length="Continuous"),
        }
        groups = CatalogExtractor._assign_burst_groups(images, metadata)
        self.assertEqual(groups[1], groups[2])
        self.assertEqual(groups[2], groups[3])

    def test_assign_burst_groups_does_not_apply_sony_logic_to_other_makes(self) -> None:
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
                capture_time="2026-04-22T17:00:05Z",
            ),
        ]
        metadata = {
            1: SequenceMetadata(make="Canon", sequence_image_number=1, sequence_file_number=1, sequence_length="Continuous"),
            2: SequenceMetadata(make="Canon", sequence_image_number=2, sequence_file_number=2, sequence_length="Continuous"),
        }
        groups = CatalogExtractor._assign_burst_groups(images, metadata)
        self.assertEqual(groups, {})

    def test_load_sequence_metadata_prefers_pyexiv2(self) -> None:
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
        ]

        fake_tags = [
            {
                "Exif.Image.Make": "SONY",
                "Exif.Sony2.SequenceNumber": "1",
                "Exif.Sony2.ReleaseMode": "2",
            },
            {
                "Exif.Image.Make": "SONY",
                "Exif.Sony2.SequenceNumber": "2",
                "Exif.Sony2.ReleaseMode": "2",
            },
        ]

        class FakeImage:
            def __init__(self, path: str) -> None:
                self.path = path

            def read_exif(self):
                return fake_tags.pop(0)

        with patch("src.catalog_extract.pyexiv2", create=True) as mocked_pyexiv2:
            mocked_pyexiv2.Image.side_effect = FakeImage
            metadata = CatalogExtractor._load_sequence_metadata(images)

        self.assertEqual(metadata[1].sequence_file_number, 1)
        self.assertEqual(metadata[2].sequence_image_number, 2)
        self.assertEqual(metadata[1].make, "SONY")
        self.assertEqual(metadata[1].sequence_length, "Continuous")
        self.assertEqual(mocked_pyexiv2.Image.call_count, 2)

    def test_load_sequence_metadata_falls_back_to_exifread(self) -> None:
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
        ]

        fake_tags = [
            {
                "Image Make": "SONY",
                "MakerNote SequenceNumber": "1",
                "MakerNote ReleaseMode": "Continuous",
            },
            {
                "Image Make": "SONY",
                "MakerNote SequenceNumber": "2",
                "MakerNote ReleaseMode": "Continuous",
            },
        ]

        with patch("pathlib.Path.open"):
            with patch("src.catalog_extract.pyexiv2", None):
                with patch("src.catalog_extract.exifread.process_file", side_effect=fake_tags) as mocked_process:
                    metadata = CatalogExtractor._load_sequence_metadata(images)

        self.assertEqual(metadata[1].sequence_file_number, 1)
        self.assertEqual(metadata[2].sequence_image_number, 2)
        self.assertEqual(metadata[1].make, "SONY")
        self.assertEqual(metadata[1].sequence_length, "Continuous")
        self.assertEqual(mocked_process.call_count, 2)

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
