# 공유 BookOasis DB·표지 tar.gz 세트를 검사하고 안전하게 교체 가져옵니다.
import gzip
import hashlib
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

try:
    from .bookoasis_db import resolve_engine
    from .kavita_migration import parse_mapping_lines
except ImportError:
    from bookoasis_db import resolve_engine
    from kavita_migration import parse_mapping_lines


DB_MEMBERS_REQUIRED = {"db/media_general.db", "db/media_adult.db"}
DB_MEMBERS_ALLOWED = DB_MEMBERS_REQUIRED | {"db/media_audiobook.db"}
MAX_ARCHIVE_MEMBERS = 1_000_000
MAX_ARCHIVE_UNCOMPRESSED = 2 * 1024**4
COPY_CHUNK_SIZE = 1024 * 1024
DB_BATCH_SIZE = 1000
PACKAGE_EXTENSIONS = (".tar.gz", ".tgz")


@contextmanager
def open_gzip_tar_writer(path, compresslevel=1):
    with Path(path).open("wb") as raw_stream:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_stream,
            compresslevel=compresslevel,
            mtime=0,
        ) as gzip_stream:
            with tarfile.open(fileobj=gzip_stream, mode="w|") as archive:
                yield archive


class BookOasisPackageImportStopped(RuntimeError):
    pass


