# 마운트된 표지 파일의 경로 안전성과 이미지 상태 검사를 검증합니다.
import base64
import struct
import tempfile
import unittest
from pathlib import Path

from cover_inspector import cleanup_orphan_files, inspect_cover_file, resolve_cover_path


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZrB8AAAAASUVORK5CYII="
)


class CoverInspectorTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "1").mkdir()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_rejects_path_outside_cover_root(self):
        self.assertIsNone(resolve_cover_path(self.root, "../secret.webp"))
        self.assertIsNone(resolve_cover_path("", "1/cover.webp"))

    def test_reports_low_resolution_and_missing_files(self):
        (self.root / "1" / "small.png").write_bytes(PNG_1X1)

        small = inspect_cover_file(self.root, "1/small.png")
        missing = inspect_cover_file(self.root, "1/missing.webp")

        self.assertEqual("low_resolution", small["status"])
        self.assertEqual((1, 1), (small["width"], small["height"]))
        self.assertNotIn("sha256", small)
        self.assertEqual("missing_file", missing["status"])

    def test_reports_small_files_and_abnormal_aspect_ratio(self):
        small_path = self.root / "1" / "small.png"
        small_path.write_bytes(PNG_1X1)
        wide_data = bytearray(PNG_1X1)
        wide_data[16:24] = struct.pack(">II", 1000, 100)
        (self.root / "1" / "wide.png").write_bytes(wide_data)

        small = inspect_cover_file(
            self.root,
            "1/small.png",
            min_width=200,
            min_height=280,
            min_file_size=5 * 1024,
            min_aspect_ratio=0.35,
        )
        wide = inspect_cover_file(
            self.root,
            "1/wide.png",
            min_width=1,
            min_height=1,
            min_file_size=0,
            min_aspect_ratio=0.35,
        )

        self.assertEqual("low_resolution", small["status"])
        self.assertEqual(["low_resolution", "small_file"], small["issues"])
        self.assertEqual("abnormal_aspect_ratio", wide["status"])
        self.assertEqual(["abnormal_aspect_ratio"], wide["issues"])
        self.assertAlmostEqual(0.1, wide["aspect_ratio"])

    def test_orphan_scan_requires_explicit_root(self):
        with self.assertRaises(FileNotFoundError):
            cleanup_orphan_files("", set(), [1])

    def test_orphan_cleanup_dry_run_only_reports_selected_library(self):
        (self.root / "2").mkdir()
        (self.root / "1" / "used.png").write_bytes(PNG_1X1)
        (self.root / "1" / "orphan.png").write_bytes(PNG_1X1)
        (self.root / "2" / "other.png").write_bytes(PNG_1X1)

        result = cleanup_orphan_files(
            self.root,
            {"1/used.png"},
            [1],
            dry_run=True,
        )

        self.assertEqual(2, result["scanned_count"])
        self.assertEqual(1, result["target_count"])
        self.assertEqual("planned", result["items"][0]["status"])
        self.assertTrue((self.root / "1" / "orphan.png").exists())
        self.assertTrue((self.root / "2" / "other.png").exists())

    def test_orphan_cleanup_deletes_only_unreferenced_selected_files(self):
        (self.root / "1" / "shared.png").write_bytes(PNG_1X1)
        orphan = self.root / "1" / "orphan.png"
        orphan.write_bytes(PNG_1X1)

        result = cleanup_orphan_files(
            self.root,
            {"1/shared.png"},
            [1],
            dry_run=False,
        )

        self.assertEqual(1, result["target_count"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual("deleted", result["items"][0]["status"])
        self.assertTrue((self.root / "1" / "shared.png").exists())
        self.assertFalse(orphan.exists())

    def test_orphan_cleanup_can_be_stopped_without_deleting(self):
        orphan = self.root / "1" / "orphan.png"
        orphan.write_bytes(PNG_1X1)

        result = cleanup_orphan_files(
            self.root,
            set(),
            [1],
            dry_run=False,
            should_stop=lambda: True,
        )

        self.assertTrue(result["stopped"])
        self.assertEqual(0, result["deleted_count"])
        self.assertTrue(orphan.exists())

    def test_orphan_cleanup_skips_symbolic_links(self):
        target = self.root / "1" / "target.png"
        link = self.root / "1" / "linked.png"
        target.write_bytes(PNG_1X1)
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("현재 환경에서 심볼릭 링크를 생성할 수 없습니다.")

        result = cleanup_orphan_files(
            self.root,
            {"1/target.png"},
            [1],
            dry_run=False,
        )

        self.assertEqual(0, result["deleted_count"])
        self.assertTrue(target.exists())
        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
