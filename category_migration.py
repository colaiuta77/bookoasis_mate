# BookOasis 카테고리 패키지를 검사하고 안전하게 내보내거나 신규·병합으로 가져옵니다.
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

try:
    from .cover_inspector import normalize_cover_path, resolve_cover_path
    from .migration_database import MigrationDatabaseContext
except ImportError:
    from cover_inspector import normalize_cover_path, resolve_cover_path
    from migration_database import MigrationDatabaseContext


PACKAGE_VERSION = "2.0"
SUPPORTED_PACKAGE_VERSIONS = {"1.2", "2.0"}
MAX_JSON_SIZE = 64 * 1024 * 1024
BOOK_COLUMNS = (
    "library_id",
    "title",
    "series_name",
    "author",
    "isbn",
    "file_path",
    "file_format",
    "total_pages",
    "has_offsets",
    "cover_image",
    "publisher",
    "link",
    "score",
    "release_date",
    "summary",
    "genre",
    "tags",
    "is_favorite",
    "cover_updated_at",
    "created_at",
    "metadata_locked",
    "file_mtime",
    "file_size",
)
OFFSET_COLUMNS = (
    "book_id",
    "page_idx",
    "filename",
    "local_header_offset",
    "compress_size",
    "file_size",
    "compress_type",
)


class MigrationStopped(RuntimeError):
    pass


def parse_paths(value):
    if not value:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values:
        for part in str(item).replace("\r", "").replace(";", "\n").replace(",", "\n").split("\n"):
            cleaned = part.strip()
            if cleaned and cleaned not in result:
                result.append(cleaned)
    return result


def parse_library_ids(value):
    if not value:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values:
        for part in re.split(r"[\s,;]+", str(item)):
            if part.isdigit() and int(part) > 0 and int(part) not in result:
                result.append(int(part))
    return result


