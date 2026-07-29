# BookOasis Mate의 읽기 전용 진단과 예외 처리를 검증합니다.
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from mate_engine import BookOasisMateEngine


SCHEMA = """
CREATE TABLE libraries (
    id INTEGER PRIMARY KEY,
    name TEXT,
    physical_path TEXT,
    cron_schedule TEXT,
    last_scanned_at TEXT,
    scan_status TEXT,
    is_remote INTEGER
);
CREATE TABLE scanner_tasks (
    id INTEGER PRIMARY KEY,
    task_type TEXT,
    task_key TEXT,
    status TEXT,
    stage TEXT,
    enqueue_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    error_message TEXT
);
CREATE TABLE books (
    id INTEGER PRIMARY KEY,
    library_id INTEGER,
    title TEXT,
    series_name TEXT,
    author TEXT,
    publisher TEXT,
    isbn TEXT,
    summary TEXT,
    cover_image TEXT,
    total_pages INTEGER,
    file_size INTEGER,
    created_at TEXT,
    metadata_locked INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0
);
"""


def create_database(path, empty=False):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    if not empty:
        connection.executescript(
            """
            INSERT INTO libraries VALUES
              (1, '일반 도서', '/books', '0 3 * * *', CURRENT_TIMESTAMP, 'ready', 0),
              (2, '오래된 만화', '/comics', NULL, '2000-01-01 00:00:00', 'failed', 1);
            INSERT INTO scanner_tasks VALUES
              (1, 'scan', 'one', 'failed', 'flush', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '테스트 오류'),
              (2, 'scan', 'two', 'pending', NULL, CURRENT_TIMESTAMP, NULL, NULL, NULL);
            INSERT INTO books VALUES
              (1, 1, '정상 도서', '정상 시리즈', '저자', '출판사', 'OK', '소개', '1/ok.webp', 100, 1000, CURRENT_TIMESTAMP, 0, 0),
              (2, 2, '문제 도서', '문제 시리즈', '', '', '', '', '', 0, 0, CURRENT_TIMESTAMP, 1, 0),
              (3, 1, '중복 첫째', '중복 시리즈', '저자', '출판사', 'DUP', '소개', '1/dup1.webp', 10, 100, CURRENT_TIMESTAMP, 0, 0),
              (4, 1, '중복 둘째', '중복 시리즈', '저자', '출판사', 'DUP', '소개', '1/dup2.webp', 10, 100, CURRENT_TIMESTAMP, 0, 0),
              (5, 1, '삭제 도서', '삭제 시리즈', '', '', '', '', '', 0, 0, CURRENT_TIMESTAMP, 0, 1);
            """
        )
    connection.commit()
    connection.close()


class BookOasisMateEngineTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "media_general.db"
        create_database(self.db_path)
        self.settings = {
            "general_db_path": str(self.db_path),
            "adult_enabled": False,
            "bookoasis_url": "http://bookoasis:5930",
            "check_missing_isbn": False,
            "stale_days": 14,
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def test_build_report_summarizes_books_and_scanner(self):
        report = BookOasisMateEngine(self.settings).build_report()

        self.assertEqual("warning", report["status"])
        self.assertEqual(4, report["totals"]["total_books"])
        self.assertEqual(3, report["totals"]["problem_books"])
        self.assertEqual(1, report["totals"]["cover"])
        self.assertEqual(2, report["totals"]["duplicate_isbn"])
        self.assertEqual(1, report["scanner"]["stale_libraries"])
        self.assertEqual(1, report["scanner"]["recent_failed_tasks"])
        self.assertTrue(report["databases"][0]["connected"])
        self.assertEqual(
            {"problem_books", "failed_libraries", "recent_failed_tasks"},
            {reason["code"] for reason in report["databases"][0]["status_reasons"]},
        )
        self.assertEqual(
            report["databases"][0]["summary"]["problem_books"],
            next(
                reason["count"]
                for reason in report["databases"][0]["status_reasons"]
                if reason["code"] == "problem_books"
            ),
        )

    def test_issue_list_filters_searches_and_paginates(self):
        engine = BookOasisMateEngine(self.settings)
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO books VALUES
              (6, 1, '표지만 없는 도서', '표지 전용', '저자', '출판사', 'COVER', '소개', '', 10, 100, CURRENT_TIMESTAMP, 0, 0)
            """
        )
        connection.commit()
        connection.close()

        all_issues = engine.list_issues(page_size=10)
        duplicate = engine.list_issues(issue_type="duplicate_isbn", page_size=10)
        searched = engine.list_issues(search="문제 도서", page_size=10)
        cover_only = engine.list_issues(search="표지만 없는 도서", page_size=10)

        self.assertEqual(3, all_issues["total"])
        self.assertEqual({"중복 첫째", "중복 둘째"}, {item["title"] for item in duplicate["items"]})
        self.assertEqual(1, searched["total"])
        self.assertEqual("문제 도서", searched["items"][0]["title"])
        self.assertEqual("문제 시리즈", searched["items"][0]["series_name"])
        self.assertNotIn("표지 없음", {issue["label"] for issue in searched["items"][0]["issues"]})
        diagnostics = searched["items"][0]["diagnostics"]
        self.assertEqual(5, diagnostics["classification"]["matched_issue_count"])
        reasons = {
            reason["type"]: reason
            for reason in diagnostics["classification"]["reasons"]
        }
        self.assertEqual("페이지 수 미기록", reasons["pages"]["label"])
        self.assertEqual("books.total_pages", reasons["pages"]["column"])
        self.assertEqual(0, reasons["pages"]["value"])
        self.assertEqual("NULL 또는 0 이하", reasons["pages"]["rule"])
        self.assertEqual("파일 크기 미기록", reasons["file_size"]["label"])
        self.assertTrue(diagnostics["classification"]["active_filter"]["matched"])
        self.assertEqual("general", diagnostics["book"]["db_type"])
        self.assertEqual(0, cover_only["total"])
        self.assertEqual(
            {
                "http://bookoasis:5930/covers/1/dup1.webp",
                "http://bookoasis:5930/covers/1/dup2.webp",
            },
            {item["cover_url"] for item in duplicate["items"]},
        )
        duplicate_reason = duplicate["items"][0]["diagnostics"]["classification"]["reasons"][0]
        self.assertEqual("duplicate_isbn", duplicate_reason["type"])
        self.assertEqual(2, duplicate_reason["matching_active_books"])

    def test_issue_list_filters_books_by_library(self):
        engine = BookOasisMateEngine(self.settings)

        filtered = engine.list_issues(library_id=2, page_size=10)

        self.assertEqual(1, filtered["total"])
        self.assertEqual(["문제 도서"], [item["title"] for item in filtered["items"]])
        self.assertEqual({2}, {item["library_id"] for item in filtered["items"]})
        with self.assertRaises(ValueError):
            engine.list_issues(library_id="invalid", page_size=10)

    def test_missing_isbn_is_optional(self):
        engine = BookOasisMateEngine(self.settings)
        disabled = engine.list_issues(issue_type="isbn")

        settings = dict(self.settings)
        settings["check_missing_isbn"] = True
        enabled = BookOasisMateEngine(settings).list_issues(issue_type="isbn")

        self.assertEqual(0, disabled["total"])
        self.assertEqual(1, enabled["total"])
        self.assertEqual("문제 도서", enabled["items"][0]["title"])

    def test_scanner_status_does_not_expose_physical_path(self):
        data = BookOasisMateEngine(self.settings).scanner_status()

        self.assertEqual(2, len(data["libraries"]))
        self.assertNotIn("physical_path", data["libraries"][0])
        self.assertEqual("failed", data["tasks"][0]["status"])

    def test_series_gap_and_cover_pages_use_read_only_book_data(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            INSERT INTO books VALUES
              (101, 1, '새 시리즈 01권#101', '새 시리즈', '저자', '출판사', 'A1', '소개', '1/a.webp', 10, 10, CURRENT_TIMESTAMP, 0, 0),
              (102, 1, '새 시리즈 02권#102', '새 시리즈', '저자', '출판사', 'A2', '소개', '1/b.webp', 10, 10, CURRENT_TIMESTAMP, 0, 0),
              (104, 1, '새 시리즈 04권#104', '새 시리즈', '저자', '출판사', 'A4', '소개', '1/d.webp', 10, 10, CURRENT_TIMESTAMP, 0, 0);
            """
        )
        connection.commit()
        connection.close()

        engine = BookOasisMateEngine(self.settings)
        analysis = engine.analyze_series_gaps(search="새 시리즈")
        gaps = engine.series_gaps(search="새 시리즈", page_size=10)
        covers = engine.cover_items(search="새 시리즈", page_size=10)

        self.assertEqual(1, analysis["total"])
        self.assertNotIn("page", analysis)
        self.assertEqual([3], gaps["items"][0]["missing"])
        self.assertEqual(101, gaps["items"][0]["id"])
        self.assertEqual("새 시리즈 01권#101", gaps["items"][0]["title"])
        self.assertEqual("general", gaps["items"][0]["db_type"])
        self.assertEqual("http://bookoasis:5930/covers/1/a.webp", gaps["items"][0]["cover_url"])
        self.assertEqual(3, covers["total"])
        self.assertTrue(all(item["cover_url"].startswith("http://bookoasis:5930/covers/1/") for item in covers["items"]))

    def test_cover_items_filters_books_by_library(self):
        engine = BookOasisMateEngine(self.settings)

        filtered = engine.cover_items(library_id=2, page_size=10)

        self.assertEqual(1, filtered["total"])
        self.assertEqual(["문제 도서"], [item["title"] for item in filtered["items"]])
        self.assertEqual({2}, {item["library_id"] for item in filtered["items"]})
        with self.assertRaises(ValueError):
            engine.cover_items(library_id="invalid", page_size=10)

    def test_series_gap_uses_database_file_path_as_fallback(self):
        gap_db = Path(self.tempdir.name) / "filepath_gap.db"
        connection = sqlite3.connect(gap_db)
        connection.executescript(
            """
            CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (
                id INTEGER PRIMARY KEY,
                library_id INTEGER,
                title TEXT,
                series_name TEXT,
                file_path TEXT,
                is_deleted INTEGER DEFAULT 0
            );
            INSERT INTO libraries VALUES (7, '07.웹툰');
            INSERT INTO books VALUES
              (201, 7, '회춘 특별편#201', '회춘 [기안84]', '/mnt/webtoon/회춘.E0001.1화 회춘#201.cbz', 0),
              (202, 7, '회춘 특별편#202', '회춘 [기안84]', '/mnt/webtoon/회춘.E0002.2화 회춘#202.cbz', 0),
              (204, 7, '회춘 특별편#204', '회춘 [기안84]', '/mnt/webtoon/회춘.E0004.4화 회춘#204.cbz', 0);
            """
        )
        connection.commit()
        connection.close()

        result = BookOasisMateEngine({"general_db_path": str(gap_db)}).series_gaps(page_size=10)

        self.assertEqual([3], result["items"][0]["missing"])
        self.assertEqual("high", result["items"][0]["confidence"])

    def test_series_gap_filters_books_by_library_before_analysis(self):
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            INSERT INTO books VALUES
              (301, 1, '필터 시리즈 A 01권#301', '필터 시리즈 A', '저자', '출판사', 'F1', '소개', '', 10, 10, CURRENT_TIMESTAMP, 0, 0),
              (303, 1, '필터 시리즈 A 03권#303', '필터 시리즈 A', '저자', '출판사', 'F3', '소개', '', 10, 10, CURRENT_TIMESTAMP, 0, 0),
              (401, 2, '필터 시리즈 B 01권#401', '필터 시리즈 B', '저자', '출판사', 'G1', '소개', '', 10, 10, CURRENT_TIMESTAMP, 0, 0),
              (404, 2, '필터 시리즈 B 04권#404', '필터 시리즈 B', '저자', '출판사', 'G4', '소개', '', 10, 10, CURRENT_TIMESTAMP, 0, 0);
            """
        )
        connection.commit()
        connection.close()

        result = BookOasisMateEngine(self.settings).series_gaps(
            library_id=2,
            search="필터 시리즈",
            page_size=10,
        )

        self.assertEqual(1, result["total"])
        self.assertEqual("필터 시리즈 B", result["items"][0]["series_name"])
        self.assertEqual([2, 3], result["items"][0]["missing"])
        self.assertEqual(2, result["analyzed_books"])
        self.assertGreaterEqual(result["duration_ms"], 0)

    def test_quick_check_is_manual_and_read_only(self):
        result = BookOasisMateEngine(self.settings).quick_check()

        self.assertTrue(result["success"])
        self.assertEqual(["ok"], result["result"])

    def test_database_details_reports_size_and_library_sections(self):
        result = BookOasisMateEngine(self.settings).database_details()

        self.assertTrue(result["success"])
        self.assertEqual(str(self.db_path), result["path"])
        self.assertGreater(result["file_size"], 0)
        self.assertEqual(
            {
                (1, "일반 도서"),
                (2, "오래된 만화"),
            },
            {(item["id"], item["name"]) for item in result["libraries"]},
        )

    def test_database_connection_rejects_writes(self):
        engine = BookOasisMateEngine(self.settings)

        with closing(engine._connect(engine.get_target("general"))) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO books (id, title) VALUES (999, '쓰기 시도')")

    def test_missing_database_returns_visible_error(self):
        settings = dict(self.settings)
        settings["general_db_path"] = str(Path(self.tempdir.name) / "missing.db")

        report = BookOasisMateEngine(settings).build_report()

        self.assertEqual("error", report["status"])
        self.assertFalse(report["databases"][0]["connected"])
        self.assertIn("찾을 수 없습니다", report["databases"][0]["error"])
        self.assertFalse(Path(settings["general_db_path"]).exists())

    def test_adult_database_is_only_used_when_enabled(self):
        adult_path = Path(self.tempdir.name) / "media_adult.db"
        create_database(adult_path, empty=True)
        settings = dict(self.settings)
        settings.update({"adult_enabled": True, "adult_db_path": str(adult_path)})

        report = BookOasisMateEngine(settings).build_report()

        self.assertEqual(["general", "adult"], [item["db_type"] for item in report["databases"]])
        self.assertTrue(all(item["connected"] for item in report["databases"]))

    def test_cover_cleanup_can_protect_references_from_disabled_adult_database(self):
        adult_path = Path(self.tempdir.name) / "media_adult.db"
        create_database(adult_path, empty=True)
        connection = sqlite3.connect(adult_path)
        connection.execute(
            """
            INSERT INTO books (
                id, library_id, title, series_name, author, publisher, isbn, summary,
                cover_image, total_pages, file_size, created_at, metadata_locked, is_deleted
            ) VALUES (1, 1, '성인 도서', '', '', '', '', '', '1/adult.webp', 1, 1, CURRENT_TIMESTAMP, 0, 0)
            """
        )
        connection.commit()
        connection.close()
        settings = dict(self.settings, adult_enabled=False, adult_db_path=str(adult_path))
        engine = BookOasisMateEngine(settings)

        active_only = engine.all_cover_references()
        protected = engine.all_cover_references(include_inactive_adult=True)

        self.assertNotIn("1/adult.webp", active_only)
        self.assertIn("1/adult.webp", protected)

    def test_cover_reference_scan_batches_only_selected_library(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE books SET cover_image = '2/comic.webp' WHERE id = 2"
        )
        connection.commit()
        connection.close()
        progress = []
        engine = BookOasisMateEngine(self.settings)

        references = engine.all_cover_references(
            library_ids=[2],
            batch_size=100,
            on_progress=lambda read, unique: progress.append((read, unique)),
        )

        self.assertEqual({"2/comic.webp"}, references)
        self.assertEqual([(1, 1)], progress)

    def test_reduced_schema_degrades_without_crashing(self):
        reduced = Path(self.tempdir.name) / "reduced.db"
        connection = sqlite3.connect(reduced)
        connection.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO books VALUES (1, '최소 도서')")
        connection.commit()
        connection.close()

        report = BookOasisMateEngine({"general_db_path": str(reduced)}).build_report()

        self.assertEqual("healthy", report["status"])
        self.assertEqual(1, report["totals"]["total_books"])
        self.assertEqual(0, report["totals"]["problem_books"])
        self.assertEqual([], report["databases"][0]["status_reasons"])


if __name__ == "__main__":
    unittest.main()
