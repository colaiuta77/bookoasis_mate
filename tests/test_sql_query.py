# BookOasis Mate 읽기 전용 SQL 도구의 안전성과 결과 제한을 검증합니다.
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sql_query import ReadOnlySqlTool


class ReadOnlySqlToolTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "media_general.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(
                """
                CREATE TABLE libraries (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    physical_path TEXT,
                    scan_status TEXT,
                    last_scanned_at TEXT
                );
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY,
                    library_id INTEGER,
                    title TEXT,
                    series_name TEXT,
                    file_path TEXT,
                    total_pages INTEGER,
                    file_size INTEGER,
                    is_deleted INTEGER DEFAULT 0
                );
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    password_hash TEXT,
                    role TEXT,
                    has_adult_access INTEGER,
                    created_at TEXT
                );
                CREATE TABLE user_progress (
                    id INTEGER PRIMARY KEY,
                    book_id INTEGER,
                    user_id INTEGER,
                    pages_read INTEGER,
                    is_completed INTEGER,
                    last_read_at TEXT
                );
                CREATE TABLE user_category_permissions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER,
                    library_id INTEGER,
                    has_access INTEGER
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
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO libraries VALUES (10, '만화', '/books/comics', 'ready', NULL);
                INSERT INTO books VALUES (100, 10, '첫 책', '시리즈', '/books/comics/1.cbz', 10, 1000, 0);
                INSERT INTO books VALUES (101, 10, '둘째 책', '시리즈', '/books/comics/2.cbz', 20, 2000, 0);
                INSERT INTO users VALUES (3, 'admin', 'secret-hash', 'admin', 1, '2026-01-01');
                INSERT INTO user_progress VALUES (1, 100, 3, 4, 0, '2026-08-03 20:00:00');
                INSERT INTO user_progress VALUES (2, 101, 2, 1, 0, '2026-08-03 21:00:00');
                INSERT INTO user_category_permissions VALUES (1, 3, 10, 1);
                INSERT INTO settings VALUES ('WEBHOOK_TOKEN', 'secret-token');
                """
            )
            connection.commit()
        finally:
            connection.close()
        self.tool = ReadOnlySqlTool(
            {"general": str(self.db_path), "adult": str(self.db_path)}
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_select_returns_columns_rows_and_truncation(self):
        result = self.tool.execute(
            "general",
            "SELECT id, title FROM books ORDER BY id",
            max_rows=1,
        )

        self.assertEqual(["id", "title"], result["columns"])
        self.assertEqual([[100, "첫 책"]], result["rows"])
        self.assertEqual(1, result["row_count"])
        self.assertTrue(result["truncated"])
        self.assertEqual("general", result["db_type"])

    def test_with_and_explain_query_plan_are_allowed(self):
        with_result = self.tool.execute(
            "general",
            "WITH active AS (SELECT id FROM books WHERE is_deleted = 0) SELECT count(*) AS count FROM active",
        )
        explain_result = self.tool.execute(
            "general",
            "EXPLAIN QUERY PLAN SELECT * FROM books WHERE id = 100",
        )

        self.assertEqual([[2]], with_result["rows"])
        self.assertGreaterEqual(len(explain_result["rows"]), 1)

    def test_semicolon_and_write_keyword_inside_literal_are_allowed(self):
        result = self.tool.execute(
            "general",
            "SELECT 'UPDATE; DELETE' AS harmless_text",
        )

        self.assertEqual([["UPDATE; DELETE"]], result["rows"])

    def test_write_multiple_statements_and_pragma_are_rejected(self):
        for query in (
            "UPDATE books SET title = '변경' WHERE id = 100",
            "SELECT id FROM books; SELECT id FROM users",
            "PRAGMA table_info(books)",
        ):
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    self.tool.execute("general", query)

        connection = sqlite3.connect(self.db_path)
        try:
            title = connection.execute(
                "SELECT title FROM books WHERE id = 100"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual("첫 책", title)

    def test_sensitive_columns_are_blocked_even_with_alias(self):
        for query in (
            "SELECT password_hash AS value FROM users",
            "SELECT value FROM settings",
            "SELECT * FROM settings",
        ):
            with self.subTest(query=query):
                with self.assertRaises(ValueError) as context:
                    self.tool.execute("general", query)
                self.assertIn("민감", str(context.exception))

    def test_unknown_database_is_rejected(self):
        with self.assertRaises(ValueError):
            self.tool.execute("other", "SELECT 1")

    def test_long_query_is_interrupted(self):
        with self.assertRaises(TimeoutError):
            self.tool.execute(
                "general",
                """
                WITH RECURSIVE counter(value) AS (
                    VALUES(1)
                    UNION ALL
                    SELECT value + 1 FROM counter
                )
                SELECT sum(value) FROM counter
                """,
                timeout_seconds=0.001,
            )

    def test_presets_include_progress_permission_and_library_diagnostics(self):
        presets = {item["id"]: item for item in self.tool.presets()}

        for preset_id in (
            "recent_progress_diagnosis",
            "orphan_progress_users",
            "library_book_counts",
            "recent_scanner_tasks",
        ):
            self.assertIn(preset_id, presets)
        self.assertIn("USER_MISSING", presets["recent_progress_diagnosis"]["sql"])
        self.assertIn("user_category_permissions", presets["recent_progress_diagnosis"]["sql"])

    def test_all_presets_execute_against_current_schema(self):
        for preset in self.tool.presets():
            with self.subTest(preset=preset["id"]):
                result = self.tool.execute("general", preset["sql"], max_rows=20)
                self.assertIn("columns", result)
                self.assertIn("rows", result)


if __name__ == "__main__":
    unittest.main()
