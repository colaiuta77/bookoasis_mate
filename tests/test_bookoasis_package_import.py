# BookOasis 공유 DB·표지 패키지의 검증, 경로 변환, 교체와 롤백을 검증합니다.
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bookoasis_package_import import (
    BookOasisPackageImportEngine,
    apply_media_mappings,
    suggested_gdrive_parent,
)


class BookOasisPackageImportEngineTest(unittest.TestCase):
    def _make_database(self, path, db_type, media_root="/source/books"):
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE libraries (
                id INTEGER PRIMARY KEY, name TEXT, physical_path TEXT
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, library_id INTEGER, title TEXT,
                file_path TEXT, cover_image TEXT
            );
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT);
            CREATE TABLE user_progress (
                id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER
            );
            """
        )
        library_id = 1 if db_type == "general" else 2
        book_id = 11 if db_type == "general" else 21
        file_path = f"{media_root}/{db_type}/sample.cbz"
        digest = hashlib.md5(file_path.encode("utf-8")).hexdigest()
        cover_image = f"{library_id}/book_{digest}.webp"
        connection.execute(
            "INSERT INTO libraries VALUES (?, ?, ?)",
            (library_id, f"{db_type} 보관함", f"{media_root}/{db_type}"),
        )
        connection.execute(
            "INSERT INTO books VALUES (?, ?, ?, ?, ?)",
            (book_id, library_id, f"{db_type} 도서", file_path, cover_image),
        )
        connection.execute(
            "INSERT INTO users VALUES (?, ?)", (library_id, f"{db_type}_user")
        )
        connection.execute(
            "INSERT INTO user_progress VALUES (?, ?, ?)",
            (library_id, library_id, book_id),
        )
        connection.execute("PRAGMA user_version = 26")
        connection.commit()
        connection.close()
        return {
            "library_id": library_id,
            "book_id": book_id,
            "file_path": file_path,
            "cover_image": cover_image,
        }

    @staticmethod
    def _make_current_database(path, marker):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE current_marker (value TEXT)")
        connection.execute("INSERT INTO current_marker VALUES (?)", (marker,))
        connection.commit()
        connection.close()

    def _make_packages(self, root, missing_cover=False, media_root="/source/books"):
        source = root / "source"
        db_dir = source / "db"
        covers_dir = source / "covers"
        db_dir.mkdir(parents=True)
        covers_dir.mkdir()
        records = {}
        for db_type in ("general", "adult"):
            records[db_type] = self._make_database(
                db_dir / f"media_{db_type}.db", db_type, media_root=media_root
            )
            if not (missing_cover and db_type == "adult"):
                cover = covers_dir / records[db_type]["cover_image"]
                cover.parent.mkdir(parents=True, exist_ok=True)
                cover.write_bytes(f"{db_type}-cover".encode("utf-8"))
        db_package = root / "db_dist_test.tar.gz"
        with tarfile.open(db_package, "w:gz") as archive:
            archive.add(db_dir, arcname="db")
        cover_package = root / "covers_dist_test.tar.gz"
        with tarfile.open(cover_package, "w:gz") as archive:
            archive.add(covers_dir, arcname="covers")
        return db_package, cover_package, records

    def _make_engine(self, root, health=False):
        work = root / "work"
        target_db = root / "target_db"
        target_covers = root / "target_covers"
        work.mkdir()
        target_db.mkdir()
        target_covers.mkdir()
        general = target_db / "media_general.db"
        adult = target_db / "media_adult.db"
        self._make_current_database(general, "general-old")
        self._make_current_database(adult, "adult-old")
        (target_covers / "old-cover.webp").write_bytes(b"old")
        engine = BookOasisPackageImportEngine(
            work,
            general,
            adult,
            target_covers,
            health_check=lambda: {"success": health},
        )
        return engine, work, general, adult, target_covers

    @staticmethod
    def _marker(path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute("SELECT value FROM current_marker").fetchone()[0]
        finally:
            connection.close()

    def test_inspect_reports_both_databases_users_progress_and_covers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, _, _, _ = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)

            result = engine.inspect(
                db_package,
                cover_package,
                "/source/books => /target/books",
            )

        self.assertEqual(2, len(result["databases"]))
        self.assertEqual(2, result["libraries_count"])
        self.assertEqual(2, result["books_count"])
        self.assertEqual(2, result["users_count"])
        self.assertEqual(2, result["progress_count"])
        self.assertEqual(2, result["cover_files_count"])
        self.assertEqual(0, result["missing_cover_count"])
        self.assertEqual(2, result["changed_books_count"])
        self.assertEqual(0, result["path_collision_count"])

    def test_lists_database_and_cover_packages_independently(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, _, _, _ = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)
            extra_database = work / "db_other.tar.gz"
            extra_database.write_bytes(b"listing-does-not-inspect-archive")

            packages = engine.list_packages()

        database_paths = {item["path"] for item in packages["databases"]}
        cover_paths = {item["path"] for item in packages["covers"]}
        self.assertEqual({str(db_package), str(extra_database)}, database_paths)
        self.assertEqual({str(cover_package)}, cover_paths)

    def test_exports_database_and_cover_archives_for_direct_import(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(root)
            nested_cover = covers / "7" / "book_sample.webp"
            nested_cover.parent.mkdir()
            nested_cover.write_bytes(b"sample-cover")

            real_tarfile_open = tarfile.open

            def python310_compatible_tarfile_open(*args, **kwargs):
                if "compresslevel" in kwargs:
                    raise TypeError(
                        "TarFile.__init__() got an unexpected keyword "
                        "argument 'compresslevel'"
                    )
                return real_tarfile_open(*args, **kwargs)

            with patch(
                "bookoasis_package_import.tarfile.open",
                side_effect=python310_compatible_tarfile_open,
            ):
                result = engine.export_package(
                    "shared books",
                    timestamp="20260728_120000",
                )

            database_package = Path(result["database_package_path"])
            cover_package = Path(result["cover_package_path"])
            with tarfile.open(database_package, "r:gz") as archive:
                database_members = {
                    member.name for member in archive.getmembers()
                }
                extract_root = root / "exported_db"
                archive.extractall(extract_root)
            with tarfile.open(cover_package, "r:gz") as archive:
                cover_members = {
                    member.name for member in archive.getmembers()
                }

            self.assertEqual(
                "db_shared_books_20260728_120000.tar.gz",
                database_package.name,
            )
            self.assertEqual(
                "covers_shared_books_20260728_120000.tar.gz",
                cover_package.name,
            )
            self.assertIn("db/media_general.db", database_members)
            self.assertIn("db/media_adult.db", database_members)
            self.assertIn("covers/7/book_sample.webp", cover_members)
            self.assertEqual("general-old", self._marker(
                extract_root / "db" / "media_general.db"
            ))
            self.assertEqual("adult-old", self._marker(
                extract_root / "db" / "media_adult.db"
            ))
            packages = engine.list_packages()
            self.assertIn(
                str(database_package),
                {item["path"] for item in packages["databases"]},
            )
            self.assertIn(
                str(cover_package),
                {item["path"] for item in packages["covers"]},
            )
            self.assertEqual(2, result["cover_files_count"])
            self.assertEqual(2, result["database_count"])

    def test_export_publish_failure_removes_half_package(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, _, _, _ = self._make_engine(root)
            real_replace = os.replace

            def fail_cover_publish(source, destination):
                if Path(destination).name.startswith(
                    "covers_failed_20260728_120000"
                ):
                    raise OSError("표지 공개 실패")
                return real_replace(source, destination)

            with patch(
                "bookoasis_package_import.os.replace",
                side_effect=fail_cover_publish,
            ):
                with self.assertRaisesRegex(OSError, "표지 공개 실패"):
                    engine.export_package(
                        "failed",
                        timestamp="20260728_120000",
                    )

            self.assertFalse(
                (work / "db_failed_20260728_120000.tar.gz").exists()
            )
            self.assertFalse(
                (work / "covers_failed_20260728_120000.tar.gz").exists()
            )
            self.assertEqual([], list(work.glob(".*_export_*.tar.gz")))

    def test_database_package_inspection_does_not_require_cover_package(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, _, _, _ = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(
                work,
                media_root="/books/GDRIVE/READING",
            )
            cover_package.unlink()

            result = engine.inspect_database_package(db_package)

        self.assertEqual("database", result["inspection_scope"])
        self.assertEqual(2, result["books_count"])
        self.assertEqual(2, result["cover_references_count"])
        self.assertEqual("/books", result["suggested_mapping_source"])

    def test_gdrive_parent_mapping_preserves_all_descendant_paths(self):
        source_paths = [
            "/books/GDRIVE/READING/잡지",
            "/books/GDRIVE/READING/책/리디셀렉트/잡지",
            "/books/GDRIVE/READING/만화/마블",
        ]

        source_parent = suggested_gdrive_parent(source_paths)
        mapped = apply_media_mappings(
            source_paths[1],
            [(source_parent, "/mnt/gds2")],
        )

        self.assertEqual("/books", source_parent)
        self.assertEqual(
            "/mnt/gds2/GDRIVE/READING/책/리디셀렉트/잡지",
            mapped,
        )

    def test_rejects_path_traversal_member(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, _, _, _ = self._make_engine(root)
            bad_package = work / "db_dist_bad.tar.gz"
            with tarfile.open(bad_package, "w:gz") as archive:
                payload = b"unsafe"
                member = tarfile.TarInfo("../escape.db")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            _, cover_package, _ = self._make_packages(work)

            with self.assertRaisesRegex(ValueError, "안전하지 않은 압축 경로"):
                engine.inspect(bad_package, cover_package)

    def test_dry_run_does_not_change_current_database_or_covers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)

            result = engine.migrate(
                db_package,
                cover_package,
                path_mappings="/source/books => /target/books",
                dry_run=True,
            )

            self.assertTrue(result["dry_run"])
            self.assertEqual(2, result["changed_books_count"])
            self.assertEqual("general-old", self._marker(general))
            self.assertEqual("adult-old", self._marker(adult))
            self.assertTrue((covers / "old-cover.webp").is_file())

    def test_migrate_inspects_each_archive_only_once(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, _, _, _ = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)

            with patch.object(
                engine,
                "_inspect_archive",
                wraps=engine._inspect_archive,
            ) as inspected:
                engine.migrate(
                    db_package,
                    cover_package,
                    dry_run=True,
                )

        self.assertEqual(2, inspected.call_count)
        self.assertEqual(
            ["database", "covers"],
            [call.args[1] for call in inspected.call_args_list],
        )

    def test_actual_import_replaces_set_and_keeps_backups(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(root)
            db_package, cover_package, records = self._make_packages(work)

            result = engine.migrate(
                db_package,
                cover_package,
                path_mappings="/source/books => /target/books",
                backup=True,
                dry_run=False,
                confirm_stopped=True,
            )

            for db_type, db_path in (("general", general), ("adult", adult)):
                connection = sqlite3.connect(db_path)
                row = connection.execute(
                    "SELECT file_path, cover_image FROM books"
                ).fetchone()
                connection.close()
                expected_path = records[db_type]["file_path"].replace(
                    "/source/books", "/target/books", 1
                )
                digest = hashlib.md5(expected_path.encode("utf-8")).hexdigest()
                expected_cover = (
                    f"{records[db_type]['library_id']}/book_{digest}.webp"
                )
                self.assertEqual(expected_path, row[0])
                self.assertEqual(expected_cover, row[1])
                self.assertTrue((covers / expected_cover).is_file())
                self.assertTrue(Path(result["database_backups"][db_type]).is_file())
            cover_backup = Path(result["cover_backup_path"])
            self.assertTrue((cover_backup / "old-cover.webp").is_file())
            self.assertFalse(result["dry_run"])

    def test_actual_import_installs_without_backup_on_new_target(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            work = root / "work"
            work.mkdir()
            db_package, cover_package, _ = self._make_packages(work)
            target_root = root / "new_bookoasis"
            general = target_root / "db" / "media_general.db"
            adult = target_root / "db" / "media_adult.db"
            covers = target_root / "covers"
            engine = BookOasisPackageImportEngine(
                work,
                general,
                adult,
                covers,
                health_check=lambda: {"success": False},
            )

            result = engine.migrate(
                db_package,
                cover_package,
                backup=True,
                dry_run=False,
                confirm_stopped=True,
            )

            self.assertTrue(general.is_file())
            self.assertTrue(adult.is_file())
            self.assertTrue(covers.is_dir())
            self.assertEqual({}, result["database_backups"])
            self.assertEqual("", result["cover_backup_path"])
            self.assertEqual(
                ["general", "adult"],
                result["installed_without_backup"],
            )
            self.assertTrue(result["cover_installed_without_backup"])

    def test_worker_process_writes_completed_status_for_dry_run(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)
            config_path = work / "job.json"
            status_path = work / "status.json"
            stop_path = work / "stop"
            config_path.write_text(
                json.dumps(
                    {
                        "work_dir": str(work),
                        "target_general_db": str(general),
                        "target_adult_db": str(adult),
                        "target_cover_root": str(covers),
                        "bookoasis_db_package_path": str(db_package),
                        "bookoasis_cover_package_path": str(cover_package),
                        "bookoasis_path_mappings": "",
                        "bookoasis_backup": True,
                        "bookoasis_dry_run": True,
                        "bookoasis_confirm_stopped": False,
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "database_migration_worker.py"
                    ),
                    str(config_path),
                    str(status_path),
                    str(stop_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            status = json.loads(status_path.read_text(encoding="utf-8"))

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual("bookoasis", status["operation"])
        self.assertTrue(status["result"]["dry_run"])
        self.assertNotEqual(os.getpid(), status["worker_pid"])

    def test_worker_process_exports_bookoasis_package(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _, work, general, adult, covers = self._make_engine(root)
            config_path = work / "export_job.json"
            status_path = work / "export_status.json"
            stop_path = work / "export_stop"
            config_path.write_text(
                json.dumps(
                    {
                        "work_dir": str(work),
                        "target_general_db": str(general),
                        "target_adult_db": str(adult),
                        "target_cover_root": str(covers),
                        "bookoasis_package_action": "export",
                        "bookoasis_export_name": "worker_share",
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(
                        Path(__file__).resolve().parents[1]
                        / "database_migration_worker.py"
                    ),
                    str(config_path),
                    str(status_path),
                    str(stop_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            status = json.loads(status_path.read_text(encoding="utf-8"))
            database_exists = Path(
                status["result"]["database_package_path"]
            ).is_file()
            covers_exists = Path(
                status["result"]["cover_package_path"]
            ).is_file()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual("export", status["package_action"])
        self.assertEqual("export", status["result"]["package_action"])
        self.assertTrue(database_exists)
        self.assertTrue(covers_exists)

    def test_healthy_bookoasis_blocks_actual_import(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(
                root, health=True
            )
            db_package, cover_package, _ = self._make_packages(work)

            with self.assertRaisesRegex(RuntimeError, "정상 응답"):
                engine.migrate(
                    db_package,
                    cover_package,
                    dry_run=False,
                    confirm_stopped=True,
                )

            self.assertEqual("general-old", self._marker(general))
            self.assertEqual("adult-old", self._marker(adult))
            self.assertTrue((covers / "old-cover.webp").is_file())

    def test_actual_import_requires_one_time_stopped_confirmation(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)

            with self.assertRaisesRegex(ValueError, "중지 확인"):
                engine.migrate(
                    db_package,
                    cover_package,
                    dry_run=False,
                    confirm_stopped=False,
                )

            self.assertEqual("general-old", self._marker(general))
            self.assertEqual("adult-old", self._marker(adult))
            self.assertTrue((covers / "old-cover.webp").is_file())

    def test_database_install_failure_restores_original_set(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, work, general, adult, covers = self._make_engine(root)
            db_package, cover_package, _ = self._make_packages(work)
            real_replace = os.replace
            failed = {"value": False}

            def fail_once(source, destination):
                if (
                    not failed["value"]
                    and ".media_adult.db.incoming_" in str(source)
                    and Path(destination) == adult
                ):
                    failed["value"] = True
                    raise OSError("설치 실패")
                return real_replace(source, destination)

            with patch(
                "bookoasis_package_import.os.replace", side_effect=fail_once
            ):
                with self.assertRaisesRegex(OSError, "설치 실패"):
                    engine.migrate(
                        db_package,
                        cover_package,
                        dry_run=False,
                        confirm_stopped=True,
                    )

            self.assertEqual("general-old", self._marker(general))
            self.assertEqual("adult-old", self._marker(adult))
            self.assertTrue((covers / "old-cover.webp").is_file())


if __name__ == "__main__":
    unittest.main()
