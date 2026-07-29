# BookOasis 로그 허용 목록, 증분 tail과 Lazy Scanner 진행률 추출을 검증합니다.
import tempfile
import unittest
from pathlib import Path

from bookoasis_logs import list_log_files, read_lazy_progress, read_log_tail


class BookOasisLogsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.lazy_log = self.root / "lazy_scanner.log"
        self.lazy_log.write_text(
            "\n".join([
                "[2026-07-25 10:00:00] [Lazy-Scanner] DB=general -> 처리 대상 도서 수: 3권",
                "[2026-07-25 10:00:01] [Lazy-Scanner] (1/3) [커버] 처리 시작 -> 첫째.cbz",
                "[2026-07-25 10:00:02] [Lazy-Scanner] (2/3) [커버] 처리 시작 -> 둘째.cbz",
            ]) + "\n",
            encoding="utf-8",
        )
        (self.root / "ignored.log").write_text("노출되면 안 됩니다.\n", encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_catalog_lists_only_allowed_existing_files(self):
        result = list_log_files(self.root)

        self.assertTrue(result["success"])
        self.assertEqual(["lazy_scanner.log"], [item["name"] for item in result["files"]])

    def test_tail_reads_incrementally_and_resets_after_truncation(self):
        first = read_log_tail(self.root, "lazy_scanner.log")
        with self.lazy_log.open("a", encoding="utf-8") as handle:
            handle.write("[2026-07-25 10:00:03] 새 로그\n")
        second = read_log_tail(
            self.root,
            "lazy_scanner.log",
            cursor_identity=first["identity"],
            cursor_offset=first["offset"],
        )
        self.lazy_log.write_text("초기화된 로그\n", encoding="utf-8")
        reset = read_log_tail(
            self.root,
            "lazy_scanner.log",
            cursor_identity=second["identity"],
            cursor_offset=second["offset"],
        )

        self.assertIn("첫째.cbz", first["text"])
        self.assertEqual("[2026-07-25 10:00:03] 새 로그\n", second["text"].replace("\r\n", "\n"))
        self.assertTrue(reset["reset"])
        self.assertEqual("초기화된 로그\n", reset["text"].replace("\r\n", "\n"))

    def test_rejects_non_allowlisted_filename(self):
        with self.assertRaises(ValueError):
            read_log_tail(self.root, "../ignored.log")

    def test_extracts_latest_lazy_scanner_progress(self):
        progress = read_lazy_progress(self.root)

        self.assertEqual(2, progress["done"])
        self.assertEqual(3, progress["total"])
        self.assertEqual(66.7, progress["percent"])
        self.assertEqual("둘째.cbz", progress["filename"])
        self.assertEqual("general", progress["db_type"])


if __name__ == "__main__":
    unittest.main()
