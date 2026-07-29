# BookOasis 카테고리 패키지 내보내기와 신규·병합 가져오기를 검증합니다.
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from category_migration import CategoryMigrationEngine


class CategoryMigrationEngineTest(unittest.TestCase):
    def _make_database(self, path, source_root):
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                physical_path TEXT,
                cron_schedule TEXT,
                icon TEXT,
                color TEXT,
                hide_cover INTEGER DEFAULT 0
            );
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                series_name TEXT,
                author TEXT,
                isbn TEXT,
                file_path TEXT UNIQUE,
                file_format TEXT,
                total_pages INTEGER,
                has_offsets INTEGER,
                cover_image TEXT,
                publisher TEXT,
                link TEXT,
                score REAL,
                release_date TEXT,
                summary TEXT,
                genre TEXT,
                tags TEXT,
                is_favorite INTEGER DEFAULT 0,
                cover_updated_at TEXT,
                created_at TEXT,
                metadata_locked INTEGER DEFAULT 0,
                file_mtime REAL,
                file_size INTEGER,
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE book_offsets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                page_idx INTEGER,
                filename TEXT,
                local_header_offset INTEGER,
                compress_size INTEGER,
                file_size INTEGER,
                compress_type INTEGER
            );
            """
        )
        connection.execute(
            """
            INSERT INTO libraries
                (name, physical_path, cron_schedule, icon, color, hide_cover)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("원본 만화", str(source_root), "0 3 * * *", "fa-book", "#123456", 0),
        )
        connection.execute(
            """
            INSERT INTO books (
                library_id, title, series_name, author, file_path, file_format,
                total_pages, has_offsets, cover_image, created_at, file_size, is_deleted
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                "테스트 1권",
                "테스트",
                "작가",
                str(source_root / "테스트 1권.cbz"),
                "cbz",
                2,
                1,
                "1/cover-one.webp",
                "2026-07-24 12:00:00",
                100,
            ),
        )
        connection.execute(
            """
            INSERT INTO book_offsets (
                book_id, page_idx, filename, local_header_offset,
                compress_size, file_size, compress_type
            ) VALUES (1, 0, '001.jpg', 10, 20, 30, 8)
            """
        )
        connection.commit()
        connection.close()

    def test_exports_inspects_and_imports_new_category_transactionally(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            source_root = root / "source"
            target_root = root / "target"
            cover_root = root / "covers"
            work_root = root / "migration"
            source_root.mkdir()
            (cover_root / "1").mkdir(parents=True)
            (cover_root / "1" / "cover-one.webp").write_bytes(b"cover")
            self._make_database(db_path, source_root)
            engine = CategoryMigrationEngine(str(work_root), str(cover_root))

            exported = engine.export_categories(
                str(db_path),
                "general",
                [1],
            )
            package_path = Path(exported["files"][0]["path"])
            inspected = engine.inspect_package(str(package_path))
            imported = engine.import_category(
                str(db_path),
                str(package_path),
                [str(target_root)],
                db_type="general",
                name="가져온 만화",
                backup=True,
            )

            self.assertTrue(package_path.is_file())
            self.assertEqual("1.2", inspected["export_version"])
            self.assertEqual(1, inspected["books_count"])
            self.assertEqual(1, inspected["covers_count"])
            self.assertEqual([str(source_root)], inspected["physical_paths"])
            self.assertTrue(Path(imported["backup_path"]).is_file())
            self.assertEqual(1, imported["books_count"])
            self.assertEqual(1, imported["offsets_count"])
            self.assertEqual(1, imported["covers_count"])

            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            library = connection.execute(
                "SELECT * FROM libraries WHERE id = ?",
                (imported["library_id"],),
            ).fetchone()
            book = connection.execute(
                "SELECT * FROM books WHERE library_id = ?",
                (imported["library_id"],),
            ).fetchone()
            offset_count = connection.execute(
                "SELECT COUNT(*) FROM book_offsets WHERE book_id = ?",
                (book["id"],),
            ).fetchone()[0]
            connection.close()

            self.assertEqual("가져온 만화", library["name"])
            self.assertEqual(str(target_root), library["physical_path"])
            self.assertEqual(str(target_root / "테스트 1권.cbz"), book["file_path"])
            self.assertEqual(
                f"{imported['library_id']}/cover-one.webp",
                book["cover_image"],
            )
            self.assertEqual(1, offset_count)
            self.assertTrue(
                (cover_root / str(imported["library_id"]) / "cover-one.webp").is_file()
            )

    def test_rejects_package_outside_configured_work_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            work_root = root / "migration"
            cover_root = root / "covers"
            cover_root.mkdir()
            outside = root / "outside.oasis.zip"
            with zipfile.ZipFile(outside, "w") as archive:
                archive.writestr("manifest.json", "{}")
                archive.writestr("metadata.json", "{}")
            engine = CategoryMigrationEngine(str(work_root), str(cover_root))

            with self.assertRaises(ValueError):
                engine.inspect_package(str(outside))

    def test_import_failure_rolls_back_database_and_temporary_covers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            source_root = root / "source"
            target_root = root / "target"
            cover_root = root / "covers"
            work_root = root / "migration"
            source_root.mkdir()
            (cover_root / "1").mkdir(parents=True)
            (cover_root / "1" / "cover-one.webp").write_bytes(b"cover")
            self._make_database(db_path, source_root)
            engine = CategoryMigrationEngine(str(work_root), str(cover_root))
            package = engine.export_categories(
                str(db_path),
                "general",
                [1],
            )["files"][0]["path"]
            inspection = engine.inspect_package(package)
            with zipfile.ZipFile(package, "r") as archive:
                members = {
                    item.filename: archive.read(item.filename)
                    for item in archive.infolist()
                    if not item.is_dir()
                }
            metadata = json.loads(members["metadata.json"])
            metadata["books"][0]["relative_path"] = "../outside.cbz"
            members["metadata.json"] = json.dumps(
                metadata,
                ensure_ascii=False,
            ).encode("utf-8")
            with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
                for filename, content in members.items():
                    archive.writestr(filename, content)

            with self.assertRaises(ValueError):
                engine.import_category(
                    str(db_path),
                    package,
                    [str(target_root)],
                    db_type="general",
                    name="실패할 가져오기",
                    backup=False,
                    inspection=inspection,
                )

            connection = sqlite3.connect(db_path)
            library_count = connection.execute(
                "SELECT COUNT(*) FROM libraries"
            ).fetchone()[0]
            book_count = connection.execute(
                "SELECT COUNT(*) FROM books"
            ).fetchone()[0]
            connection.close()
            cover_directories = sorted(
                path.name for path in cover_root.iterdir() if path.is_dir()
            )

            self.assertEqual(1, library_count)
            self.assertEqual(1, book_count)
            self.assertEqual(["1"], cover_directories)

    def test_merges_into_existing_category_and_skips_exact_duplicate_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            source_root = root / "source"
            old_target_root = root / "existing"
            new_target_root = root / "merged"
            cover_root = root / "covers"
            work_root = root / "migration"
            source_root.mkdir()
            old_target_root.mkdir()
            (cover_root / "1").mkdir(parents=True)
            (cover_root / "1" / "cover-one.webp").write_bytes(b"cover")
            self._make_database(db_path, source_root)
            connection = sqlite3.connect(db_path)
            connection.execute(
                "INSERT INTO libraries (name, physical_path) VALUES (?, ?)",
                ("병합 대상", str(old_target_root)),
            )
            connection.commit()
            connection.close()
            engine = CategoryMigrationEngine(str(work_root), str(cover_root))
            package = engine.export_categories(
                str(db_path),
                "general",
                [1],
            )["files"][0]["path"]

            first = engine.import_category(
                str(db_path),
                package,
                [str(new_target_root)],
                db_type="general",
                merge_to=2,
                backup=False,
            )
            second = engine.import_category(
                str(db_path),
                package,
                [str(new_target_root)],
                db_type="general",
                merge_to="병합 대상",
                backup=False,
            )

            connection = sqlite3.connect(db_path)
            physical_path = connection.execute(
                "SELECT physical_path FROM libraries WHERE id = 2"
            ).fetchone()[0]
            books = connection.execute(
                "SELECT file_path, cover_image FROM books WHERE library_id = 2"
            ).fetchall()
            offset_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM book_offsets o
                JOIN books b ON b.id = o.book_id
                WHERE b.library_id = 2
                """
            ).fetchone()[0]
            connection.close()

            self.assertEqual("merge", first["mode"])
            self.assertEqual(1, first["books_count"])
            self.assertEqual(0, first["skipped_duplicate_books_count"])
            self.assertEqual(0, second["books_count"])
            self.assertEqual(1, second["skipped_duplicate_books_count"])
            self.assertEqual(
                [str(old_target_root), str(new_target_root)],
                physical_path.splitlines(),
            )
            self.assertEqual(
                [(str(new_target_root / "테스트 1권.cbz"), "2/cover-one.webp")],
                books,
            )
            self.assertEqual(1, offset_count)
            self.assertTrue((cover_root / "2" / "cover-one.webp").is_file())

    def test_maps_multiple_source_roots_and_falls_back_to_first_target(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            source_one = root / "source-one"
            source_two = root / "source-two"
            target_one = root / "target-one"
            target_two = root / "target-two"
            fallback_root = root / "fallback"
            cover_root = root / "covers"
            work_root = root / "migration"
            source_one.mkdir()
            source_two.mkdir()
            (cover_root / "1").mkdir(parents=True)
            (cover_root / "1" / "cover-one.webp").write_bytes(b"one")
            (cover_root / "1" / "cover-two.webp").write_bytes(b"two")
            self._make_database(db_path, source_one)
            connection = sqlite3.connect(db_path)
            connection.execute(
                "UPDATE libraries SET physical_path = ? WHERE id = 1",
                (f"{source_one}\n{source_two}",),
            )
            connection.execute(
                """
                INSERT INTO books (
                    library_id, title, file_path, file_format, total_pages,
                    has_offsets, cover_image, created_at, file_size, is_deleted
                ) VALUES (1, ?, ?, 'cbz', 1, 0, ?, '2026-07-24 12:00:00', 50, 0)
                """,
                ("테스트 2권", str(source_two / "테스트 2권.cbz"), "1/cover-two.webp"),
            )
            connection.commit()
            connection.close()
            engine = CategoryMigrationEngine(str(work_root), str(cover_root))
            package = engine.export_categories(
                str(db_path),
                "general",
                [1],
            )["files"][0]["path"]

            mapped = engine.import_category(
                str(db_path),
                package,
                [str(target_one), str(target_two)],
                db_type="general",
                name="다중 경로 가져오기",
                backup=False,
            )
            fallback = engine.import_category(
                str(db_path),
                package,
                [str(fallback_root)],
                db_type="general",
                name="단일 경로 폴백",
                backup=False,
            )

            connection = sqlite3.connect(db_path)
            mapped_paths = [
                row[0]
                for row in connection.execute(
                    "SELECT file_path FROM books WHERE library_id = ? ORDER BY title",
                    (mapped["library_id"],),
                ).fetchall()
            ]
            fallback_paths = [
                row[0]
                for row in connection.execute(
                    "SELECT file_path FROM books WHERE library_id = ? ORDER BY title",
                    (fallback["library_id"],),
                ).fetchall()
            ]
            connection.close()

            self.assertEqual(2, mapped["source_books_count"])
            self.assertEqual(2, mapped["books_count"])
            self.assertEqual(0, mapped["fallback_books_count"])
            self.assertEqual(
                [
                    str(target_one / "테스트 1권.cbz"),
                    str(target_two / "테스트 2권.cbz"),
                ],
                mapped_paths,
            )
            self.assertEqual(1, fallback["fallback_books_count"])
            self.assertEqual(
                [
                    str(fallback_root / "테스트 1권.cbz"),
                    str(fallback_root / "테스트 2권.cbz"),
                ],
                fallback_paths,
            )


if __name__ == "__main__":
    unittest.main()
