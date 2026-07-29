# FlaskFarm 외부 유지보수 작업의 상태 파일과 결과를 검증합니다.
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
