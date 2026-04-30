from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from src.lightroom_preview import load_standard_preview, lookup_file_uuid, resolve_standard_preview_path


class LightroomPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.catalog_path = root / "Catalog.lrcat"
        self.preview_root = root / "Catalog Previews.lrdata"
        self.preview_root.mkdir()
        (self.preview_root / "A" / "ABCD").mkdir(parents=True)
        self._build_catalog()
        self._build_previews()
        lookup_file_uuid.cache_clear()
        resolve_standard_preview_path.cache_clear()

    def _build_catalog(self) -> None:
        conn = sqlite3.connect(self.catalog_path)
        conn.executescript(
            """
            CREATE TABLE Adobe_images (id_local INTEGER PRIMARY KEY, rootFile INTEGER, masterImage INTEGER);
            CREATE TABLE AgLibraryFile (id_local INTEGER PRIMARY KEY, id_global TEXT, folder INTEGER, baseName TEXT);
            CREATE TABLE AgLibraryFolder (id_local INTEGER PRIMARY KEY, rootFolder INTEGER, pathFromRoot TEXT);
            CREATE TABLE AgLibraryRootFolder (id_local INTEGER PRIMARY KEY, absolutePath TEXT);
            """
        )
        conn.execute("INSERT INTO AgLibraryRootFolder VALUES (1, '/photos/')")
        conn.execute("INSERT INTO AgLibraryFolder VALUES (1, 1, 'birds/')")
        conn.execute("INSERT INTO AgLibraryFile VALUES (1, 'ABCD1234-UUID', 1, 'sample')")
        conn.execute("INSERT INTO Adobe_images VALUES (1, 1, NULL)")
        conn.commit()
        conn.close()

    def _build_previews(self) -> None:
        conn = sqlite3.connect(self.preview_root / "previews.db")
        conn.executescript(
            """
            CREATE TABLE Pyramid (uuid NOT NULL, digest NOT NULL, colorProfile NOT NULL, fileTimeStamp, quality NOT NULL, croppedWidth NOT NULL, croppedHeight NOT NULL, pyramidFileTimeStamp, fingerprint, fromProxy);
            CREATE TABLE PyramidLevel (uuid NOT NULL, digest NOT NULL, level NOT NULL, longDimension NOT NULL, lastAccess NOT NULL, width NOT NULL, height NOT NULL, fileSize);
            """
        )
        conn.execute(
            "INSERT INTO Pyramid VALUES (?, ?, 'AdobeRGB', NULL, 'standard', 4000, 3000, NULL, NULL, 0)",
            ("ABCD1234-UUID", "digest123"),
        )
        conn.execute(
            "INSERT INTO PyramidLevel VALUES (?, ?, 4, 3840, 0, 3840, 2560, 12345)",
            ("ABCD1234-UUID", "digest123"),
        )
        conn.commit()
        conn.close()

        preview_path = self.preview_root / "A" / "ABCD" / "ABCD1234-UUID-digest123_3840"
        Image.new("RGB", (3840, 2560), color=(10, 20, 30)).save(preview_path, format="JPEG")

    def test_lookup_file_uuid_matches_catalog_path_without_extension(self) -> None:
        self.assertEqual(lookup_file_uuid(str(self.catalog_path), "/photos/birds/sample.ARW"), "ABCD1234-UUID")

    def test_resolve_standard_preview_path_builds_hashed_path(self) -> None:
        path = resolve_standard_preview_path(str(self.catalog_path), "/photos/birds/sample.ARW")
        self.assertIsNotNone(path)
        assert path is not None
        self.assertEqual(path.name, "ABCD1234-UUID-digest123_3840")

    def test_load_standard_preview_loads_preview_image(self) -> None:
        image = load_standard_preview(str(self.catalog_path), "/photos/birds/sample.ARW")
        self.assertIsNotNone(image)
        assert image is not None
        self.assertEqual(image.size, (3840, 2560))


if __name__ == "__main__":
    unittest.main()
