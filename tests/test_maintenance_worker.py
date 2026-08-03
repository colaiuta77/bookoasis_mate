# FlaskFarm 외부 유지보수 작업의 상태 파일과 결과를 검증합니다.
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maintenance_worker import run_worker


class MaintenanceWorkerTest(unittest.TestCase):
    @staticmethod
    def _read_status(path):
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_orphan_cleanup_worker_writes_bounded_completed_status(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            cover_root = root / "covers"
            (cover_root / "1").mkdir(parents=True)
            (cover_root / "1" / "used.webp").write_bytes(b"used")
            (cover_root / "1" / "orphan.webp").write_bytes(b"orphan")
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY,
                    library_id INTEGER,
                    title TEXT,
                    cover_image TEXT,
                    is_deleted INTEGER
                );
                INSERT INTO libraries VALUES (1, '테스트');
                INSERT INTO books VALUES (1, 1, '사용 도서', '1/used.webp', 0);
                """
            )
            connection.commit()
            connection.close()
            config_path = root / "config.json"
            status_path = root / "status.json"
            stop_path = root / "stop"
            config = {
                "job_type": "orphan_cleanup",
                "settings": {
                    "general_db_path": str(db_path),
                    "adult_db_path": "",
                    "adult_enabled": False,
                    "cover_root_path": str(cover_root),
                },
                "library_ids": [1],
                "dry_run": True,
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

            return_code = run_worker(config_path, status_path, stop_path)
            status = self._read_status(status_path)

        self.assertEqual(0, return_code)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual(2, status["scanned_count"])
        self.assertEqual(1, status["target_count"])
        self.assertEqual("1/orphan.webp", status["items"][0]["path"])
        self.assertLessEqual(len(status["items"]), 500)

    def test_category_export_worker_writes_completed_status(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            cover_root = root / "covers"
            work_root = root / "migration"
            source_root = root / "books"
            cover_root.mkdir()
            work_root.mkdir()
            source_root.mkdir()
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE libraries (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    physical_path TEXT,
                    cron_schedule TEXT,
                    icon TEXT,
                    color TEXT,
                    hide_cover INTEGER
                );
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY,
                    library_id INTEGER,
                    title TEXT,
                    file_path TEXT,
                    cover_image TEXT,
                    is_deleted INTEGER
                );
                """
            )
            connection.execute(
                "INSERT INTO libraries VALUES (1, '테스트', ?, '', '', '', 0)",
                (str(source_root),),
            )
            connection.commit()
            connection.close()
            config_path = root / "config.json"
            status_path = root / "status.json"
            stop_path = root / "stop"
            config = {
                "job_type": "category_migration",
                "operation": "export",
                "work_dir": str(work_root),
                "cover_root_path": str(cover_root),
                "target_general_db": str(db_path),
                "target_adult_db": "",
                "export_db_type": "general",
                "export_library_ids": [1],
            }
            config_path.write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

            return_code = run_worker(config_path, status_path, stop_path)
            status = self._read_status(status_path)

        self.assertEqual(0, return_code)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual(100, status["progress_percent"])
        self.assertEqual(1, status["result"]["count"])
        self.assertEqual("카테고리 내보내기를 완료했습니다.", status["message"])

    def test_batch_book_rescan_worker_tracks_results_and_removes_secret_config(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE books (
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    series_name TEXT
                );
                INSERT INTO books VALUES (1, '성공 도서', '성공 시리즈');
                INSERT INTO books VALUES (2, '실패 도서', '실패 시리즈');
                """
            )
            connection.commit()
            connection.close()
            config_path = root / "config.json"
            status_path = root / "status.json"
            stop_path = root / "stop"
            config = {
                "job_type": "batch_book_rescan",
                "delete_config_after_read": True,
                "db_type": "general",
                "db_path": str(db_path),
                "book_ids": [1, 2],
                "source": "issues",
                "source_label": "문제 도서",
                "bookoasis_url": "http://bookoasis:5930",
                "bookoasis_username": "admin",
                "bookoasis_password": "secret",
                "api_timeout": 30,
            }
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

            with patch("maintenance_worker.BookOasisClient") as client_class:
                client = client_class.return_value
                client.login_admin.return_value = {"success": True}
                client.scan_book.side_effect = [
                    {"success": True, "message": "완료"},
                    {"success": False, "message": "실패 원인"},
                ]
                return_code = run_worker(config_path, status_path, stop_path)
            status = self._read_status(status_path)

        self.assertEqual(0, return_code)
        self.assertFalse(config_path.exists())
        self.assertEqual("wait", status["is_working"])
        self.assertEqual(2, status["current"])
        self.assertEqual(1, status["success_count"])
        self.assertEqual(1, status["failed_count"])
        self.assertEqual(["성공 도서", "실패 도서"], [item["title"] for item in status["items"]])
        self.assertNotIn("secret", json.dumps(status, ensure_ascii=False))
        client_class.assert_called_once_with(
            "http://bookoasis:5930",
            30,
            username="admin",
            password="secret",
        )

    def test_batch_book_rescan_retries_transient_failure_without_stopping_job(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);
                INSERT INTO books VALUES (1, '재시도 도서');
                INSERT INTO books VALUES (2, '다음 도서');
                """
            )
            connection.commit()
            connection.close()
            config_path = root / "config.json"
            status_path = root / "status.json"
            stop_path = root / "stop"
            config_path.write_text(
                json.dumps(
                    {
                        "job_type": "batch_book_rescan",
                        "db_type": "general",
                        "db_path": str(db_path),
                        "book_ids": [1, 2],
                        "source": "issues",
                        "source_label": "문제 도서",
                        "bookoasis_url": "http://bookoasis:5930",
                        "bookoasis_username": "admin",
                        "bookoasis_password": "secret",
                        "api_timeout": 30,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("maintenance_worker.BookOasisClient") as client_class:
                client = client_class.return_value
                client.login_admin.return_value = {"success": True}
                client.scan_book.side_effect = [
                    {
                        "success": False,
                        "message": "IncompleteRead(0 bytes read)",
                        "retryable": True,
                    },
                    {"success": True, "message": "재시도 완료"},
                    {"success": True, "message": "다음 완료"},
                ]
                with patch("maintenance_worker.time.sleep") as mocked_sleep:
                    return_code = run_worker(config_path, status_path, stop_path)
            status = self._read_status(status_path)

        self.assertEqual(0, return_code)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual(2, status["success_count"])
        self.assertEqual(0, status["failed_count"])
        self.assertEqual(3, client.scan_book.call_count)
        mocked_sleep.assert_called_once()

    def test_batch_book_rescan_records_exhausted_transient_item_and_continues(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT);
                INSERT INTO books VALUES (1, '응답 단절 도서');
                INSERT INTO books VALUES (2, '정상 도서');
                """
            )
            connection.commit()
            connection.close()
            config_path = root / "config.json"
            status_path = root / "status.json"
            stop_path = root / "stop"
            config_path.write_text(
                json.dumps(
                    {
                        "job_type": "batch_book_rescan",
                        "db_type": "general",
                        "db_path": str(db_path),
                        "book_ids": [1, 2],
                        "source": "issues",
                        "source_label": "문제 도서",
                        "bookoasis_url": "http://bookoasis:5930",
                        "bookoasis_username": "admin",
                        "bookoasis_password": "secret",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            transient = {
                "success": False,
                "message": "IncompleteRead(0 bytes read)",
                "retryable": True,
            }

            with patch("maintenance_worker.BookOasisClient") as client_class:
                client = client_class.return_value
                client.login_admin.return_value = {"success": True}
                client.scan_book.side_effect = [
                    transient,
                    transient,
                    transient,
                    {"success": True, "message": "완료"},
                ]
                with patch("maintenance_worker.time.sleep"):
                    return_code = run_worker(config_path, status_path, stop_path)
            status = self._read_status(status_path)

        self.assertEqual(0, return_code)
        self.assertEqual(2, status["current"])
        self.assertEqual(1, status["success_count"])
        self.assertEqual(1, status["failed_count"])
        self.assertIn("3회", status["items"][0]["message"])

    def test_cover_inspection_worker_persists_shared_file_analysis(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config_path = root / "config.json"
            status_path = root / "status.json"
            result_path = root / "result.json"
            stop_path = root / "stop"
            config_path.write_text(
                json.dumps(
                    {
                        "job_type": "cover_inspection",
                        "settings": {
                            "cover_root_path": str(root),
                            "cover_min_width": 200,
                            "cover_min_height": 280,
                            "cover_min_file_size_kb": 15,
                            "cover_min_aspect_percent": 45,
                        },
                        "db_type": "general",
                        "library_id": "7",
                        "search": "표지",
                        "fingerprint": "test-fingerprint",
                        "result_path": str(result_path),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            source = {
                "items": [
                    {"id": 1, "title": "정상", "cover_path": "7/ok.webp"},
                    {"id": 2, "title": "복합 문제", "cover_path": "7/bad.webp"},
                ],
                "total": 2,
                "page": 1,
                "pages": 1,
            }

            with patch("maintenance_worker.BookOasisMateEngine") as engine_class:
                engine_class.return_value.cover_items.return_value = source
                with patch(
                    "maintenance_worker.inspect_cover_file",
                    side_effect=[
                        {"status": "ok", "issues": []},
                        {
                            "status": "low_resolution",
                            "issues": ["low_resolution", "small_file"],
                            "width": 100,
                            "height": 140,
                        },
                    ],
                ):
                    return_code = run_worker(config_path, status_path, stop_path)
            status = self._read_status(status_path)
            result = self._read_status(result_path)

        self.assertEqual(0, return_code)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual(2, status["current"])
        self.assertEqual(2, status["total"])
        self.assertTrue(status["result_ready"])
        self.assertEqual(1, status["issue_counts"]["low_resolution"])
        self.assertEqual(1, status["issue_counts"]["small_file"])
        self.assertEqual([2], [item["id"] for item in result["items"]])
        self.assertEqual("test-fingerprint", result["fingerprint"])


if __name__ == "__main__":
    unittest.main()
