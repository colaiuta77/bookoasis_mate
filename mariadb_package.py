# MariaDB 기반 BookOasis 전체 DB를 논리 백업 패키지로 내보내고 복원합니다.
import hashlib
import gzip
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

try:
    from .kavita_migration import parse_mapping_lines
    from .mariadb_backup import (
        MariaDBClientTools,
        clear_mariadb_database,
        sha256_file,
    )
    from .migration_database import MigrationDatabaseContext
except ImportError:
    from kavita_migration import parse_mapping_lines
    from mariadb_backup import (
        MariaDBClientTools,
        clear_mariadb_database,
        sha256_file,
    )
    from migration_database import MigrationDatabaseContext


PACKAGE_FORMAT = "bookoasis-mate-mariadb"
PACKAGE_VERSION = "1.0"
DB_TYPES = ("general", "adult", "audiobook", "video")
MAX_MANIFEST_SIZE = 2 * 1024 * 1024
COPY_CHUNK_SIZE = 1024 * 1024
WRITE_BATCH_SIZE = 500


@contextmanager
def _open_gzip_tar_writer(path, compresslevel=1):
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


def _normalize_path(value):
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def _apply_mapping(value, mappings):
    original = str(value or "").strip()
    normalized = _normalize_path(original)
    for source, target in sorted(
        [(_normalize_path(source), _normalize_path(target)) for source, target in mappings],
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if normalized == source:
            return target
        if normalized.startswith(source + "/"):
            return target + normalized[len(source) :]
    return original


def _replace_library_paths(value, mappings):
    return "\n".join(
        _apply_mapping(line, mappings) if line.strip() else line
        for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    )


def _safe_member_name(value):
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if not text or text.startswith("/") or any(part in {"", ".."} for part in path.parts):
        raise ValueError(f"안전하지 않은 압축 경로입니다: {text}")
    if path.parts and ":" in path.parts[0]:
        raise ValueError(f"안전하지 않은 압축 경로입니다: {text}")
    return path.as_posix().rstrip("/")


class MariaDBPackageEngine:
    def __init__(
        self,
        work_root,
        cover_root,
        settings,
        health_check=None,
        should_stop=None,
        on_progress=None,
    ):
        self.work_root = Path(work_root).expanduser().resolve()
        self.cover_root = Path(cover_root).expanduser().resolve() if cover_root else None
        self.settings = dict(settings or {})
        self.health_check = health_check or (lambda: {"success": False})
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress
        self.database = MigrationDatabaseContext(
            self.settings,
            work_dir=self.work_root,
            should_stop=self.should_stop,
            on_progress=self.on_progress,
        )
        if self.database.engine != "mariadb":
            raise ValueError("MariaDB 패키지 엔진은 MariaDB 모드에서만 사용할 수 있습니다.")
        self.tools = MariaDBClientTools(
            self.settings,
            work_dir=self.work_root,
            should_stop=self.should_stop,
            on_progress=self.on_progress,
        )

    def _check_stop(self):
        if self.should_stop():
            raise RuntimeError("사용자가 BookOasis 패키지 작업을 중지했습니다.")

    def _progress(self, stage, current, total, message):
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

    @staticmethod
    def _iter_rows(cursor, batch_size=1000):
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield from rows

    def _database_summary(self, db_type):
        connection = self.database.connect(db_type, readonly=True)
        try:
            tables = connection.tables()
            libraries = []
            if "libraries" in tables:
                for row in self._iter_rows(
                    connection.execute(
                        "SELECT id, name, physical_path FROM libraries ORDER BY id"
                    )
                ):
                    libraries.append(
                        {
                            "id": int(row["id"]),
                            "name": str(row["name"] or ""),
                            "physical_path": str(row["physical_path"] or ""),
                        }
                    )
            item_table = {
                "audiobook": "audiobooks",
                "video": "videos",
            }.get(db_type, "books")
            progress_table = {
                "audiobook": "audiobook_progress",
                "video": "video_progress",
            }.get(db_type, "user_progress")
            items_count = (
                int(connection.execute(f"SELECT COUNT(*) AS cnt FROM `{item_table}`").fetchone()["cnt"] or 0)
                if item_table in tables
                else 0
            )
            users_count = (
                int(connection.execute("SELECT COUNT(*) AS cnt FROM users").fetchone()["cnt"] or 0)
                if "users" in tables
                else 0
            )
            progress_count = (
                int(connection.execute(f"SELECT COUNT(*) AS cnt FROM `{progress_table}`").fetchone()["cnt"] or 0)
                if progress_table in tables
                else 0
            )
            return {
                "db_type": db_type,
                "database": self.database.target(db_type).database,
                "libraries": libraries,
                "libraries_count": len(libraries),
                "books_count": items_count if db_type in {"general", "adult"} else 0,
                "audiobooks_count": items_count if db_type == "audiobook" else 0,
                "videos_count": items_count if db_type == "video" else 0,
                "users_count": users_count,
                "progress_count": progress_count,
            }
        finally:
            connection.close()

    @staticmethod
    def _source_paths(databases):
        paths = []
        for database in databases:
            for library in database.get("libraries", []):
                for value in str(library.get("physical_path") or "").splitlines():
                    normalized = _normalize_path(value)
                    if normalized and normalized not in paths:
                        paths.append(normalized)
        return paths

    def export_database_archive(self, target_path):
        stage = Path(tempfile.mkdtemp(prefix=".mariadb_export_", dir=str(self.work_root)))
        try:
            database_root = stage / "db"
            database_root.mkdir(parents=True)
            summaries = []
            files = {}
            for index, db_type in enumerate(DB_TYPES, start=1):
                self._check_stop()
                target = self.database.target(db_type)
                destination = database_root / f"media_{db_type}.sql"
                self._progress(
                    "export_databases",
                    index - 1,
                    len(DB_TYPES),
                    f"{target.database} 논리 백업을 생성하고 있습니다.",
                )
                self.tools.dump(target.database, destination)
                summary = self._database_summary(db_type)
                summaries.append(summary)
                files[f"db/media_{db_type}.sql"] = {
                    "database": target.database,
                    "size": destination.stat().st_size,
                    "sha256": sha256_file(destination),
                }
                self._progress(
                    "export_databases",
                    index,
                    len(DB_TYPES),
                    f"{target.database} 논리 백업을 완료했습니다.",
                )
            manifest = {
                "format": PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "engine": "mariadb",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "database_prefix": str(self.settings.get("mariadb_database_prefix") or "media_"),
                "files": files,
                "databases": summaries,
                "source_paths": self._source_paths(summaries),
            }
            manifest_path = stage / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with _open_gzip_tar_writer(target_path, compresslevel=1) as archive:
                archive.add(str(manifest_path), arcname="manifest.json", recursive=False)
                for db_type in DB_TYPES:
                    archive.add(
                        str(database_root / f"media_{db_type}.sql"),
                        arcname=f"db/media_{db_type}.sql",
                        recursive=False,
                    )
            return manifest
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)

    def inspect_database_archive(self, archive_path):
        archive_path = Path(archive_path)
        allowed = {"manifest.json"} | {
            f"db/media_{db_type}.sql" for db_type in DB_TYPES
        }
        found = set()
        hashes = {}
        sizes = {}
        manifest = None
        try:
            with tarfile.open(str(archive_path), "r|gz") as archive:
                for member in archive:
                    self._check_stop()
                    name = _safe_member_name(member.name)
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise ValueError(f"링크·장치 압축 항목은 허용하지 않습니다: {name}")
                    if member.isdir():
                        if name != "db":
                            raise ValueError(f"DB 패키지 디렉터리가 올바르지 않습니다: {name}")
                        continue
                    if not member.isfile() or name not in allowed:
                        raise ValueError(f"DB 패키지에 허용되지 않은 파일이 있습니다: {name}")
                    if name in found:
                        raise ValueError(f"중복된 압축 항목입니다: {name}")
                    found.add(name)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"압축 항목을 읽을 수 없습니다: {name}")
                    if name == "manifest.json":
                        payload = source.read(MAX_MANIFEST_SIZE + 1)
                        if len(payload) > MAX_MANIFEST_SIZE:
                            raise ValueError("DB 패키지 manifest.json이 너무 큽니다.")
                        manifest = json.loads(payload.decode("utf-8"))
                    else:
                        digest = hashlib.sha256()
                        total = 0
                        while True:
                            chunk = source.read(COPY_CHUNK_SIZE)
                            if not chunk:
                                break
                            digest.update(chunk)
                            total += len(chunk)
                        hashes[name] = digest.hexdigest()
                        sizes[name] = total
        except (tarfile.TarError, EOFError, OSError, json.JSONDecodeError) as error:
            raise ValueError(f"MariaDB DB 패키지를 읽을 수 없습니다: {error}") from error
        missing = sorted(
            {"manifest.json", "db/media_general.sql", "db/media_adult.sql"}
            - found
        )
        if missing:
            raise ValueError("DB 패키지 필수 파일이 없습니다: " + ", ".join(missing))
        if not isinstance(manifest, dict):
            raise ValueError("DB 패키지 manifest.json이 올바르지 않습니다.")
        if manifest.get("format") != PACKAGE_FORMAT or manifest.get("engine") != "mariadb":
            raise ValueError("Mate MariaDB 통DB 패키지가 아닙니다.")
        if str(manifest.get("version") or "") != PACKAGE_VERSION:
            raise ValueError("지원하지 않는 MariaDB 통DB 패키지 버전입니다.")
        manifest_files = manifest.get("files") or {}
        database_files = sorted(found - {"manifest.json"})
        for name in database_files:
            metadata = manifest_files.get(name) or {}
            if hashes.get(name) != metadata.get("sha256"):
                raise ValueError(f"DB 패키지 SHA-256 검증에 실패했습니다: {name}")
            if int(metadata.get("size") or -1) != sizes.get(name):
                raise ValueError(f"DB 패키지 크기 검증에 실패했습니다: {name}")
        manifest["db_types"] = [
            db_type
            for db_type in DB_TYPES
            if f"db/media_{db_type}.sql" in found
        ]
        return manifest

    def preview(self, archive_path, path_mappings=""):
        manifest = self.inspect_database_archive(archive_path)
        mappings = parse_mapping_lines(path_mappings)
        databases = list(manifest.get("databases") or [])
        source_paths = list(manifest.get("source_paths") or self._source_paths(databases))
        changed_libraries = sum(
            1
            for database in databases
            for library in database.get("libraries", [])
            if _replace_library_paths(library.get("physical_path"), mappings)
            != str(library.get("physical_path") or "")
        )
        return {
            "source_type": "bookoasis",
            "mode": "package",
            "package_engine": "mariadb",
            "package_version": manifest.get("version"),
            "package_db_types": list(manifest.get("db_types") or []),
            "dry_run": True,
            "inspection_scope": "database",
            "database_package": {
                "path": str(archive_path),
                "filename": Path(archive_path).name,
                "compressed_size": Path(archive_path).stat().st_size,
            },
            "databases": databases,
            "database_count": len(databases),
            "libraries_count": sum(int(item.get("libraries_count") or 0) for item in databases),
            "books_count": sum(int(item.get("books_count") or 0) for item in databases),
            "audiobooks_count": sum(int(item.get("audiobooks_count") or 0) for item in databases),
            "videos_count": sum(int(item.get("videos_count") or 0) for item in databases),
            "users_count": sum(int(item.get("users_count") or 0) for item in databases),
            "progress_count": sum(int(item.get("progress_count") or 0) for item in databases),
            "cover_references_count": 0,
            "changed_libraries_count": changed_libraries,
            "changed_books_count": 0,
            "changed_covers_count": 0,
            "path_collision_count": 0,
            "path_collisions": [],
            "source_paths": source_paths,
            "validation_note": "SQL 논리 백업의 테이블·경로 충돌 검사는 실제 복원 후 다시 수행됩니다.",
        }

    def extract_database_archive(self, archive_path, target_root):
        self.inspect_database_archive(archive_path)
        target_root = Path(target_root).resolve()
        with tarfile.open(str(archive_path), "r|gz") as archive:
            for member in archive:
                self._check_stop()
                name = _safe_member_name(member.name)
                destination = (target_root / PurePosixPath(name)).resolve()
                destination.relative_to(target_root)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"압축 항목을 읽을 수 없습니다: {name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, COPY_CHUNK_SIZE)

    def _check_path_collisions(self, connection, table, id_column, path_column, mappings):
        seen = {}
        collisions = []
        cursor = connection.execute(
            f"SELECT `{id_column}` AS item_id, `{path_column}` AS item_path FROM `{table}`"
        )
        for row in self._iter_rows(cursor):
            mapped = _apply_mapping(row["item_path"], mappings)
            if not mapped:
                continue
            previous = seen.get(mapped)
            if previous is not None and len(collisions) < 100:
                collisions.append(
                    {"path": mapped, "ids": [previous, int(row["item_id"])]}
                )
            else:
                seen[mapped] = int(row["item_id"])
        if collisions:
            raise ValueError(
                f"변경 후 {table} 경로 충돌이 {len(collisions)}건 이상 있습니다."
            )

    @staticmethod
    def _standard_cover_relative(library_id, file_path):
        digest = hashlib.md5(str(file_path or "").encode("utf-8")).hexdigest()
        return f"{int(library_id)}/book_{digest}.webp"

    def _apply_mappings(self, db_type, mappings, cover_root):
        if not mappings:
            return {"changed_libraries_count": 0, "changed_items_count": 0}
        connection = self.database.connect(db_type, readonly=False)
        changed_libraries = 0
        changed_items = 0
        try:
            tables = connection.tables()
            folder_media = db_type in {"audiobook", "video"}
            item_table = {"audiobook": "audiobooks", "video": "videos"}.get(
                db_type, "books"
            )
            path_column = "folder_path" if folder_media else "file_path"
            if item_table not in tables:
                return {"changed_libraries_count": 0, "changed_items_count": 0}
            self._check_path_collisions(
                connection,
                item_table,
                "id",
                path_column,
                mappings,
            )
            connection.begin()
            if "libraries" in tables:
                updates = []
                for row in self._iter_rows(
                    connection.execute("SELECT id, physical_path FROM libraries")
                ):
                    current = str(row["physical_path"] or "")
                    mapped = _replace_library_paths(current, mappings)
                    if mapped != current:
                        updates.append((mapped, int(row["id"])))
                if updates:
                    connection.executemany(
                        "UPDATE libraries SET physical_path=? WHERE id=?",
                        updates,
                    )
                    changed_libraries = len(updates)
            updates = []
            if folder_media:
                select_sql = (
                    "SELECT id, folder_path AS item_path, poster "
                    f"FROM {item_table} ORDER BY id"
                )
            else:
                select_sql = (
                    "SELECT id, library_id, file_path AS item_path, cover_image "
                    "FROM books ORDER BY id"
                )
            cursor = connection.execute(select_sql)
            for row in self._iter_rows(cursor):
                current = str(row["item_path"] or "")
                mapped = _apply_mapping(current, mappings)
                if folder_media:
                    poster = str(row["poster"] or "")
                    mapped_poster = (
                        poster
                        if poster.startswith(("http://", "https://"))
                        else _apply_mapping(poster, mappings)
                    )
                    if mapped == current and mapped_poster == poster:
                        continue
                    updates.append((mapped, mapped_poster, int(row["id"])))
                else:
                    cover = str(row["cover_image"] or "").replace("\\", "/").lstrip("/")
                    old_cover = self._standard_cover_relative(
                        row["library_id"],
                        current,
                    )
                    new_cover = self._standard_cover_relative(
                        row["library_id"],
                        mapped,
                    )
                    if mapped != current and cover == old_cover:
                        source = Path(cover_root) / old_cover
                        destination = Path(cover_root) / new_cover
                        if source.is_file() and source != destination:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            if destination.exists():
                                raise FileExistsError(
                                    f"표지 목적지 파일이 이미 존재합니다: {destination}"
                                )
                            os.replace(str(source), str(destination))
                        cover = new_cover
                    if mapped == current and cover == str(row["cover_image"] or ""):
                        continue
                    updates.append((mapped, cover, int(row["id"])))
                if len(updates) >= WRITE_BATCH_SIZE:
                    if folder_media:
                        connection.executemany(
                            f"UPDATE {item_table} SET folder_path=?, poster=? WHERE id=?",
                            updates,
                        )
                    else:
                        connection.executemany(
                            "UPDATE books SET file_path=?, cover_image=? WHERE id=?",
                            updates,
                        )
                    changed_items += len(updates)
                    updates = []
            if updates:
                if folder_media:
                    connection.executemany(
                        f"UPDATE {item_table} SET folder_path=?, poster=? WHERE id=?",
                        updates,
                    )
                else:
                    connection.executemany(
                        "UPDATE books SET file_path=?, cover_image=? WHERE id=?",
                        updates,
                    )
                changed_items += len(updates)
            if db_type == "audiobook" and "audiobook_tracks" in tables:
                track_updates = []
                for row in self._iter_rows(
                    connection.execute(
                        "SELECT id, file_path FROM audiobook_tracks ORDER BY id"
                    )
                ):
                    current = str(row["file_path"] or "")
                    mapped = _apply_mapping(current, mappings)
                    if mapped != current:
                        track_updates.append((mapped, int(row["id"])))
                    if len(track_updates) >= WRITE_BATCH_SIZE:
                        connection.executemany(
                            "UPDATE audiobook_tracks SET file_path=? WHERE id=?",
                            track_updates,
                        )
                        track_updates = []
                if track_updates:
                    connection.executemany(
                        "UPDATE audiobook_tracks SET file_path=? WHERE id=?",
                        track_updates,
                    )
            if db_type == "video" and "video_episodes" in tables:
                episode_updates = []
                for row in self._iter_rows(
                    connection.execute(
                        "SELECT id, file_path FROM video_episodes ORDER BY id"
                    )
                ):
                    current = str(row["file_path"] or "")
                    mapped = _apply_mapping(current, mappings)
                    if mapped != current:
                        episode_updates.append((mapped, int(row["id"])))
                    if len(episode_updates) >= WRITE_BATCH_SIZE:
                        connection.executemany(
                            "UPDATE video_episodes SET file_path=? WHERE id=?",
                            episode_updates,
                        )
                        episode_updates = []
                if episode_updates:
                    connection.executemany(
                        "UPDATE video_episodes SET file_path=? WHERE id=?",
                        episode_updates,
                    )
            connection.commit()
            return {
                "changed_libraries_count": changed_libraries,
                "changed_items_count": changed_items,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _restore_sql(self, db_type, sql_path):
        connection = self.database.connect(db_type, readonly=False)
        try:
            clear_mariadb_database(connection)
            connection.commit()
        finally:
            connection.close()
        self.tools.restore(self.database.target(db_type).database, sql_path)

    def migrate(
        self,
        db_archive,
        cover_archive,
        path_mappings,
        backup,
        dry_run,
        confirm_stopped,
        inspect_cover,
        extract_cover,
    ):
        if not dry_run and not confirm_stopped:
            raise ValueError("BookOasis 중지 확인 옵션을 켜 주세요.")
        if not dry_run and not backup:
            raise ValueError("MariaDB 통DB 가져오기는 롤백을 위해 DB 백업을 켜야 합니다.")
        if not dry_run and (self.health_check() or {}).get("success"):
            raise RuntimeError("BookOasis를 완전히 중지한 뒤 다시 실행해 주세요.")
        preview = self.preview(db_archive, path_mappings)
        cover_metadata = inspect_cover(cover_archive)
        preview.update(
            {
                "inspection_scope": "full",
                "cover_package": {
                    key: value
                    for key, value in cover_metadata.items()
                    if key not in {"regular_names", "cover_names", "fingerprint"}
                },
                "cover_files_count": len(cover_metadata.get("cover_names") or []),
                "cover_validation_note": "표지 파일명과 DB 참조의 최종 대조는 복원 후 BookOasis에서 확인해야 합니다.",
            }
        )
        if dry_run:
            return preview

        mappings = parse_mapping_lines(path_mappings)
        stage = Path(tempfile.mkdtemp(prefix=".mariadb_import_", dir=str(self.work_root)))
        database_backups = {}
        cover_backup = None
        restored = []
        cover_installed = False
        token = uuid.uuid4().hex[:12]
        if self.cover_root is None:
            raise ValueError("BookOasis 표지 대상 경로가 설정되지 않았습니다.")
        incoming_cover = self.cover_root.parent / f".{self.cover_root.name}.incoming_{token}"
        previous_cover = self.cover_root.parent / f"{self.cover_root.name}_before_package_{token}"
        try:
            self.extract_database_archive(db_archive, stage)
            extract_cover(cover_archive, stage, cover_metadata)
            if (self.health_check() or {}).get("success"):
                raise RuntimeError("패키지 검증 중 BookOasis가 다시 실행되었습니다.")
            package_db_types = list(preview.get("package_db_types") or DB_TYPES)
            for db_type in package_db_types:
                self._check_stop()
                self._progress("backup", len(database_backups), len(package_db_types), f"{db_type} DB를 백업하고 있습니다.")
                database_backups[db_type] = str(
                    self.database.backup(db_type, reason="before_package")
                )
            for index, db_type in enumerate(package_db_types, start=1):
                self._check_stop()
                self._progress("restore", index - 1, len(package_db_types), f"{db_type} DB를 복원하고 있습니다.")
                restored.append(db_type)
                self._restore_sql(db_type, stage / "db" / f"media_{db_type}.sql")
                self._apply_mappings(db_type, mappings, stage / "covers")
                self.database.integrity_check(db_type)
                self._progress("restore", index, len(package_db_types), f"{db_type} DB 복원을 완료했습니다.")

            self.cover_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(stage / "covers"), str(incoming_cover))
            if self.cover_root.exists():
                os.replace(str(self.cover_root), str(previous_cover))
                cover_backup = str(previous_cover)
            os.replace(str(incoming_cover), str(self.cover_root))
            cover_installed = True
            preview.update(
                {
                    "dry_run": False,
                    "database_backups": database_backups,
                    "cover_backup_path": cover_backup or "",
                    "imported_databases": package_db_types,
                }
            )
            return preview
        except Exception as error:
            if cover_installed and self.cover_root and self.cover_root.exists():
                shutil.rmtree(str(self.cover_root), ignore_errors=True)
            if previous_cover.exists() and self.cover_root:
                os.replace(str(previous_cover), str(self.cover_root))
            rollback_errors = []
            for db_type in reversed(restored):
                backup_path = database_backups.get(db_type)
                if backup_path:
                    try:
                        self._restore_sql(db_type, backup_path)
                    except Exception as rollback_error:
                        rollback_errors.append(f"{db_type}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "MariaDB 통DB 복원 실패 후 롤백도 일부 실패했습니다. "
                    + "; ".join(rollback_errors)
                ) from error
            raise
        finally:
            shutil.rmtree(str(stage), ignore_errors=True)
            if incoming_cover.exists():
                shutil.rmtree(str(incoming_cover), ignore_errors=True)