class CategoryMigrationEngine:
    def __init__(
        self,
        work_dir,
        cover_root,
        should_stop=None,
        on_progress=None,
        database_settings=None,
        database_context=None,
    ):
        self.work_root = self._prepare_directory(
            work_dir,
            "이관 작업 디렉터리",
            create=True,
        )
        self.cover_root = self._prepare_directory(
            cover_root,
            "표지 디렉터리",
            create=False,
        )
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress
        self.database_context = database_context
        if self.database_context is None and database_settings:
            self.database_context = MigrationDatabaseContext(
                database_settings,
                work_dir=self.work_root,
                should_stop=self.should_stop,
                on_progress=self.on_progress,
            )

    @staticmethod
    def _prepare_directory(value, label, create=False):
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{label}가 설정되지 않았습니다.")
        path = Path(text).expanduser()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise FileNotFoundError(f"{label}를 찾을 수 없습니다.")
        return path.resolve()

    @staticmethod
    def _connect(db_path, readonly=False):
        path = Path(str(db_path or "")).expanduser()
        if not path.is_file():
            raise FileNotFoundError("BookOasis DB 파일을 찾을 수 없습니다.")
        if readonly:
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=30,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(str(path.resolve()), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _database_connection(self, db_path, db_type, readonly=False):
        if self.database_context is not None:
            return self.database_context.connect(db_type, readonly=readonly)
        return self._connect(db_path, readonly=readonly)

    @staticmethod
    def _tables(connection):
        if hasattr(connection, "tables"):
            return connection.tables()
        return {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    @staticmethod
    def _columns(connection, table):
        if hasattr(connection, "columns"):
            return connection.columns(table)
        safe_table = str(table).replace('"', '""')
        return {
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
        }

    def _progress(self, stage, current=0, total=0, message="", log=None):
        if self.on_progress:
            self.on_progress(
                {
                    "stage": stage,
                    "current": int(current or 0),
                    "total": int(total or 0),
                    "message": str(message or ""),
                    "log": str(log if log is not None else message or ""),
                }
            )

    def _check_stop(self):
        if self.should_stop():
            raise MigrationStopped("사용자가 작업 중지를 요청했습니다.")

    def _work_path(self, value, must_exist=False):
        text = str(value or "").strip()
        if not text:
            raise ValueError("패키지 경로가 비어 있습니다.")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.work_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.work_root)
        except ValueError:
            raise ValueError("패키지는 설정된 이관 작업 디렉터리 안에 있어야 합니다.")
        if must_exist and not resolved.is_file():
            raise FileNotFoundError("카테고리 패키지 파일을 찾을 수 없습니다.")
        return resolved

    @staticmethod
    def _read_json_member(archive, name):
        try:
            info = archive.getinfo(name)
        except KeyError:
            raise ValueError(f"올바른 패키지가 아닙니다. {name} 파일이 없습니다.")
        if info.file_size > MAX_JSON_SIZE:
            raise ValueError(f"{name} 파일이 허용 크기를 초과했습니다.")
        try:
            return json.loads(archive.read(name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{name} 파일을 읽을 수 없습니다: {error}")

    def inspect_package(self, package_path):
        path = self._work_path(package_path, must_exist=True)
        if not path.name.lower().endswith(".oasis.zip"):
            raise ValueError(".oasis.zip 패키지만 사용할 수 있습니다.")
        try:
            with zipfile.ZipFile(str(path), "r") as archive:
                corrupt = archive.testzip()
                if corrupt:
                    raise ValueError(f"ZIP CRC 검사에 실패했습니다: {corrupt}")
                manifest = self._read_json_member(archive, "manifest.json")
                metadata = self._read_json_member(archive, "metadata.json")
                cover_members = [
                    member
                    for member in archive.infolist()
                    if member.filename.startswith("covers/") and not member.is_dir()
                ]
        except zipfile.BadZipFile as error:
            raise ValueError(f"ZIP 패키지를 읽을 수 없습니다: {error}")

        if not isinstance(manifest, dict) or not isinstance(metadata, dict):
            raise ValueError("패키지 메타데이터 형식이 올바르지 않습니다.")
        if str(manifest.get("export_version") or "") not in SUPPORTED_PACKAGE_VERSIONS:
            raise ValueError(
                f"지원하지 않는 패키지 버전입니다: {manifest.get('export_version')}"
            )
        library = metadata.get("library")
        books = metadata.get("books")
        offsets = metadata.get("offsets", {})
        if not isinstance(library, dict) or not isinstance(books, list) or not isinstance(offsets, dict):
            raise ValueError("패키지 library, books 또는 offsets 형식이 올바르지 않습니다.")
        physical_paths = library.get("physical_paths", [])
        if not isinstance(physical_paths, list):
            raise ValueError("패키지 물리 경로 형식이 올바르지 않습니다.")

        return {
            "path": str(path),
            "filename": path.name,
            "size": path.stat().st_size,
            "export_version": str(manifest.get("export_version")),
            "created_at": manifest.get("created_at"),
            "db_type": manifest.get("db_type", "general"),
            "library_id": manifest.get("library_id"),
            "library_name": manifest.get("library_name") or library.get("name"),
            "books_count": len(books),
            "covers_count": len(cover_members),
            "offsets_count": sum(len(items) for items in offsets.values() if isinstance(items, list)),
            "user_progress_count": len(metadata.get("user_progress", [])),
            "user_favorites_count": len(metadata.get("user_favorites", [])),
            "physical_paths": [str(path) for path in physical_paths],
            "root_paths_count": len(physical_paths),
            "cover_uncompressed_size": sum(member.file_size for member in cover_members),
        }

    def list_packages(self):
        packages = []
        for path in sorted(
            self.work_root.glob("*.oasis.zip"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            packages.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
        return packages

    @staticmethod
    def libraries(db_path):
        connection = CategoryMigrationEngine._connect(db_path, readonly=True)
        try:
            if "libraries" not in CategoryMigrationEngine._tables(connection):
                return []
            columns = CategoryMigrationEngine._columns(connection, "libraries")
            if not {"id", "name"}.issubset(columns):
                return []
            rows = connection.execute(
                "SELECT id, name FROM libraries ORDER BY name COLLATE NOCASE, id"
            ).fetchall()
            return [{"id": int(row["id"]), "name": row["name"]} for row in rows]
        finally:
            connection.close()

    def list_libraries(self, db_path, db_type):
        if self.database_context is not None:
            return self.database_context.libraries(db_type)
        return self.libraries(db_path)

    @staticmethod
    def _root_paths(raw_path):
        return [
            part.strip()
            for part in str(raw_path or "").replace("\r", "").replace(";", "\n").split("\n")
            if part.strip()
        ]

    @staticmethod
    def _relative_path(file_path, root_paths):
        source = Path(str(file_path or ""))
        for index, root in enumerate(root_paths):
            try:
                return index, source.relative_to(Path(root)).as_posix()
            except ValueError:
                continue
        if root_paths:
            try:
                return 0, Path(os.path.relpath(str(source), root_paths[0])).as_posix()
            except ValueError:
                pass
        return 0, source.name

    def _cover_path(self, cover_image):
        normalized = normalize_cover_path(cover_image)
        if not normalized:
            return None
        return resolve_cover_path(str(self.cover_root), normalized)

    @staticmethod
    def _safe_filename(name, fallback):
        cleaned = "".join(
            character
            for character in str(name or "")
            if character.isalnum() or character in {"_", "-"}
        ).strip()
        return cleaned or fallback

    def export_categories(self, db_path, db_type, library_ids):
        selected_ids = parse_library_ids(library_ids)
        if not selected_ids:
            raise ValueError("내보낼 카테고리를 선택해 주세요.")
        files = []
        for index, library_id in enumerate(selected_ids, 1):
            self._check_stop()
            self._progress(
                "export",
                index - 1,
                len(selected_ids),
                f"카테고리 {library_id} 내보내기를 준비합니다.",
            )
            result = self._export_one(db_path, db_type, library_id)
            files.append(result)
            self._progress(
                "export",
                index,
                len(selected_ids),
                f"{result['library_name']} 내보내기를 완료했습니다.",
            )
        return {"db_type": db_type, "count": len(files), "files": files}

    def _export_one(self, db_path, db_type, library_id):
        connection = self._database_connection(db_path, db_type, readonly=True)
        partial_path = None
        try:
            tables = self._tables(connection)
            if not {"libraries", "books"}.issubset(tables):
                raise RuntimeError("libraries 또는 books 테이블이 없습니다.")
            connection.execute("BEGIN")
            library_row = connection.execute(
                "SELECT * FROM libraries WHERE id = ?",
                (library_id,),
            ).fetchone()
            if library_row is None:
                raise ValueError(f"카테고리 ID {library_id}를 찾을 수 없습니다.")
            library = dict(library_row)
            library_name = library.get("name") or f"category_{library_id}"
            root_paths = self._root_paths(library.get("physical_path"))
            book_columns = self._columns(connection, "books")
            active = (
                "(is_deleted IS NULL OR is_deleted = 0)"
                if "is_deleted" in book_columns
                else "1 = 1"
            )
            rows = connection.execute(
                f"SELECT * FROM books WHERE library_id = ? AND {active}",
                (library_id,),
            ).fetchall()
            books = [dict(row) for row in rows]
            books_payload = []
            offsets_payload = {}
            user_progress_payload = []
            user_favorites_payload = []
            cover_paths = {}
            has_offsets = "book_offsets" in tables
            offset_columns = self._columns(connection, "book_offsets") if has_offsets else set()
            required_offsets = set(OFFSET_COLUMNS) - {"book_id"}
            can_read_offsets = has_offsets and required_offsets.issubset(offset_columns)

            for book_index, book in enumerate(books):
                self._check_stop()
                root_index, relative_path = self._relative_path(
                    book.get("file_path"),
                    root_paths,
                )
                payload = dict(book)
                payload["root_index"] = root_index
                payload["relative_path"] = relative_path
                books_payload.append(payload)
                cover = self._cover_path(book.get("cover_image"))
                if cover is not None and cover.is_file():
                    try:
                        relative_cover = cover.resolve().relative_to(self.cover_root).as_posix()
                    except ValueError:
                        relative_cover = cover.name
                    cover_paths[relative_cover] = cover
                if can_read_offsets:
                    offset_rows = connection.execute(
                        """
                        SELECT page_idx, filename, local_header_offset,
                               compress_size, file_size, compress_type
                        FROM book_offsets
                        WHERE book_id = ?
                        ORDER BY page_idx
                        """,
                        (book.get("id"),),
                    ).fetchall()
                    if offset_rows:
                        offsets_payload[str(book_index)] = [
                            dict(offset) for offset in offset_rows
                        ]
                if "user_progress" in tables:
                    for progress in connection.execute(
                        "SELECT * FROM user_progress WHERE book_id = ?",
                        (book.get("id"),),
                    ).fetchall():
                        item = dict(progress)
                        item["book_export_index"] = book_index
                        user_progress_payload.append(item)
                if "user_favorites" in tables:
                    for favorite in connection.execute(
                        "SELECT * FROM user_favorites WHERE book_id = ?",
                        (book.get("id"),),
                    ).fetchall():
                        item = dict(favorite)
                        item["book_export_index"] = book_index
                        user_favorites_payload.append(item)
                if book_index % 100 == 0 or book_index + 1 == len(books):
                    self._progress(
                        "export_books",
                        book_index + 1,
                        len(books),
                        f"{library_name} 도서 정보를 수집하고 있습니다.",
                    )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = self._safe_filename(library_name, f"category_{library_id}")
            output = self.work_root / (
                f"{safe_name}_{db_type}_lib{library_id}_{timestamp}.oasis.zip"
            )
            sequence = 1
            while output.exists():
                output = self.work_root / (
                    f"{safe_name}_{db_type}_lib{library_id}_{timestamp}_{sequence}.oasis.zip"
                )
                sequence += 1
            partial_path = output.with_name(output.name + ".part")
            manifest = {
                "export_version": PACKAGE_VERSION,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "db_type": db_type,
                "library_id": library_id,
                "library_name": library_name,
                "root_paths_count": len(root_paths),
                "books_count": len(books_payload),
                "covers_count": len(cover_paths),
                "user_progress_count": len(user_progress_payload),
                "user_favorites_count": len(user_favorites_payload),
                "media_kind": "book",
            }
            metadata = {
                "library": {
                    "id": library_id,
                    "name": library_name,
                    "physical_paths": root_paths,
                    "cron_schedule": library.get("cron_schedule"),
                    "icon": library.get("icon", "fa-book"),
                    "color": library.get("color", "#94a3b8"),
                    "hide_cover": library.get("hide_cover", 0),
                },
                "books": books_payload,
                "offsets": offsets_payload,
                "user_progress": user_progress_payload,
                "user_favorites": user_favorites_payload,
            }
            with zipfile.ZipFile(
                str(partial_path),
                "w",
                compression=zipfile.ZIP_STORED,
            ) as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                archive.writestr(
                    "metadata.json",
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                )
                for cover_index, (relative, cover_path) in enumerate(
                    sorted(cover_paths.items()),
                    1,
                ):
                    self._check_stop()
                    archive.write(str(cover_path), arcname=f"covers/{relative}")
                    self._progress(
                        "export_covers",
                        cover_index,
                        len(cover_paths),
                        f"{library_name} 표지를 패키징하고 있습니다.",
                    )
            os.replace(str(partial_path), str(output))
            partial_path = None
            return {
                "path": str(output),
                "filename": output.name,
                "size": output.stat().st_size,
                "library_id": library_id,
                "library_name": library_name,
                "books_count": len(books_payload),
                "covers_count": len(cover_paths),
                "offsets_count": sum(len(items) for items in offsets_payload.values()),
                "user_progress_count": len(user_progress_payload),
                "user_favorites_count": len(user_favorites_payload),
            }
        except Exception:
            if partial_path is not None:
                try:
                    partial_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            connection.close()

    def _quick_check(self, connection, db_type):
        if self.database_context is not None:
            self.database_context.integrity_check(db_type)
            return
        rows = connection.execute("PRAGMA quick_check").fetchall()
        result = [str(row[0]) for row in rows]
        if result != ["ok"]:
            raise RuntimeError("대상 DB quick_check에 실패했습니다: " + ", ".join(result[:10]))

    def _check_required_sqlite_modules(self, connection):
        if self.database_context is not None and self.database_context.engine != "sqlite":
            return
        rows = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table' AND LOWER(COALESCE(sql, '')) LIKE '%using fts5%'
            """
        ).fetchall()
        if not rows:
            return
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE temp.bookoasis_mate_fts5_probe USING fts5(value)"
            )
            connection.execute("DROP TABLE temp.bookoasis_mate_fts5_probe")
        except sqlite3.Error as error:
            raise RuntimeError(
                "FlaskFarm Python SQLite에서 FTS5를 사용할 수 없어 "
                f"BookOasis DB에 안전하게 가져올 수 없습니다: {error}"
            )

    def _backup_database(self, db_path, db_type):
        if self.database_context is not None:
            return self.database_context.backup(db_type, reason="before_import")
        backup_dir = self.work_root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        source_name = Path(db_path).stem
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{source_name}_{db_type}_before_import_{timestamp}.db"
        sequence = 1
        while target.exists():
            target = backup_dir / (
                f"{source_name}_{db_type}_before_import_{timestamp}_{sequence}.db"
            )
            sequence += 1
        source = self._connect(db_path, readonly=True)
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        except Exception:
            destination.close()
            source.close()
            try:
                target.unlink()
            except OSError:
                pass
            raise
        finally:
            try:
                destination.close()
            except sqlite3.Error:
                pass
            source.close()
        return target

    @staticmethod
    def _safe_relative_path(value):
        pure = PurePosixPath(str(value or "").replace("\\", "/"))
        if (
            not pure.parts
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValueError(f"안전하지 않은 도서 상대 경로입니다: {value}")
        return pure

    @staticmethod
    def _insert_row(connection, table, values):
        columns = list(values.keys())
        placeholders = ", ".join("?" for _ in columns)
        safe_columns = ", ".join(f'`{column}`' for column in columns)
        cursor = connection.execute(
            f'INSERT INTO `{table}` ({safe_columns}) VALUES ({placeholders})',
            tuple(values[column] for column in columns),
        )
        return cursor.lastrowid

    @classmethod
    def _upsert_row(cls, connection, table, key_columns, values):
        where = " AND ".join(f"`{column}` = ?" for column in key_columns)
        existing = connection.execute(
            f"SELECT id FROM `{table}` WHERE {where}",
            tuple(values[column] for column in key_columns),
        ).fetchone()
        if existing is None:
            return cls._insert_row(connection, table, values)
        update_columns = [
            column for column in values
            if column not in key_columns and column != "id"
        ]
        if update_columns:
            assignments = ", ".join(f"`{column}` = ?" for column in update_columns)
            connection.execute(
                f"UPDATE `{table}` SET {assignments} WHERE id = ?",
                tuple(values[column] for column in update_columns)
                + (int(existing["id"]),),
            )
        return int(existing["id"])

    def import_category(
        self,
        db_path,
        package_path,
        target_paths,
        db_type=None,
        name=None,
        merge_to=None,
        backup=True,
        inspection=None,
    ):
        inspection = inspection or self.inspect_package(package_path)
        selected_db_type = db_type or inspection["db_type"] or "general"
        paths = parse_paths(target_paths)
        if not paths:
            raise ValueError("가져올 대상 물리 경로를 하나 이상 입력해 주세요.")
        resolved_targets = []
        for raw_path in paths:
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                raise ValueError("대상 물리 경로는 절대 경로로 입력해 주세요.")
            target.mkdir(parents=True, exist_ok=True)
            resolved_targets.append(Path(os.path.abspath(str(target))))

        self._check_stop()
        self._progress("preflight", 0, 1, "패키지와 대상 DB를 검사하고 있습니다.")
        connection = self._database_connection(
            db_path, selected_db_type, readonly=False
        )
        try:
            self._check_required_sqlite_modules(connection)
            self._quick_check(connection, selected_db_type)
        finally:
            connection.close()
        package = self._work_path(package_path, must_exist=True)
        free_space = shutil.disk_usage(str(self.cover_root)).free
        if inspection["cover_uncompressed_size"] > free_space:
            raise OSError("표지 복원에 필요한 디스크 여유 공간이 부족합니다.")

        backup_path = None
        if backup:
            self._check_stop()
            self._progress("backup", 0, 1, "가져오기 전 대상 DB를 백업하고 있습니다.")
            backup_path = self._backup_database(db_path, selected_db_type)
            self._progress("backup", 1, 1, "대상 DB 백업을 완료했습니다.")

        stage = Path(tempfile.mkdtemp(prefix=".bookoasis_mate_import_", dir=str(self.cover_root)))
        final_cover_dir = None
        connection = None
        moved_stage = False
        moved_cover_names = []
        backed_up_cover_names = []
        rollback_cover_dir = None
        created_merge_cover_dir = False
        database_committed = False
        try:
            with zipfile.ZipFile(str(package), "r") as archive:
                metadata = self._read_json_member(archive, "metadata.json")
                books = metadata["books"]
                offsets = metadata.get("offsets", {})
                user_progress = metadata.get("user_progress", [])
                user_favorites = metadata.get("user_favorites", [])
                library_meta = metadata["library"]
                cover_members = [
                    member
                    for member in archive.infolist()
                    if member.filename.startswith("covers/") and not member.is_dir()
                ]
                staged_names = set()
                for cover_index, member in enumerate(cover_members, 1):
                    self._check_stop()
                    filename = Path(member.filename).name
                    if not filename or filename in staged_names:
                        raise ValueError(f"중복되거나 잘못된 표지 파일명입니다: {member.filename}")
                    staged_names.add(filename)
                    target = stage / filename
                    with archive.open(member, "r") as source, target.open("wb") as destination:
                        while True:
                            self._check_stop()
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            destination.write(chunk)
                    self._progress(
                        "import_covers",
                        cover_index,
                        len(cover_members),
                        "표지를 임시 디렉터리에 복원하고 있습니다.",
                    )

            self._check_stop()
            connection = self._database_connection(
                db_path, selected_db_type, readonly=False
            )
            tables = self._tables(connection)
            if not {"libraries", "books"}.issubset(tables):
                raise RuntimeError("libraries 또는 books 테이블이 없습니다.")
            library_columns = self._columns(connection, "libraries")
            book_columns = self._columns(connection, "books")
            if not {"name", "physical_path"}.issubset(library_columns):
                raise RuntimeError("libraries 필수 컬럼이 없습니다.")
            if not {"library_id", "title", "file_path"}.issubset(book_columns):
                raise RuntimeError("books 필수 컬럼이 없습니다.")
            if offsets and "book_offsets" not in tables:
                raise RuntimeError("패키지 offset을 복원할 book_offsets 테이블이 없습니다.")
            offset_columns = (
                self._columns(connection, "book_offsets")
                if "book_offsets" in tables
                else set()
            )

            if hasattr(connection, "begin"):
                connection.begin(immediate=True)
            else:
                connection.execute("BEGIN IMMEDIATE")
            merge_value = str(merge_to or "").strip()
            mode = "merge" if merge_value else "new"
            added_physical_paths = []
            if mode == "merge":
                target_library = None
                if merge_value.isdigit():
                    target_library = connection.execute(
                        "SELECT id, name, physical_path FROM libraries WHERE id = ?",
                        (int(merge_value),),
                    ).fetchone()
                if target_library is None:
                    target_library = connection.execute(
                        "SELECT id, name, physical_path FROM libraries WHERE name = ?",
                        (merge_value,),
                    ).fetchone()
                if target_library is None:
                    raise ValueError(
                        f"병합 대상 카테고리를 찾을 수 없습니다: {merge_value}"
                    )
                new_library_id = int(target_library["id"])
                target_name = str(target_library["name"])
                physical_paths = [
                    item.strip()
                    for item in str(target_library["physical_path"] or "").splitlines()
                    if item.strip()
                ]
                for path in resolved_targets:
                    text = str(path)
                    if text not in physical_paths:
                        physical_paths.append(text)
                        added_physical_paths.append(text)
                connection.execute(
                    "UPDATE libraries SET physical_path = ? WHERE id = ?",
                    ("\n".join(physical_paths), new_library_id),
                )
            else:
                target_name = str(
                    name or library_meta.get("name") or "Imported Library"
                ).strip()
                if not target_name:
                    raise ValueError("신규 카테고리 이름이 비어 있습니다.")
                collision = connection.execute(
                    "SELECT id FROM libraries WHERE name = ?",
                    (target_name,),
                ).fetchone()
                if collision is not None:
                    base_name = (
                        f"{target_name} (Imported {datetime.now().strftime('%H%M%S')})"
                    )
                    target_name = base_name
                    suffix = 2
                    while connection.execute(
                        "SELECT id FROM libraries WHERE name = ?",
                        (target_name,),
                    ).fetchone() is not None:
                        target_name = f"{base_name}-{suffix}"
                        suffix += 1
                physical_paths = [str(path) for path in resolved_targets]
                added_physical_paths = list(physical_paths)
                library_values = {
                    "name": target_name,
                    "physical_path": "\n".join(physical_paths),
                    "cron_schedule": library_meta.get("cron_schedule"),
                    "icon": library_meta.get("icon", "fa-book"),
                    "color": library_meta.get("color", "#94a3b8"),
                    "hide_cover": library_meta.get("hide_cover", 0),
                }
                library_values = {
                    key: value
                    for key, value in library_values.items()
                    if key in library_columns
                }
                new_library_id = self._insert_row(
                    connection,
                    "libraries",
                    library_values,
                )

            existing_book_count = connection.execute(
                "SELECT COUNT(*) FROM books WHERE library_id = ?",
                (new_library_id,),
            ).fetchone()[0]
            existing_offset_count = 0
            if "book_offsets" in tables:
                existing_offset_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM book_offsets o
                    JOIN books b ON b.id = o.book_id
                    WHERE b.library_id = ?
                    """,
                    (new_library_id,),
                ).fetchone()[0]
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            imported_offsets = 0
            imported_books = 0
            skipped_duplicate_books = 0
            fallback_books = 0
            book_index_to_id = {}
            for book_index, book in enumerate(books):
                self._check_stop()
                relative = self._safe_relative_path(book.get("relative_path"))
                root_index = int(book.get("root_index") or 0)
                if root_index < 0 or root_index >= len(resolved_targets):
                    root_index = 0
                    fallback_books += 1
                file_path = resolved_targets[root_index].joinpath(*relative.parts)
                duplicate = connection.execute(
                    "SELECT id FROM books WHERE file_path = ?",
                    (str(file_path),),
                ).fetchone()
                if duplicate is not None:
                    skipped_duplicate_books += 1
                    book_index_to_id[book_index] = int(duplicate["id"])
                else:
                    cover_image = str(book.get("cover_image") or "").strip()
                    if cover_image:
                        cover_image = f"{new_library_id}/{Path(cover_image).name}"
                    values = {
                        "library_id": new_library_id,
                        "title": book.get("title") or "Unknown Title",
                        "series_name": book.get("series_name"),
                        "author": book.get("author"),
                        "isbn": book.get("isbn"),
                        "file_path": str(file_path),
                        "file_format": book.get("file_format", "zip"),
                        "total_pages": book.get("total_pages", 0),
                        "has_offsets": book.get("has_offsets", 0),
                        "cover_image": cover_image,
                        "publisher": book.get("publisher"),
                        "link": book.get("link"),
                        "score": book.get("score"),
                        "release_date": book.get("release_date"),
                        "summary": book.get("summary"),
                        "genre": book.get("genre"),
                        "tags": book.get("tags"),
                        "is_favorite": book.get("is_favorite", 0),
                        "cover_updated_at": book.get("cover_updated_at") or now,
                        "created_at": now,
                        "metadata_locked": book.get("metadata_locked", 0),
                        "file_mtime": book.get("file_mtime", 0.0),
                        "file_size": book.get("file_size", 0),
                    }
                    values = {
                        key: value
                        for key, value in values.items()
                        if key in book_columns and key in BOOK_COLUMNS
                    }
                    new_book_id = self._insert_row(connection, "books", values)
                    book_index_to_id[book_index] = int(new_book_id)
                    imported_books += 1
                    offset_items = offsets.get(
                        str(book_index),
                        offsets.get(book_index, []),
                    )
                    for offset in (
                        offset_items if isinstance(offset_items, list) else []
                    ):
                        offset_values = {
                            "book_id": new_book_id,
                            "page_idx": offset.get("page_idx"),
                            "filename": offset.get("filename"),
                            "local_header_offset": offset.get("local_header_offset"),
                            "compress_size": offset.get("compress_size"),
                            "file_size": offset.get("file_size"),
                            "compress_type": offset.get("compress_type"),
                        }
                        offset_values = {
                            key: value
                            for key, value in offset_values.items()
                            if key in offset_columns and key in OFFSET_COLUMNS
                        }
                        self._insert_row(connection, "book_offsets", offset_values)
                        imported_offsets += 1
                if book_index % 100 == 0 or book_index + 1 == len(books):
                    self._progress(
                        "import_books",
                        book_index + 1,
                        len(books),
                        "도서와 offset을 대상 카테고리에 저장하고 있습니다.",
                    )

            valid_users = set()
            if "users" in tables:
                valid_users = {
                    int(row["id"])
                    for row in connection.execute("SELECT id FROM users").fetchall()
                }
            imported_progress = 0
            skipped_progress = 0
            if "user_progress" in tables:
                progress_columns = self._columns(connection, "user_progress")
                for item in user_progress if isinstance(user_progress, list) else []:
                    export_index = int(item.get("book_export_index", -1))
                    user_id = int(item.get("user_id") or 0)
                    if export_index not in book_index_to_id or user_id not in valid_users:
                        skipped_progress += 1
                        continue
                    values = {
                        key: value
                        for key, value in item.items()
                        if key not in {"id", "book_export_index", "book_id"}
                        and key in progress_columns
                    }
                    values["book_id"] = book_index_to_id[export_index]
                    values["user_id"] = user_id
                    self._upsert_row(
                        connection,
                        "user_progress",
                        ("book_id", "user_id"),
                        values,
                    )
                    imported_progress += 1

            imported_favorites = 0
            skipped_favorites = 0
            if "user_favorites" in tables:
                favorite_columns = self._columns(connection, "user_favorites")
                for item in user_favorites if isinstance(user_favorites, list) else []:
                    export_index = int(item.get("book_export_index", -1))
                    user_id = int(item.get("user_id") or 0)
                    if export_index not in book_index_to_id or user_id not in valid_users:
                        skipped_favorites += 1
                        continue
                    values = {
                        key: value
                        for key, value in item.items()
                        if key not in {"id", "book_export_index", "book_id"}
                        and key in favorite_columns
                    }
                    values["book_id"] = book_index_to_id[export_index]
                    values["user_id"] = user_id
                    self._upsert_row(
                        connection,
                        "user_favorites",
                        ("user_id", "book_id"),
                        values,
                    )
                    imported_favorites += 1

            if "series_summary" in tables:
                connection.execute(
                    "DELETE FROM series_summary WHERE library_id = ?",
                    (new_library_id,),
                )
            if "series_summary_state" in tables:
                state = connection.execute(
                    "SELECT id FROM series_summary_state WHERE id = 1"
                ).fetchone()
                if state is None:
                    connection.execute(
                        "INSERT INTO series_summary_state "
                        "(id, is_ready, refreshed_at) VALUES (1, 0, NULL)"
                    )
                else:
                    connection.execute(
                        "UPDATE series_summary_state SET is_ready = 0, "
                        "refreshed_at = NULL WHERE id = 1"
                    )

            final_cover_dir = self.cover_root / str(new_library_id)
            if mode == "new":
                if final_cover_dir.exists():
                    raise FileExistsError(
                        "신규 카테고리 표지 디렉터리가 이미 존재합니다."
                    )
                os.replace(str(stage), str(final_cover_dir))
                moved_stage = True
            elif staged_names:
                if not final_cover_dir.exists():
                    final_cover_dir.mkdir(parents=True)
                    created_merge_cover_dir = True
                rollback_cover_dir = Path(
                    tempfile.mkdtemp(
                        prefix=".bookoasis_mate_cover_rollback_",
                        dir=str(self.cover_root),
                    )
                )
                for filename in sorted(staged_names):
                    source = stage / filename
                    destination = final_cover_dir / filename
                    if destination.exists():
                        if destination.is_symlink() or not destination.is_file():
                            raise RuntimeError(
                                f"병합 대상 표지 경로가 일반 파일이 아닙니다: {filename}"
                            )
                        os.replace(
                            str(destination),
                            str(rollback_cover_dir / filename),
                        )
                        backed_up_cover_names.append(filename)
                    os.replace(str(source), str(destination))
                    moved_cover_names.append(filename)

            verified_books = connection.execute(
                "SELECT COUNT(*) FROM books WHERE library_id = ?",
                (new_library_id,),
            ).fetchone()[0]
            if verified_books != existing_book_count + imported_books:
                raise RuntimeError("가져온 도서 수 검증에 실패했습니다.")
            if "book_offsets" in tables:
                verified_offsets = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM book_offsets o
                    JOIN books b ON b.id = o.book_id
                    WHERE b.library_id = ?
                    """,
                    (new_library_id,),
                ).fetchone()[0]
                if verified_offsets != existing_offset_count + imported_offsets:
                    raise RuntimeError("가져온 offset 수 검증에 실패했습니다.")
            if not all(
                (final_cover_dir / filename).is_file()
                for filename in staged_names
            ):
                raise RuntimeError("복원한 표지 수 검증에 실패했습니다.")
            connection.commit()
            database_committed = True
            if rollback_cover_dir is not None:
                shutil.rmtree(str(rollback_cover_dir), ignore_errors=True)
            self._progress("verify", 1, 1, "가져오기 결과를 검증했습니다.")
            return {
                "mode": mode,
                "db_type": selected_db_type,
                "library_id": new_library_id,
                "library_name": target_name,
                "physical_paths": physical_paths,
                "added_physical_paths": added_physical_paths,
                "source_books_count": len(books),
                "books_count": imported_books,
                "skipped_duplicate_books_count": skipped_duplicate_books,
                "fallback_books_count": fallback_books,
                "offsets_count": imported_offsets,
                "user_progress_count": imported_progress,
                "skipped_user_progress_count": skipped_progress,
                "user_favorites_count": imported_favorites,
                "skipped_user_favorites_count": skipped_favorites,
                "covers_count": len(staged_names),
                "backup_path": str(backup_path) if backup_path else "",
                "package_path": str(package),
            }
        except Exception:
            if connection is not None and not database_committed:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if not database_committed:
                if moved_stage and final_cover_dir is not None:
                    shutil.rmtree(str(final_cover_dir), ignore_errors=True)
                if final_cover_dir is not None:
                    for filename in moved_cover_names:
                        destination = final_cover_dir / filename
                        if destination.exists():
                            destination.unlink()
                    if rollback_cover_dir is not None:
                        for filename in backed_up_cover_names:
                            backup_cover = rollback_cover_dir / filename
                            if backup_cover.exists():
                                os.replace(
                                    str(backup_cover),
                                    str(final_cover_dir / filename),
                                )
                    if created_merge_cover_dir and final_cover_dir.exists():
                        try:
                            final_cover_dir.rmdir()
                        except OSError:
                            pass
            raise
        finally:
            if connection is not None:
                connection.close()
            if stage.exists():
                shutil.rmtree(str(stage), ignore_errors=True)
            if rollback_cover_dir is not None and rollback_cover_dir.exists():
                shutil.rmtree(str(rollback_cover_dir), ignore_errors=True)