def normalize_member_name(value):
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if not text or text.startswith("/") or any(part in {"", ".."} for part in path.parts):
        raise ValueError(f"안전하지 않은 압축 경로입니다: {text}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"안전하지 않은 압축 경로입니다: {text}")
    return path.as_posix().rstrip("/")


def normalize_media_path(value):
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def apply_media_mappings(value, mappings):
    original = str(value or "").strip()
    normalized = normalize_media_path(original)
    normalized_rules = [
        (normalize_media_path(source), normalize_media_path(target))
        for source, target in mappings
    ]
    for source, target in sorted(
        normalized_rules, key=lambda item: len(item[0]), reverse=True
    ):
        if normalized == source:
            return target
        if normalized.startswith(source + "/"):
            return target + normalized[len(source) :]
    return original


def standard_cover_relative(library_id, file_path):
    digest = hashlib.md5(str(file_path or "").encode("utf-8")).hexdigest()
    filename = f"book_{digest}.webp"
    return f"{int(library_id)}/{filename}" if library_id is not None else filename


def replace_library_mappings(value, mappings):
    text = str(value or "")
    if not text:
        return text
    return "\n".join(
        apply_media_mappings(line, mappings) if line.strip() else line
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )


def package_archive_kind(path):
    name = Path(path).name
    lower = name.lower()
    if not lower.endswith(PACKAGE_EXTENSIONS):
        return None
    for kind, prefix in (("databases", "db_"), ("covers", "covers_")):
        if lower.startswith(prefix):
            return kind
    return None


def normalize_export_name(value):
    text = str(value or "dist_books").strip()
    safe = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._-")
    if not safe:
        safe = "dist_books"
    return safe[:80]


def suggested_gdrive_parent(paths):
    parents = set()
    found = False
    for value in paths:
        normalized = normalize_media_path(value)
        if not normalized:
            continue
        parts = [part for part in normalized.split("/") if part]
        marker_index = next(
            (index for index, part in enumerate(parts) if part.lower() == "gdrive"),
            None,
        )
        if marker_index is None:
            return ""
        found = True
        prefix = "/".join(parts[:marker_index])
        if normalized.startswith("/"):
            prefix = "/" + prefix if prefix else "/"
        parents.add(prefix)
    return next(iter(parents)) if found and len(parents) == 1 else ""


class BookOasisPackageImportEngine:
    def __init__(
        self,
        work_dir,
        target_general_db,
        target_adult_db,
        target_cover_root,
        health_check=None,
        should_stop=None,
        on_progress=None,
        database_settings=None,
    ):
        self.work_root = self._required_directory(work_dir, "이관 작업 디렉터리")
        self.database_settings = dict(database_settings or {})
        self.target_databases = {
            "general": self._path(target_general_db),
            "adult": self._path(target_adult_db),
            "audiobook": self._path(
                self.database_settings.get("audiobook_db_path")
                or self.database_settings.get("target_audiobook_db")
            ) if self.database_settings else None,
        }
        self.target_cover_root = self._path(target_cover_root)
        self.health_check = health_check or (lambda: {"success": False})
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress
        self.mariadb_engine = None
        if self.database_settings and resolve_engine(self.database_settings) == "mariadb":
            try:
                from .mariadb_package import MariaDBPackageEngine
            except ImportError:
                from mariadb_package import MariaDBPackageEngine
            self.mariadb_engine = MariaDBPackageEngine(
                self.work_root,
                self.target_cover_root,
                self.database_settings,
                health_check=self.health_check,
                should_stop=self.should_stop,
                on_progress=self.on_progress,
            )

    @staticmethod
    def _path(value):
        text = str(value or "").strip()
        return Path(text).expanduser().resolve() if text else None

    @staticmethod
    def _required_directory(value, label):
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{label}가 설정되지 않았습니다.")
        path = Path(text).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise FileNotFoundError(f"{label}를 찾을 수 없습니다.")
        return path.resolve()

    @staticmethod
    def _connect(path, readonly=False):
        if readonly:
            connection = sqlite3.connect(
                f"{Path(path).resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=30,
            )
            connection.execute("PRAGMA query_only = ON")
        else:
            connection = sqlite3.connect(str(Path(path).resolve()), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _tables(connection):
        return {
            row["name"]
            for row in BookOasisPackageImportEngine._iter_rows(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
        }

    @staticmethod
    def _iter_rows(cursor, batch_size=DB_BATCH_SIZE):
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield from rows

    @staticmethod
    def _archive_fingerprint(path):
        stat = Path(path).stat()
        return {
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }

    def _check_stop(self):
        if self.should_stop():
            raise BookOasisPackageImportStopped(
                "사용자가 BookOasis 패키지 가져오기를 중지했습니다."
            )

    def _progress(self, stage, current=0, total=0, message=""):
        if self.on_progress:
            self.on_progress(
                {
                    "stage": stage,
                    "current": int(current or 0),
                    "total": int(total or 0),
                    "message": str(message or ""),
                    "log": str(message or ""),
                }
            )

    def _work_file(self, value, label):
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{label}을 선택해 주세요.")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = self.work_root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.work_root)
        except ValueError:
            raise ValueError(f"{label}은 이관 작업 디렉터리 안에 있어야 합니다.")
        if not resolved.is_file():
            raise FileNotFoundError(f"{label}을 찾을 수 없습니다.")
        if not (
            resolved.name.lower().endswith(".tar.gz")
            or resolved.name.lower().endswith(".tgz")
        ):
            raise ValueError(f"{label}은 .tar.gz 또는 .tgz 파일이어야 합니다.")
        return resolved

    def list_packages(self):
        packages = {"databases": [], "covers": []}
        for path in self.work_root.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            kind = package_archive_kind(path)
            if kind is None:
                continue
            resolved = path.resolve()
            packages[kind].append(
                {
                    "name": path.name,
                    "path": str(resolved),
                    "size": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                    "_modified_timestamp": path.stat().st_mtime,
                }
            )
        for kind in packages:
            packages[kind].sort(
                key=lambda item: (item["_modified_timestamp"], item["name"]),
                reverse=True,
            )
            for item in packages[kind]:
                item.pop("_modified_timestamp", None)
        return packages

    @staticmethod
    def _backup_database_to(source_path, target_path, should_stop=None):
        source = BookOasisPackageImportEngine._connect(
            source_path,
            readonly=True,
        )
        destination = sqlite3.connect(str(target_path))
        try:
            def progress(_status, _remaining, _total):
                if should_stop:
                    should_stop()

            source.backup(
                destination,
                pages=1000,
                progress=progress,
            )
        finally:
            destination.close()
            source.close()

    @staticmethod
    def _quick_check_database(path, label):
        connection = BookOasisPackageImportEngine._connect(
            path,
            readonly=True,
        )
        try:
            result = [
                str(row[0])
                for row in BookOasisPackageImportEngine._iter_rows(
                    connection.execute("PRAGMA quick_check")
                )
            ]
        finally:
            connection.close()
        if result != ["ok"]:
            raise RuntimeError(
                f"{label} DB quick_check에 실패했습니다: "
                + ", ".join(result[:10])
            )

    def _cover_export_stats(self):
        if self.target_cover_root is None or not self.target_cover_root.is_dir():
            raise FileNotFoundError(
                "내보낼 BookOasis 표지 디렉터리를 찾을 수 없습니다."
            )
        file_count = 0
        total_size = 0
        for current_root, directories, filenames in os.walk(
            self.target_cover_root,
            followlinks=False,
        ):
            self._check_stop()
            directories.sort()
            filenames.sort()
            for name in directories:
                if (Path(current_root) / name).is_symlink():
                    raise ValueError(
                        "표지 디렉터리의 심볼릭 링크는 내보낼 수 없습니다: "
                        f"{Path(current_root) / name}"
                    )
            for name in filenames:
                path = Path(current_root) / name
                if path.is_symlink():
                    raise ValueError(
                        f"표지 파일의 심볼릭 링크는 내보낼 수 없습니다: {path}"
                    )
                if not path.is_file():
                    continue
                file_count += 1
                total_size += path.stat().st_size
        return file_count, total_size

    def _write_cover_archive(self, target_path, total_files):
        completed = 0
        with open_gzip_tar_writer(target_path, compresslevel=1) as archive:
            archive.add(
                str(self.target_cover_root),
                arcname="covers",
                recursive=False,
            )
            for current_root, directories, filenames in os.walk(
                self.target_cover_root,
                followlinks=False,
            ):
                self._check_stop()
                directories.sort()
                filenames.sort()
                root = Path(current_root)
                for name in directories:
                    path = root / name
                    relative = path.relative_to(self.target_cover_root)
                    archive.add(
                        str(path),
                        arcname=(
                            PurePosixPath("covers")
                            / PurePosixPath(relative.as_posix())
                        ).as_posix(),
                        recursive=False,
                    )
                for name in filenames:
                    path = root / name
                    if not path.is_file():
                        continue
                    relative = path.relative_to(self.target_cover_root)
                    archive.add(
                        str(path),
                        arcname=(
                            PurePosixPath("covers")
                            / PurePosixPath(relative.as_posix())
                        ).as_posix(),
                        recursive=False,
                    )
                    completed += 1
                    if completed == total_files or completed % 500 == 0:
                        self._progress(
                            "export_covers",
                            completed,
                            total_files,
                            "표지 패키지 "
                            f"{completed}/{total_files}개 파일을 저장했습니다.",
                        )

    def export_package(self, export_name="dist_books", timestamp=None):
        name = normalize_export_name(export_name)
        stamp = timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:12]
        final_database = self.work_root / f"db_{name}_{stamp}.tar.gz"
        final_covers = self.work_root / f"covers_{name}_{stamp}.tar.gz"
        if final_database.exists() or final_covers.exists():
            raise FileExistsError(
                "같은 이름의 BookOasis 내보내기 패키지가 이미 있습니다."
            )
        if self.mariadb_engine is None:
            for db_type in ("general", "adult"):
                source = self.target_databases[db_type]
                if source is None or not source.is_file():
                    raise FileNotFoundError(
                        f"내보낼 BookOasis {db_type} DB 파일을 찾을 수 없습니다."
                    )
        total_cover_files, cover_source_size = self._cover_export_stats()
        stage = Path(
            tempfile.mkdtemp(
                prefix=".bookoasis_package_export_",
                dir=str(self.work_root),
            )
        )
        temporary_database = self.work_root / f".db_export_{token}.tar.gz"
        temporary_covers = self.work_root / f".covers_export_{token}.tar.gz"
        database_published = False
        covers_published = False
        try:
            if self.mariadb_engine is not None:
                manifest = self.mariadb_engine.export_database_archive(
                    temporary_database
                )
                self._write_cover_archive(
                    temporary_covers,
                    total_cover_files,
                )
                if total_cover_files == 0:
                    self._progress(
                        "export_covers",
                        1,
                        1,
                        "빈 표지 패키지를 저장했습니다.",
                    )
                self._check_stop()
                os.replace(str(temporary_database), str(final_database))
                database_published = True
                os.replace(str(temporary_covers), str(final_covers))
                covers_published = True
                return {
                    "source_type": "bookoasis",
                    "package_action": "export",
                    "package_engine": "mariadb",
                    "package_version": manifest.get("version"),
                    "dry_run": False,
                    "database_package_path": str(final_database),
                    "database_package_size": final_database.stat().st_size,
                    "cover_package_path": str(final_covers),
                    "cover_package_size": final_covers.stat().st_size,
                    "database_count": len(manifest.get("databases") or []),
                    "cover_files_count": total_cover_files,
                    "cover_source_size": cover_source_size,
                }
            database_root = stage / "db"
            database_root.mkdir(parents=True)
            export_db_types = ["general", "adult"]
            if (
                self.target_databases.get("audiobook") is not None
                and self.target_databases["audiobook"].is_file()
            ):
                export_db_types.append("audiobook")
            for index, db_type in enumerate(export_db_types, start=1):
                self._check_stop()
                destination = database_root / f"media_{db_type}.db"
                self._progress(
                    "export_databases",
                    index - 1,
                    len(export_db_types),
                    f"{db_type} DB를 SQLite backup API로 복사하고 있습니다.",
                )
                self._backup_database_to(
                    self.target_databases[db_type],
                    destination,
                    should_stop=self._check_stop,
                )
                self._quick_check_database(destination, db_type)
                self._progress(
                    "export_databases",
                    index,
                    len(export_db_types),
                    f"{db_type} DB 백업을 완료했습니다.",
                )
            with open_gzip_tar_writer(
                temporary_database,
                compresslevel=1,
            ) as archive:
                archive.add(
                    str(database_root),
                    arcname="db",
                    recursive=True,
                )
            self._write_cover_archive(
                temporary_covers,
                total_cover_files,
            )
            if total_cover_files == 0:
                self._progress(
                    "export_covers",
                    1,
                    1,
                    "빈 표지 패키지를 저장했습니다.",
                )
            self._check_stop()
            os.replace(str(temporary_database), str(final_database))
            database_published = True
            os.replace(str(temporary_covers), str(final_covers))
            covers_published = True
            return {
                "source_type": "bookoasis",
                "package_action": "export",
                "dry_run": False,
                "database_package_path": str(final_database),
                "database_package_size": final_database.stat().st_size,
                "cover_package_path": str(final_covers),
                "cover_package_size": final_covers.stat().st_size,
                "database_count": len(export_db_types),
                "cover_files_count": total_cover_files,
                "cover_source_size": cover_source_size,
            }
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)
            for path in (temporary_database, temporary_covers):
                if path.exists():
                    path.unlink()
            if database_published and not covers_published:
                final_database.unlink(missing_ok=True)

    def _inspect_archive(self, archive_path, kind):
        regular_names = set()
        cover_names = set()
        member_count = 0
        file_count = 0
        total_size = 0
        try:
            with tarfile.open(str(archive_path), "r|gz") as archive:
                for member in archive:
                    self._check_stop()
                    name = normalize_member_name(member.name)
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise ValueError(f"링크·장치 압축 항목은 허용하지 않습니다: {name}")
                    if not member.isdir() and not member.isfile():
                        raise ValueError(f"지원하지 않는 압축 항목입니다: {name}")
                    if member.isfile():
                        if kind == "database" and name not in DB_MEMBERS_ALLOWED:
                            raise ValueError(f"DB 패키지에 허용되지 않은 파일이 있습니다: {name}")
                        if kind == "covers":
                            if not name.startswith("covers/"):
                                raise ValueError(f"표지 패키지 경로가 올바르지 않습니다: {name}")
                            if name != "covers/.gitkeep" and not name.lower().endswith(
                                ".webp"
                            ):
                                raise ValueError(f"표지 패키지 파일 형식이 올바르지 않습니다: {name}")
                            relative_name = name[len("covers/") :]
                            if relative_name in cover_names:
                                raise ValueError(f"중복된 압축 항목입니다: {name}")
                            if name != "covers/.gitkeep":
                                cover_names.add(relative_name)
                        else:
                            if name in regular_names:
                                raise ValueError(f"중복된 압축 항목입니다: {name}")
                            regular_names.add(name)
                        total_size += int(member.size or 0)
                        file_count += 1
                    elif kind == "database" and name not in {"db"}:
                        raise ValueError(f"DB 패키지 디렉터리가 올바르지 않습니다: {name}")
                    elif kind == "covers" and not (
                        name == "covers" or name.startswith("covers/")
                    ):
                        raise ValueError(f"표지 패키지 디렉터리가 올바르지 않습니다: {name}")
                    member_count += 1
                    if member_count > MAX_ARCHIVE_MEMBERS:
                        raise ValueError("압축 항목 수가 안전 제한을 초과했습니다.")
                    if total_size > MAX_ARCHIVE_UNCOMPRESSED:
                        raise ValueError("압축 해제 크기가 안전 제한을 초과했습니다.")
        except (tarfile.TarError, EOFError, OSError) as error:
            raise ValueError(f"tar.gz 패키지를 읽을 수 없습니다: {error}") from error
        if kind == "database":
            missing = sorted(DB_MEMBERS_REQUIRED - regular_names)
            if missing:
                raise ValueError("DB 패키지 필수 파일이 없습니다: " + ", ".join(missing))
        return {
            "path": str(archive_path),
            "filename": archive_path.name,
            "compressed_size": archive_path.stat().st_size,
            "uncompressed_size": total_size,
            "member_count": member_count,
            "file_count": file_count,
            "regular_names": regular_names,
            "cover_names": cover_names,
            "fingerprint": self._archive_fingerprint(archive_path),
        }

    def _extract_archive(self, archive_path, target_root, kind, metadata=None):
        metadata = metadata or self._inspect_archive(archive_path, kind)
        if metadata.get("fingerprint") != self._archive_fingerprint(archive_path):
            raise RuntimeError("검사 후 압축파일이 변경되었습니다. 다시 검사해 주세요.")
        free = shutil.disk_usage(str(target_root)).free
        if free < metadata["uncompressed_size"]:
            raise OSError("압축을 해제할 디스크 여유 공간이 부족합니다.")
        total_files = int(metadata["file_count"])
        completed = 0
        with tarfile.open(str(archive_path), "r|gz") as archive:
            for member in archive:
                self._check_stop()
                name = normalize_member_name(member.name)
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValueError(f"링크·장치 압축 항목은 허용하지 않습니다: {name}")
                destination = (Path(target_root) / PurePosixPath(name)).resolve()
                try:
                    destination.relative_to(Path(target_root).resolve())
                except ValueError:
                    raise ValueError(f"안전하지 않은 압축 경로입니다: {name}")
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"압축 항목을 읽을 수 없습니다: {name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, COPY_CHUNK_SIZE)
                completed += 1
                if completed == total_files or completed % 500 == 0:
                    self._progress(
                        "extract",
                        completed,
                        total_files,
                        f"{kind} 패키지 {completed}/{total_files}개 파일을 해제했습니다.",
                    )

    def _inspect_database(self, path, db_type):
        connection = self._connect(path, readonly=True)
        try:
            quick_check = [
                str(row[0])
                for row in self._iter_rows(
                    connection.execute("PRAGMA quick_check")
                )
            ]
            if quick_check != ["ok"]:
                raise RuntimeError(
                    f"{db_type} DB quick_check에 실패했습니다: "
                    + ", ".join(quick_check[:10])
                )
            tables = self._tables(connection)
            item_table = "audiobooks" if db_type == "audiobook" else "books"
            if not {"libraries", item_table}.issubset(tables):
                raise RuntimeError(
                    f"{db_type} DB에 libraries 또는 {item_table} 테이블이 없습니다."
                )
            libraries = [
                {
                    "id": int(row["id"]),
                    "name": str(row["name"] or ""),
                    "physical_path": str(row["physical_path"] or ""),
                }
                for row in self._iter_rows(
                    connection.execute(
                        "SELECT id, name, physical_path FROM libraries ORDER BY id"
                    )
                )
            ]
            items_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM `{item_table}`"
                ).fetchone()[0]
            )
            cover_references = set()
            if db_type != "audiobook":
                cover_references = {
                    str(row["cover_image"] or "").replace("\\", "/").lstrip("/")
                    for row in self._iter_rows(
                        connection.execute(
                            """
                            SELECT DISTINCT cover_image
                            FROM books
                            WHERE COALESCE(cover_image, '') != ''
                            """
                        )
                    )
                    if row["cover_image"]
                }
            users_count = (
                int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                if "users" in tables
                else 0
            )
            progress_count = (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM "
                        + (
                            "audiobook_progress"
                            if db_type == "audiobook"
                            else "user_progress"
                        )
                    ).fetchone()[0]
                )
                if (
                    "audiobook_progress"
                    if db_type == "audiobook"
                    else "user_progress"
                ) in tables
                else 0
            )
            return {
                "db_type": db_type,
                "user_version": int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                ),
                "libraries": libraries,
                "libraries_count": len(libraries),
                "books_count": items_count if db_type != "audiobook" else 0,
                "audiobooks_count": items_count if db_type == "audiobook" else 0,
                "users_count": users_count,
                "progress_count": progress_count,
                "cover_references": cover_references,
            }
        finally:
            connection.close()

    @staticmethod
    def _mapping_preview(database_path, mappings, db_type="general"):
        if db_type == "audiobook":
            return BookOasisPackageImportEngine._audiobook_mapping_preview(
                database_path,
                mappings,
            )
        connection = BookOasisPackageImportEngine._connect(
            database_path, readonly=False
        )
        try:
            connection.execute(
                "CREATE TEMP TABLE mapped_paths "
                "(book_id INTEGER PRIMARY KEY, file_path TEXT)"
            )
            changed_books = 0
            changed_covers = 0
            cursor = connection.execute(
                "SELECT id, library_id, file_path, cover_image FROM books"
            )
            while True:
                rows = cursor.fetchmany(DB_BATCH_SIZE)
                if not rows:
                    break
                mapped_rows = []
                for row in rows:
                    old_path = str(row["file_path"] or "").strip()
                    new_path = apply_media_mappings(old_path, mappings)
                    mapped_rows.append((int(row["id"]), new_path))
                    if new_path != old_path:
                        changed_books += 1
                        old_cover = standard_cover_relative(
                            row["library_id"], old_path
                        )
                        if (
                            str(row["cover_image"] or "")
                            .replace("\\", "/")
                            .lstrip("/")
                            == old_cover
                        ):
                            changed_covers += 1
                connection.executemany(
                    "INSERT INTO mapped_paths(book_id, file_path) VALUES (?, ?)",
                    mapped_rows,
                )
            collision_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT file_path
                        FROM mapped_paths
                        WHERE COALESCE(file_path, '') != ''
                        GROUP BY file_path
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
            )
            collisions = []
            collision_cursor = connection.execute(
                """
                SELECT file_path
                FROM mapped_paths
                WHERE COALESCE(file_path, '') != ''
                GROUP BY file_path
                HAVING COUNT(*) > 1
                ORDER BY file_path
                LIMIT 100
                """
            )
            for collision in BookOasisPackageImportEngine._iter_rows(
                collision_cursor
            ):
                book_ids = [
                    int(row["book_id"])
                    for row in BookOasisPackageImportEngine._iter_rows(
                        connection.execute(
                            "SELECT book_id FROM mapped_paths "
                            "WHERE file_path=? ORDER BY book_id",
                            (collision["file_path"],),
                        )
                    )
                ]
                collisions.append(
                    {"path": collision["file_path"], "book_ids": book_ids}
                )
            changed_libraries = 0
            for row in BookOasisPackageImportEngine._iter_rows(
                connection.execute("SELECT physical_path FROM libraries")
            ):
                old_value = str(row["physical_path"] or "")
                if replace_library_mappings(old_value, mappings) != old_value:
                    changed_libraries += 1
            return {
                "changed_libraries_count": changed_libraries,
                "changed_books_count": changed_books,
                "changed_covers_count": changed_covers,
                "path_collisions": collisions,
                "path_collision_count": collision_count,
            }
        finally:
            connection.close()

    @staticmethod
    def _audiobook_mapping_preview(database_path, mappings):
        connection = BookOasisPackageImportEngine._connect(
            database_path,
            readonly=True,
        )
        try:
            changed_items = 0
            mapped_paths = {}
            collisions = []
            for row in BookOasisPackageImportEngine._iter_rows(
                connection.execute("SELECT id, folder_path FROM audiobooks ORDER BY id")
            ):
                old_path = str(row["folder_path"] or "")
                new_path = apply_media_mappings(old_path, mappings)
                if new_path != old_path:
                    changed_items += 1
                if new_path in mapped_paths and len(collisions) < 100:
                    collisions.append(
                        {
                            "path": new_path,
                            "book_ids": [mapped_paths[new_path], int(row["id"])],
                        }
                    )
                else:
                    mapped_paths[new_path] = int(row["id"])
            changed_libraries = 0
            for row in BookOasisPackageImportEngine._iter_rows(
                connection.execute("SELECT physical_path FROM libraries")
            ):
                old_value = str(row["physical_path"] or "")
                if replace_library_mappings(old_value, mappings) != old_value:
                    changed_libraries += 1
            return {
                "changed_libraries_count": changed_libraries,
                "changed_books_count": changed_items,
                "changed_covers_count": 0,
                "path_collisions": collisions,
                "path_collision_count": len(collisions),
            }
        finally:
            connection.close()

    def _inspect_staged_databases(self, stage, database_package, mappings):
        databases = []
        references = set()
        changed_libraries = 0
        changed_books = 0
        changed_covers = 0
        collision_count = 0
        collisions = []
        database_types = ["general", "adult"]
        if "db/media_audiobook.db" in database_package.get("regular_names", set()):
            database_types.append("audiobook")
        for db_type in database_types:
            filename = f"media_{db_type}.db"
            db_path = stage / "db" / filename
            data = self._inspect_database(db_path, db_type)
            mapping = self._mapping_preview(db_path, mappings, db_type)
            data.update(mapping)
            databases.append(data)
            references.update(data.pop("cover_references"))
            changed_libraries += mapping["changed_libraries_count"]
            changed_books += mapping["changed_books_count"]
            changed_covers += mapping["changed_covers_count"]
            collision_count += mapping["path_collision_count"]
            if len(collisions) < 100:
                collisions.extend(
                    {"db_type": db_type, **item}
                    for item in mapping["path_collisions"][
                        : 100 - len(collisions)
                    ]
                )
        source_paths = []
        for database in databases:
            for library in database["libraries"]:
                for path in str(library["physical_path"]).splitlines():
                    normalized = normalize_media_path(path)
                    if normalized and normalized not in source_paths:
                        source_paths.append(normalized)
        result = {
            "source_type": "bookoasis",
            "mode": "package",
            "dry_run": True,
            "inspection_scope": "database",
            "database_package": {
                key: value
                for key, value in database_package.items()
                if key not in {"regular_names", "cover_names", "fingerprint"}
            },
            "databases": databases,
            "database_count": len(databases),
            "libraries_count": sum(
                item["libraries_count"] for item in databases
            ),
            "books_count": sum(item["books_count"] for item in databases),
            "audiobooks_count": sum(
                item.get("audiobooks_count", 0) for item in databases
            ),
            "users_count": sum(item["users_count"] for item in databases),
            "progress_count": sum(item["progress_count"] for item in databases),
            "cover_references_count": len(references),
            "changed_libraries_count": changed_libraries,
            "changed_books_count": changed_books,
            "changed_covers_count": changed_covers,
            "path_collision_count": collision_count,
            "path_collisions": collisions,
            "source_paths": source_paths,
            "suggested_mapping_source": suggested_gdrive_parent(source_paths),
        }
        return result, references

    def _inspect_database_package(self, db_archive, path_mappings=""):
        db_path = self._work_file(db_archive, "DB 패키지")
        database_package = self._inspect_archive(db_path, "database")
        mappings = parse_mapping_lines(path_mappings)
        stage = Path(
            tempfile.mkdtemp(
                prefix=".bookoasis_package_inspect_",
                dir=str(self.work_root),
            )
        )
        try:
            self._extract_archive(
                db_path,
                stage,
                "database",
                database_package,
            )
            return self._inspect_staged_databases(
                stage,
                database_package,
                mappings,
            )
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)

    def inspect_database_package(self, db_archive, path_mappings=""):
        if self.mariadb_engine is not None:
            return self.mariadb_engine.preview(
                self._work_file(db_archive, "DB 패키지"),
                path_mappings,
            )
        result, _ = self._inspect_database_package(db_archive, path_mappings)
        return result

    @staticmethod
    def _apply_cover_inspection(result, references, cover_package):
        cover_names = cover_package["cover_names"]
        missing = []
        missing_count = 0
        for name in references:
            if name in cover_names:
                continue
            missing_count += 1
            if len(missing) < 100:
                missing.append(name)
        result.update(
            {
                "inspection_scope": "full",
                "cover_package": {
                    key: value
                    for key, value in cover_package.items()
                    if key not in {"regular_names", "cover_names", "fingerprint"}
                },
                "cover_files_count": len(cover_names),
                "missing_cover_count": missing_count,
                "missing_covers": sorted(missing),
                "unreferenced_cover_count": sum(
                    1 for name in cover_names if name not in references
                ),
            }
        )
        return result

    def inspect(self, db_archive, cover_archive, path_mappings=""):
        if self.mariadb_engine is not None:
            result = self.mariadb_engine.preview(
                self._work_file(db_archive, "DB 패키지"),
                path_mappings,
            )
            cover_package = self._inspect_archive(
                self._work_file(cover_archive, "표지 패키지"),
                "covers",
            )
            result.update(
                {
                    "inspection_scope": "full",
                    "cover_package": {
                        key: value
                        for key, value in cover_package.items()
                        if key not in {"regular_names", "cover_names", "fingerprint"}
                    },
                    "cover_files_count": len(cover_package["cover_names"]),
                    "cover_validation_note": (
                        "표지 참조의 최종 대조는 MariaDB 복원 후 확인합니다."
                    ),
                }
            )
            return result
        result, references = self._inspect_database_package(
            db_archive, path_mappings
        )
        cover_package = self._inspect_archive(
            self._work_file(cover_archive, "표지 패키지"), "covers"
        )
        return self._apply_cover_inspection(
            result,
            references,
            cover_package,
        )

    def _transform_database(
        self,
        database_path,
        cover_root,
        mappings,
        db_type="general",
    ):
        if db_type == "audiobook":
            return self._transform_audiobook_database(
                database_path,
                mappings,
            )
        connection = self._connect(database_path, readonly=False)
        moved_files = []
        changed_books = 0
        changed_libraries = 0
        changed_covers = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TEMP TABLE transformed_books (
                    book_id INTEGER PRIMARY KEY,
                    library_id INTEGER,
                    old_path TEXT,
                    new_path TEXT,
                    cover_image TEXT
                )
                """
            )
            cursor = connection.execute(
                "SELECT id, library_id, file_path, cover_image FROM books"
            )
            while True:
                rows = cursor.fetchmany(DB_BATCH_SIZE)
                if not rows:
                    break
                self._check_stop()
                connection.executemany(
                    """
                    INSERT INTO transformed_books(
                        book_id, library_id, old_path, new_path, cover_image
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(row["id"]),
                            row["library_id"],
                            str(row["file_path"] or "").strip(),
                            apply_media_mappings(
                                str(row["file_path"] or "").strip(),
                                mappings,
                            ),
                            str(row["cover_image"] or ""),
                        )
                        for row in rows
                    ],
                )
            collision_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT new_path
                        FROM transformed_books
                        WHERE COALESCE(new_path, '') != ''
                        GROUP BY new_path
                        HAVING COUNT(*) > 1
                    )
                    """
                ).fetchone()[0]
            )
            if collision_count:
                raise ValueError(
                    f"변경 후 도서 경로 충돌이 {collision_count}건 있습니다."
                )
            for row in self._iter_rows(
                connection.execute("SELECT id, physical_path FROM libraries")
            ):
                old_value = str(row["physical_path"] or "")
                new_value = replace_library_mappings(old_value, mappings)
                if new_value != old_value:
                    connection.execute(
                        "UPDATE libraries SET physical_path=? WHERE id=?",
                        (new_value, int(row["id"])),
                    )
                    changed_libraries += 1
            transformed_cursor = connection.execute(
                """
                SELECT book_id, library_id, old_path, new_path, cover_image
                FROM transformed_books
                WHERE new_path != old_path
                ORDER BY book_id
                """
            )
            while True:
                rows = transformed_cursor.fetchmany(DB_BATCH_SIZE)
                if not rows:
                    break
                self._check_stop()
                for row in rows:
                    old_path = str(row["old_path"] or "")
                    new_path = str(row["new_path"] or "")
                    cover_value = (
                        str(row["cover_image"] or "")
                        .replace("\\", "/")
                        .lstrip("/")
                    )
                    old_cover = standard_cover_relative(
                        row["library_id"], old_path
                    )
                    new_cover = standard_cover_relative(
                        row["library_id"], new_path
                    )
                    if cover_value == old_cover:
                        source = cover_root / old_cover
                        destination = cover_root / new_cover
                        if source.is_file() and source != destination:
                            if destination.exists():
                                raise FileExistsError(
                                    "표지 목적지 파일이 이미 존재합니다: "
                                    f"{destination}"
                                )
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(str(source), str(destination))
                            moved_files.append((source, destination))
                            changed_covers += 1
                        connection.execute(
                            "UPDATE books SET file_path=?, cover_image=? WHERE id=?",
                            (new_path, new_cover, int(row["book_id"])),
                        )
                    else:
                        connection.execute(
                            "UPDATE books SET file_path=? WHERE id=?",
                            (new_path, int(row["book_id"])),
                        )
                    changed_books += 1
            connection.commit()
        except Exception:
            connection.rollback()
            for source, destination in reversed(moved_files):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(destination), str(source))
            raise
        finally:
            connection.close()
        return {
            "changed_libraries_count": changed_libraries,
            "changed_books_count": changed_books,
            "changed_covers_count": changed_covers,
        }

    def _transform_audiobook_database(self, database_path, mappings):
        connection = self._connect(database_path, readonly=False)
        changed_items = 0
        changed_libraries = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            mapped_paths = set()
            updates = []
            for row in self._iter_rows(
                connection.execute("SELECT id, folder_path, poster FROM audiobooks ORDER BY id")
            ):
                old_path = str(row["folder_path"] or "")
                new_path = apply_media_mappings(old_path, mappings)
                if new_path in mapped_paths:
                    raise ValueError(
                        f"변경 후 오디오북 경로 충돌이 있습니다: {new_path}"
                    )
                mapped_paths.add(new_path)
                old_poster = str(row["poster"] or "")
                new_poster = (
                    old_poster
                    if old_poster.startswith(("http://", "https://"))
                    else apply_media_mappings(old_poster, mappings)
                )
                if new_path != old_path or new_poster != old_poster:
                    updates.append((new_path, new_poster, int(row["id"])))
            if updates:
                connection.executemany(
                    "UPDATE audiobooks SET folder_path=?, poster=? WHERE id=?",
                    updates,
                )
                changed_items = len(updates)
            for row in self._iter_rows(
                connection.execute("SELECT id, physical_path FROM libraries")
            ):
                old_value = str(row["physical_path"] or "")
                new_value = replace_library_mappings(old_value, mappings)
                if new_value != old_value:
                    connection.execute(
                        "UPDATE libraries SET physical_path=? WHERE id=?",
                        (new_value, int(row["id"])),
                    )
                    changed_libraries += 1
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "changed_libraries_count": changed_libraries,
            "changed_books_count": changed_items,
            "changed_covers_count": 0,
        }

    def _backup_database(self, source_path, db_type):
        backup_dir = self.work_root / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{source_path.stem}_{db_type}_before_package_{timestamp}.db"
        source = self._connect(source_path, readonly=True)
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return target

    @staticmethod
    def _checkpoint_database(path):
        connection = BookOasisPackageImportEngine._connect(path, readonly=False)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def _activate(self, stage, backup=True):
        active_databases = {
            db_type: target
            for db_type, target in self.target_databases.items()
            if (stage / "db" / f"media_{db_type}.db").is_file()
        }
        for db_type, target in active_databases.items():
            if target is None:
                raise ValueError(
                    f"BookOasis {db_type} DB 대상 경로가 설정되지 않았습니다."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
        if self.target_cover_root is None:
            raise ValueError("BookOasis 표지 대상 경로가 설정되지 않았습니다.")
        self.target_cover_root.parent.mkdir(parents=True, exist_ok=True)
        if self.target_cover_root.exists() and not self.target_cover_root.is_dir():
            raise NotADirectoryError(
                "BookOasis 표지 대상 경로가 디렉터리가 아닙니다."
            )

        token = uuid.uuid4().hex[:12]
        target_existed = {
            db_type: target.is_file()
            for db_type, target in active_databases.items()
        }
        cover_existed = self.target_cover_root.is_dir()
        incoming = {}
        rollback = {}
        sidecar_rollback = []
        database_backups = {}
        cover_backup = None
        if cover_existed:
            cover_backup = (
                self.target_cover_root.parent
                / (
                    f"{self.target_cover_root.name}_before_package_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{token}"
                )
            )
        incoming_cover = (
            self.target_cover_root.parent
            / f".{self.target_cover_root.name}_incoming_{token}"
        )
        installed_databases = []
        moved_databases = []
        cover_installed = False
        cover_moved = False
        try:
            for db_type, target in active_databases.items():
                self._check_stop()
                if target_existed[db_type]:
                    self._checkpoint_database(target)
                if backup and target_existed[db_type]:
                    database_backups[db_type] = str(
                        self._backup_database(target, db_type)
                    )
                incoming_path = target.parent / f".{target.name}.incoming_{token}"
                rollback_path = target.parent / f".{target.name}.before_{token}"
                shutil.copy2(
                    str(stage / "db" / f"media_{db_type}.db"),
                    str(incoming_path),
                )
                incoming[db_type] = incoming_path
                rollback[db_type] = rollback_path
            cover_stage_size = sum(
                path.stat().st_size
                for path in (stage / "covers").rglob("*")
                if path.is_file()
            )
            if (
                shutil.disk_usage(str(self.target_cover_root.parent)).free
                < cover_stage_size
            ):
                raise OSError("표지 패키지를 적용할 디스크 여유 공간이 부족합니다.")
            shutil.copytree(str(stage / "covers"), str(incoming_cover))

            for db_type, target in active_databases.items():
                if target_existed[db_type]:
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(target) + suffix)
                        if sidecar.exists():
                            saved = Path(str(rollback[db_type]) + suffix)
                            os.replace(str(sidecar), str(saved))
                            sidecar_rollback.append((sidecar, saved))
                    os.replace(str(target), str(rollback[db_type]))
                    moved_databases.append(db_type)
                os.replace(str(incoming[db_type]), str(target))
                installed_databases.append(db_type)

            if cover_existed:
                os.replace(str(self.target_cover_root), str(cover_backup))
                cover_moved = True
            os.replace(str(incoming_cover), str(self.target_cover_root))
            cover_installed = True
        except Exception:
            if cover_installed and self.target_cover_root.exists():
                shutil.rmtree(str(self.target_cover_root), ignore_errors=True)
            if cover_moved and cover_backup and cover_backup.exists():
                os.replace(str(cover_backup), str(self.target_cover_root))
            for db_type in reversed(installed_databases):
                target = active_databases[db_type]
                if target.exists():
                    target.unlink()
            for db_type in reversed(moved_databases):
                target = active_databases[db_type]
                if rollback[db_type].exists():
                    os.replace(str(rollback[db_type]), str(target))
            for sidecar, saved in reversed(sidecar_rollback):
                if saved.exists():
                    os.replace(str(saved), str(sidecar))
            raise
        finally:
            for path in incoming.values():
                if path.exists():
                    path.unlink()
            if incoming_cover.exists():
                shutil.rmtree(str(incoming_cover), ignore_errors=True)

        for path in rollback.values():
            if path.exists():
                path.unlink()
        for _, saved in sidecar_rollback:
            if saved.exists():
                saved.unlink()
        return {
            "database_backups": database_backups,
            "cover_backup_path": str(cover_backup) if cover_backup else "",
            "installed_without_backup": [
                db_type
                for db_type, existed in target_existed.items()
                if not existed
            ],
            "cover_installed_without_backup": not cover_existed,
        }

    def migrate(
        self,
        db_archive,
        cover_archive,
        path_mappings="",
        backup=True,
        dry_run=True,
        confirm_stopped=False,
    ):
        if self.mariadb_engine is not None:
            db_path = self._work_file(db_archive, "DB 패키지")
            cover_path = self._work_file(cover_archive, "표지 패키지")
            return self.mariadb_engine.migrate(
                db_path,
                cover_path,
                path_mappings,
                backup,
                dry_run,
                confirm_stopped,
                inspect_cover=lambda path: self._inspect_archive(path, "covers"),
                extract_cover=lambda path, target, metadata: self._extract_archive(
                    path,
                    target,
                    "covers",
                    metadata,
                ),
            )
        if not dry_run and not confirm_stopped:
            raise ValueError("BookOasis 중지 확인 옵션을 켜 주세요.")
        health = (self.health_check() or {}) if not dry_run else {}
        if not dry_run and health.get("success"):
            raise RuntimeError(
                "BookOasis 상태 API가 정상 응답하고 있습니다. "
                "BookOasis를 완전히 중지한 뒤 다시 실행해 주세요."
            )

        db_path = self._work_file(db_archive, "DB 패키지")
        cover_path = self._work_file(cover_archive, "표지 패키지")
        database_package = self._inspect_archive(db_path, "database")
        cover_package = self._inspect_archive(cover_path, "covers")
        mappings = parse_mapping_lines(path_mappings)
        stage = Path(
            tempfile.mkdtemp(prefix=".bookoasis_package_import_", dir=str(self.work_root))
        )
        try:
            self._extract_archive(db_path, stage, "database", database_package)
            self._extract_archive(cover_path, stage, "covers", cover_package)
            preview, references = self._inspect_staged_databases(
                stage,
                database_package,
                mappings,
            )
            self._apply_cover_inspection(
                preview,
                references,
                cover_package,
            )
            if preview["path_collision_count"]:
                raise ValueError(
                    "변경 후 도서 경로 충돌이 "
                    f"{preview['path_collision_count']}건 있습니다."
                )
            transformed = {
                "changed_libraries_count": 0,
                "changed_books_count": 0,
                "changed_covers_count": 0,
            }
            imported_db_types = ["general", "adult"]
            if (stage / "db" / "media_audiobook.db").is_file():
                imported_db_types.append("audiobook")
            for db_type in imported_db_types:
                self._check_stop()
                result = self._transform_database(
                    stage / "db" / f"media_{db_type}.db",
                    stage / "covers",
                    mappings,
                    db_type,
                )
                for key in transformed:
                    transformed[key] += result[key]
                self._inspect_database(
                    stage / "db" / f"media_{db_type}.db", db_type
                )
            preview.update(transformed)
            if dry_run:
                preview["dry_run"] = True
                return preview
            health = self.health_check() or {}
            if health.get("success"):
                raise RuntimeError(
                    "패키지 검증 중 BookOasis가 다시 실행되었습니다. "
                    "BookOasis를 완전히 중지한 뒤 다시 실행해 주세요."
                )
            self._progress("activate", 0, 1, "검증된 DB와 표지 적용을 준비합니다.")
            activation = self._activate(stage, backup=backup)
            self._progress("activate", 1, 1, "BookOasis 패키지 적용을 완료했습니다.")
            preview.update(activation)
            preview["dry_run"] = False
            preview["imported_databases"] = imported_db_types
            return preview
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)
