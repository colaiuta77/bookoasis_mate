# Kavita 안전 이관 엔진의 경로 매칭·Dry Run·메타데이터·진행률 적용을 검증합니다.
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kavita_migration import (
    KavitaMigrationEngine,
    apply_path_mappings,
    parse_mapping_lines,
)


class KavitaMigrationEngineTest(unittest.TestCase):
    def _make_kavita_database(self, path):
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE Library (Id INTEGER PRIMARY KEY, Name TEXT);
            CREATE TABLE FolderPath (Id INTEGER PRIMARY KEY, LibraryId INTEGER, Path TEXT);
            CREATE TABLE Series (
                Id INTEGER PRIMARY KEY, LibraryId INTEGER, Name TEXT, CoverImage TEXT
            );
            CREATE TABLE Volume (
                Id INTEGER PRIMARY KEY, SeriesId INTEGER, Name TEXT, CoverImage TEXT
            );
            CREATE TABLE Chapter (
                Id INTEGER PRIMARY KEY, VolumeId INTEGER, Title TEXT, CoverImage TEXT
            );
            CREATE TABLE MangaFile (
                Id INTEGER PRIMARY KEY, ChapterId INTEGER, FilePath TEXT, Pages INTEGER
            );
            CREATE TABLE SeriesMetadata (
                Id INTEGER PRIMARY KEY, SeriesId INTEGER, ReleaseYear INTEGER, Summary TEXT
            );
            CREATE TABLE AspNetUsers (Id INTEGER PRIMARY KEY, UserName TEXT);
            CREATE TABLE AppUserProgresses (
                Id INTEGER PRIMARY KEY, ChapterId INTEGER, AppUserId INTEGER,
                PagesRead INTEGER, LastModified TEXT
            );
            INSERT INTO Library VALUES (1, 'Kavita 만화');
            INSERT INTO FolderPath VALUES (1, 1, '/kavita/books');
            INSERT INTO Series VALUES (1, 1, '테스트 시리즈', '');
            INSERT INTO Volume VALUES (1, 1, '1', '');
            INSERT INTO Chapter VALUES (1, 1, '테스트 1권', '');
            INSERT INTO MangaFile VALUES (
                1, 1, '/kavita/books/테스트 시리즈/테스트 1권.cbz', 100
            );
            INSERT INTO SeriesMetadata VALUES (1, 1, 2026, 'Kavita 소개');
            INSERT INTO AspNetUsers VALUES (5, 'reader');
            INSERT INTO AppUserProgresses VALUES (
                1, 1, 5, 25, '2026-07-26 12:00:00'
            );
            """
        )
        connection.commit()
        connection.close()

    def _make_bookoasis_database(self, path):
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE libraries (
                id INTEGER PRIMARY KEY, name TEXT, physical_path TEXT
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY, library_id INTEGER, title TEXT,
                series_name TEXT, author TEXT, publisher TEXT, summary TEXT,
                release_date TEXT, genre TEXT, tags TEXT, file_path TEXT,
                file_format TEXT NOT NULL,
                cover_image TEXT, cover_updated_at TEXT, total_pages INTEGER,
                metadata_locked INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, username TEXT UNIQUE
            );
            CREATE TABLE user_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT, book_id INTEGER,
                user_id INTEGER, pages_read INTEGER, is_completed INTEGER,
                last_read_at TEXT, UNIQUE(book_id, user_id)
            );
            INSERT INTO libraries VALUES (7, 'BookOasis 만화', '/volume1/books');
            INSERT INTO books (
                id, library_id, title, series_name, file_path, file_format,
                total_pages
            ) VALUES (
                11, 7, '기존 제목', '기존 시리즈',
                '/volume1/books/테스트 시리즈/테스트 1권.cbz', 'cbz', 100
            );
            INSERT INTO users VALUES (3, 'bookreader');
            """
        )
        connection.commit()
        connection.close()

    def _engine(self, root):
        kavita_db = root / "kavita.db"
        bookoasis_db = root / "media_general.db"
        covers = root / "covers"
        work = root / "work"
        covers.mkdir()
        self._make_kavita_database(kavita_db)
        self._make_bookoasis_database(bookoasis_db)
        return KavitaMigrationEngine(
            str(kavita_db),
            "",
            str(bookoasis_db),
            str(covers),
            str(work),
        ), bookoasis_db, work

    def test_path_mapping_uses_longest_prefix(self):
        mappings = parse_mapping_lines(
            "/kavita => /fallback\n/kavita/books => /volume1/books"
        )

        result = apply_path_mappings(
            "/kavita/books/시리즈/1.cbz",
            mappings,
        )

        self.assertEqual("/volume1/books/시리즈/1.cbz", result)

    def test_inspect_reports_matched_books_and_users(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, _, _ = self._engine(Path(tempdir))

            data = engine.inspect(
                path_mappings="/kavita/books => /volume1/books"
            )

        self.assertEqual(1, data["books_count"])
        self.assertEqual(1, data["matched_books_count"])
        self.assertEqual(0, data["new_books_count"])
        self.assertEqual(0, data["unmatched_books_count"])
        self.assertEqual(1, data["target_books_count"])
        self.assertEqual(["reader"], data["source_users"])
        self.assertEqual(["bookreader"], data["target_users"])
        self.assertEqual(1, data["progress_count"])
        self.assertEqual(1, data["libraries"][0]["matched"])
        self.assertFalse(data["path_mappings_auto_applied"])
        self.assertEqual(
            ["/kavita/books => /volume1/books"],
            data["effective_path_mappings"],
        )

    def test_inspect_keeps_legacy_appuser_and_userid_compatibility(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, _, _ = self._engine(Path(tempdir))
            connection = sqlite3.connect(engine.kavita_db_path)
            connection.execute("ALTER TABLE AspNetUsers RENAME TO AppUser")
            connection.execute(
                "ALTER TABLE AppUserProgresses "
                "RENAME COLUMN AppUserId TO UserId"
            )
            connection.commit()
            connection.close()

            data = engine.inspect(
                path_mappings="/kavita/books => /volume1/books"
            )

        self.assertEqual(["reader"], data["source_users"])
        self.assertEqual(1, data["progress_count"])

    def test_inspect_auto_applies_inferred_path_mapping(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, _, _ = self._engine(Path(tempdir))

            data = engine.inspect()

        self.assertEqual(
            ["/kavita/books => /volume1/books"],
            data["suggested_path_mappings"],
        )
        self.assertEqual(
            data["suggested_path_mappings"],
            data["effective_path_mappings"],
        )
        self.assertTrue(data["path_mappings_auto_applied"])
        self.assertEqual(1, data["matched_books_count"])
        self.assertEqual(0, data["unmatched_books_count"])

    def test_inspect_replaces_identity_mapping_with_inferred_mapping(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, _, _ = self._engine(Path(tempdir))

            data = engine.inspect(
                path_mappings="/kavita/books => /kavita/books"
            )

        self.assertTrue(data["path_mappings_auto_applied"])
        self.assertEqual(
            ["/kavita/books => /volume1/books"],
            data["effective_path_mappings"],
        )
        self.assertEqual(1, data["matched_books_count"])

    def test_inspect_suggests_only_same_named_target_user(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, bookoasis_db, _ = self._engine(Path(tempdir))
            connection = sqlite3.connect(bookoasis_db)
            connection.execute("INSERT INTO users VALUES (4, 'Reader')")
            connection.commit()
            connection.close()

            data = engine.inspect()

        self.assertEqual(
            ["reader => Reader"],
            data["suggested_user_mappings"],
        )
        self.assertEqual(
            ["Reader", "bookreader"],
            data["target_users"],
        )

    def test_inspect_does_not_suggest_ambiguous_target_prefix(self):
        suggestions = KavitaMigrationEngine._suggest_path_mappings(
            [
                {
                    "library_name": "만화",
                    "file_path": "/kavita/books/시리즈/1.cbz",
                }
            ],
            {"만화": ["/kavita/books"]},
            {
                "/target-a/시리즈/1.cbz": {},
                "/target-b/시리즈/1.cbz": {},
            },
        )

        self.assertEqual([], suggestions)

    def test_inspect_collapses_gdrive_roots_to_one_parent_mapping(self):
        suggestions = KavitaMigrationEngine._suggest_path_mappings(
            [
                {
                    "library_name": "잡지",
                    "file_path": "/books/GDRIVE/READING/잡지/a.cbz",
                },
                {
                    "library_name": "만화",
                    "file_path": "/books/GDRIVE/READING/만화/b.cbz",
                },
            ],
            {
                "잡지": ["/books/GDRIVE/READING/잡지"],
                "만화": ["/books/GDRIVE/READING/만화"],
            },
            {
                "/mnt/gds2/GDRIVE/READING/잡지/a.cbz": {},
                "/mnt/gds2/GDRIVE/READING/만화/b.cbz": {},
            },
        )

        self.assertEqual(
            [("/books", "/mnt/gds2")],
            suggestions,
        )

    def test_inspect_uses_library_gdrive_roots_without_matching_files(self):
        suggestions = KavitaMigrationEngine._suggest_path_mappings(
            [
                {
                    "library_name": "만화",
                    "file_path": "/books/GDRIVE/READING/만화/kavita-name.cbz",
                }
            ],
            {"만화": ["/books/GDRIVE/READING/만화"]},
            {
                "/mnt/gds2/GDRIVE/READING/만화/bookoasis-name.cbz": {},
            },
            ["/mnt/gds2/GDRIVE/READING/만화"],
        )

        self.assertEqual(
            [("/books", "/mnt/gds2")],
            suggestions,
        )

    def test_dry_run_does_not_modify_bookoasis_database(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, bookoasis_db, _ = self._engine(Path(tempdir))

            result = engine.migrate(
                path_mappings="/kavita/books => /volume1/books",
                user_mappings="reader => bookreader",
                import_covers=False,
                import_progress=True,
                dry_run=True,
            )
            connection = sqlite3.connect(bookoasis_db)
            row = connection.execute(
                "SELECT title, metadata_locked FROM books WHERE id=11"
            ).fetchone()
            progress_count = connection.execute(
                "SELECT COUNT(*) FROM user_progress"
            ).fetchone()[0]
            connection.close()

        self.assertTrue(result["dry_run"])
        self.assertEqual(("기존 제목", 0), row)
        self.assertEqual(0, progress_count)

    def test_migrate_updates_only_matched_book_and_mapped_user_progress(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, bookoasis_db, work = self._engine(Path(tempdir))

            result = engine.migrate(
                path_mappings="/kavita/books => /volume1/books",
                user_mappings="reader => bookreader",
                import_covers=False,
                import_progress=True,
                lock_metadata=True,
                backup=True,
                dry_run=False,
            )
            connection = sqlite3.connect(bookoasis_db)
            book = connection.execute(
                """
                SELECT title, series_name, summary, release_date, metadata_locked
                FROM books WHERE id=11
                """
            ).fetchone()
            progress = connection.execute(
                """
                SELECT user_id, pages_read, is_completed, last_read_at
                FROM user_progress WHERE book_id=11
                """
            ).fetchone()
            connection.close()
            backup_exists = Path(result["backup_path"]).is_file()
            backup_count = len(list((work / "backups").glob("*.db")))

        self.assertFalse(result["dry_run"])
        self.assertEqual(1, result["metadata_updated_count"])
        self.assertEqual(1, result["progress_updated_count"])
        self.assertEqual(
            ("1 - 테스트 1권", "테스트 시리즈", "Kavita 소개", "2026-01-01", 1),
            book,
        )
        self.assertEqual((3, 25, 0, "2026-07-26 12:00:00"), progress)
        self.assertTrue(backup_exists)
        self.assertEqual(1, backup_count)

    def test_migrate_builds_empty_bookoasis_database_with_identity_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, bookoasis_db, _ = self._engine(root)
            source = sqlite3.connect(engine.kavita_db_path)
            source.execute(
                "UPDATE FolderPath SET Path='/mnt/gds/GDRIVE/READING/만화'"
            )
            source.execute(
                "UPDATE MangaFile SET "
                "FilePath='/mnt/gds/GDRIVE/READING/만화/테스트/1.cbz'"
            )
            source.commit()
            source.close()
            target = sqlite3.connect(bookoasis_db)
            target.execute("DELETE FROM books")
            target.execute("DELETE FROM libraries")
            target.commit()
            target.close()

            preview = engine.inspect(
                path_mappings="/mnt/gds => /mnt/gds"
            )
            result = engine.migrate(
                path_mappings="/mnt/gds => /mnt/gds",
                user_mappings="reader => bookreader",
                import_covers=False,
                import_progress=True,
                lock_metadata=True,
                backup=False,
                dry_run=False,
            )
            target = sqlite3.connect(bookoasis_db)
            library = target.execute(
                "SELECT name, physical_path FROM libraries"
            ).fetchone()
            book = target.execute(
                """
                SELECT title, series_name, file_path, file_format,
                       total_pages, metadata_locked
                FROM books
                """
            ).fetchone()
            progress = target.execute(
                """
                SELECT user_id, pages_read, is_completed
                FROM user_progress
                """
            ).fetchone()
            target.close()

        self.assertEqual(0, preview["target_books_count"])
        self.assertEqual(0, preview["matched_books_count"])
        self.assertEqual(1, preview["new_books_count"])
        self.assertEqual(0, preview["unmatched_books_count"])
        self.assertEqual(1, result["books_created_count"])
        self.assertEqual(0, result["books_updated_count"])
        self.assertEqual(
            ("Kavita 만화", "/mnt/gds/GDRIVE/READING/만화"),
            library,
        )
        self.assertEqual(
            (
                "1 - 테스트 1권",
                "테스트 시리즈",
                "/mnt/gds/GDRIVE/READING/만화/테스트/1.cbz",
                "cbz",
                100,
                1,
            ),
            book,
        )
        self.assertEqual((3, 25, 0), progress)

    def test_worker_process_builds_empty_bookoasis_database(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, bookoasis_db, work = self._engine(root)
            target = sqlite3.connect(bookoasis_db)
            target.execute("DELETE FROM books")
            target.execute("DELETE FROM libraries")
            target.commit()
            target.close()
            config_path = work / "kavita_job.json"
            status_path = work / "kavita_status.json"
            stop_path = work / "kavita_stop"
            config_path.write_text(
                json.dumps(
                    {
                        "source_type": "kavita",
                        "work_dir": str(work),
                        "kavita_db_path": str(engine.kavita_db_path),
                        "kavita_cover_path": "",
                        "target_db_type": "general",
                        "target_general_db": str(bookoasis_db),
                        "target_cover_root": str(root / "covers"),
                        "path_mappings": (
                            "/kavita/books => /volume1/books"
                        ),
                        "user_mappings": "reader => bookreader",
                        "selected_libraries": [],
                        "import_covers": False,
                        "import_progress": True,
                        "lock_metadata": True,
                        "backup": False,
                        "dry_run": False,
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
            target = sqlite3.connect(bookoasis_db)
            book_count = target.execute(
                "SELECT COUNT(*) FROM books"
            ).fetchone()[0]
            progress_count = target.execute(
                "SELECT COUNT(*) FROM user_progress"
            ).fetchone()[0]
            target.close()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("wait", status["is_working"])
        self.assertEqual("kavita", status["operation"])
        self.assertEqual(1, status["result"]["books_created_count"])
        self.assertEqual(1, book_count)
        self.assertEqual(1, progress_count)

    def test_batch_insert_resolves_new_book_ids_for_progress(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, bookoasis_db, _ = self._engine(root)
            source = sqlite3.connect(engine.kavita_db_path)
            source.executescript(
                """
                INSERT INTO Volume VALUES (2, 1, '2', '');
                INSERT INTO Chapter VALUES (2, 2, '테스트 2권', '');
                INSERT INTO MangaFile VALUES (
                    2, 2,
                    '/kavita/books/테스트 시리즈/테스트 2권.cbz',
                    120
                );
                INSERT INTO AppUserProgresses VALUES (
                    2, 2, 5, 40, '2026-07-27 12:00:00'
                );
                """
            )
            source.commit()
            source.close()
            target = sqlite3.connect(bookoasis_db)
            target.execute("DELETE FROM books")
            target.execute("DELETE FROM libraries")
            target.commit()
            target.close()

            result = engine.migrate(
                path_mappings="/kavita/books => /volume1/books",
                user_mappings="reader => bookreader",
                import_covers=False,
                import_progress=True,
                backup=False,
                dry_run=False,
            )
            target = sqlite3.connect(bookoasis_db)
            rows = target.execute(
                """
                SELECT b.title, p.pages_read
                FROM books b
                JOIN user_progress p ON p.book_id=b.id
                ORDER BY b.title
                """
            ).fetchall()
            target.close()

        self.assertEqual(2, result["books_created_count"])
        self.assertEqual(2, result["progress_updated_count"])
        self.assertEqual(
            [("1 - 테스트 1권", 25), ("2 - 테스트 2권", 40)],
            rows,
        )

    def test_unknown_target_user_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, _, _ = self._engine(Path(tempdir))

            with self.assertRaisesRegex(ValueError, "없는 사용자"):
                engine.migrate(
                    path_mappings="/kavita/books => /volume1/books",
                    user_mappings="reader => missing",
                    import_covers=False,
                    import_progress=True,
                    dry_run=False,
                )

    def test_progress_failure_rolls_back_metadata_update(self):
        with tempfile.TemporaryDirectory() as tempdir:
            engine, bookoasis_db, _ = self._engine(Path(tempdir))
            connection = sqlite3.connect(bookoasis_db)
            connection.execute("DROP TABLE user_progress")
            connection.commit()
            connection.close()

            with self.assertRaises(sqlite3.OperationalError):
                engine.migrate(
                    path_mappings="/kavita/books => /volume1/books",
                    user_mappings="reader => bookreader",
                    import_covers=False,
                    import_progress=True,
                    dry_run=False,
                )
            connection = sqlite3.connect(bookoasis_db)
            title, locked = connection.execute(
                "SELECT title, metadata_locked FROM books WHERE id=11"
            ).fetchone()
            connection.close()

        self.assertEqual("기존 제목", title)
        self.assertEqual(0, locked)

    def test_valid_cover_is_converted_to_bookoasis_hash_path(self):
        class FakeImage:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def save(self, target, _format, quality=80):
                Path(target).write_bytes(f"webp-{quality}".encode())

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, bookoasis_db, work = self._engine(root)
            kavita_covers = root / "kavita-covers"
            kavita_covers.mkdir()
            (kavita_covers / "sample.jpg").write_bytes(b"jpeg")
            connection = sqlite3.connect(root / "kavita.db")
            connection.execute(
                "UPDATE Chapter SET CoverImage='sample.jpg' WHERE Id=1"
            )
            connection.commit()
            connection.close()
            engine = KavitaMigrationEngine(
                str(root / "kavita.db"),
                str(kavita_covers),
                str(bookoasis_db),
                str(root / "covers"),
                str(work),
            )
            fake_pil = types.ModuleType("PIL")
            fake_pil.Image = types.SimpleNamespace(open=lambda _path: FakeImage())
            with patch.dict(sys.modules, {"PIL": fake_pil}):
                result = engine.migrate(
                    path_mappings="/kavita/books => /volume1/books",
                    import_covers=True,
                    import_progress=False,
                    backup=False,
                    dry_run=False,
                )
            connection = sqlite3.connect(bookoasis_db)
            library_id, cover_path = connection.execute(
                "SELECT library_id, cover_image FROM books WHERE id=11"
            ).fetchone()
            connection.close()
            stored_cover = root / "covers" / cover_path
            stored_cover_bytes = stored_cover.read_bytes()

        self.assertEqual(1, result["covers_updated_count"])
        self.assertTrue(cover_path.startswith(f"{library_id}/book_"))
        self.assertTrue(cover_path.endswith(".webp"))
        self.assertEqual(b"webp-80", stored_cover_bytes)

    def test_cover_conversion_failure_does_not_change_target(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, bookoasis_db, work = self._engine(root)
            kavita_covers = root / "kavita-covers"
            kavita_covers.mkdir()
            (kavita_covers / "sample.jpg").write_bytes(b"jpeg")
            source = sqlite3.connect(engine.kavita_db_path)
            source.execute(
                "UPDATE Chapter SET CoverImage='sample.jpg' WHERE Id=1"
            )
            source.commit()
            source.close()
            engine = KavitaMigrationEngine(
                str(root / "kavita.db"),
                str(kavita_covers),
                str(bookoasis_db),
                str(root / "covers"),
                str(work),
            )

            with patch.object(
                engine,
                "_convert_cover",
                side_effect=RuntimeError("표지 변환 실패"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "표지 변환 실패",
                ):
                    engine.migrate(
                        path_mappings=(
                            "/kavita/books => /volume1/books"
                        ),
                        import_covers=True,
                        import_progress=False,
                        backup=False,
                        dry_run=False,
                    )
            target = sqlite3.connect(bookoasis_db)
            title, cover_image = target.execute(
                "SELECT title, cover_image FROM books WHERE id=11"
            ).fetchone()
            target.close()
            stored_covers = list((root / "covers").rglob("*.webp"))

        self.assertEqual("기존 제목", title)
        self.assertIsNone(cover_image)
        self.assertEqual([], stored_covers)

    def test_cover_staging_uses_bounded_parallel_workers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            engine, _, _ = self._engine(root)
            source_root = root / "kavita-covers"
            staging = root / "staging"
            source_root.mkdir()
            staging.mkdir()
            migration_books = []
            for index in range(4):
                filename = f"cover-{index}.jpg"
                (source_root / filename).write_bytes(b"jpeg")
                migration_books.append((
                    {"covers": [filename]},
                    f"/volume1/books/book-{index}.cbz",
                    None,
                ))
            engine.kavita_cover_root = source_root.resolve()
            barrier = threading.Barrier(2)
            worker_names = set()
            worker_lock = threading.Lock()

            def fake_convert(_source, target):
                with worker_lock:
                    worker_names.add(threading.current_thread().name)
                barrier.wait(timeout=2)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"webp")

            with patch.object(
                engine,
                "_convert_cover",
                side_effect=fake_convert,
            ):
                converted = engine._stage_covers(
                    migration_books,
                    staging,
                    max_workers=2,
                )

        self.assertEqual(4, converted)
        self.assertEqual(2, len(worker_names))
        self.assertTrue(all(name.startswith("kavita-cover") for name in worker_names))

    def test_statement_batches_use_executemany(self):
        connection = Mock()
        batches = {
            "UPDATE books SET title=? WHERE id=?": [
                ("첫 번째", 1),
                ("두 번째", 2),
            ],
            "DELETE FROM books": [],
        }

        KavitaMigrationEngine._execute_statement_batches(
            connection,
            batches,
        )

        connection.executemany.assert_called_once_with(
            "UPDATE books SET title=? WHERE id=?",
            [("첫 번째", 1), ("두 번째", 2)],
        )


if __name__ == "__main__":
    unittest.main()
