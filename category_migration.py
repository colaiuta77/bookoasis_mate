# BookOasis 카테고리 패키지를 검사하고 안전하게 내보내거나 신규·병합으로 가져옵니다.
import io
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
MAX_MANIFEST_JSON_SIZE = 4 * 1024 * 1024
MAX_METADATA_JSON_SIZE = 2 * 1024 * 1024 * 1024
MAX_INLINE_INSPECTION_METADATA_SIZE = 64 * 1024 * 1024
MAX_LIBRARY_JSON_PREFIX_CHARS = 8 * 1024 * 1024
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
AUDIOBOOK_COLUMNS = (
    "library_id",
    "title",
    "sort_title",
    "web_id",
    "author",
    "publisher",
    "code",
    "poster",
    "premiered",
    "ratings",
    "author_intro",
    "description",
    "folder_name",
    "folder_path",
    "total_duration",
    "total_tracks",
    "file_type",
    "is_favorite",
    "created_at",
    "updated_at",
    "is_deleted",
    "deleted_at",
)
AUDIOBOOK_TRACK_COLUMNS = (
    "audiobook_id",
    "track_number",
    "track_code",
    "title",
    "filename",
    "file_path",
    "file_mtime",
    "file_size",
    "duration",
    "format",
    "created_at",
)
VIDEO_COLUMNS = (
    "library_id", "title", "sort_title", "web_id", "genres", "poster",
    "backdrop", "premiered", "description", "folder_name", "folder_path",
    "total_duration", "total_episodes", "is_favorite", "created_at",
    "updated_at", "is_deleted", "deleted_at",
)
VIDEO_EPISODE_COLUMNS = (
    "video_id", "episode_number", "episode_code", "title", "filename",
    "file_path", "file_mtime", "file_size", "duration", "width", "height",
    "premiered", "format", "needs_transcode", "subtitle_path",
    "container_verified",
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
    def _json_member_info(archive, name, max_size):
        try:
            info = archive.getinfo(name)
        except KeyError:
            raise ValueError(f"올바른 패키지가 아닙니다. {name} 파일이 없습니다.")
        if info.file_size > max_size:
            raise ValueError(
                f"{name} 파일이 허용 크기를 초과했습니다. "
                f"최대 {max_size // (1024 * 1024):,}MB까지 지원합니다."
            )
        return info

    @staticmethod
    def _read_json_member(archive, name):
        max_size = (
            MAX_MANIFEST_JSON_SIZE
            if name == "manifest.json"
            else MAX_METADATA_JSON_SIZE
        )
        info = CategoryMigrationEngine._json_member_info(archive, name, max_size)
        try:
            with archive.open(info, "r") as source:
                with io.TextIOWrapper(source, encoding="utf-8") as text:
                    return json.load(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"{name} 파일을 읽을 수 없습니다: {error}")
        except MemoryError as error:
            raise ValueError(
                f"{name} 파일을 처리할 메모리가 부족합니다. "
                "더 작은 카테고리로 나누어 다시 내보내 주세요."
            ) from error

    @staticmethod
    def _read_library_metadata(archive):
        info = CategoryMigrationEngine._json_member_info(
            archive,
            "metadata.json",
            MAX_METADATA_JSON_SIZE,
        )
        try:
            with archive.open(info, "r") as source:
                with io.TextIOWrapper(source, encoding="utf-8") as text:
                    prefix = text.read(MAX_LIBRARY_JSON_PREFIX_CHARS)
        except UnicodeDecodeError as error:
            raise ValueError(f"metadata.json 파일을 읽을 수 없습니다: {error}")

        match = re.search(r'"library"\s*:\s*', prefix)
        if match is None:
            raise ValueError("metadata.json에서 library 정보를 찾을 수 없습니다.")
        start = match.end()
        if start >= len(prefix) or prefix[start] != "{":
            raise ValueError("metadata.json의 library 형식이 올바르지 않습니다.")

        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(prefix)):
            character = prefix[index]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    try:
                        library = json.loads(prefix[start : index + 1])
                    except json.JSONDecodeError as error:
                        raise ValueError(
                            f"metadata.json의 library 정보를 읽을 수 없습니다: {error}"
                        ) from error
                    if not isinstance(library, dict):
                        raise ValueError("패키지 library 형식이 올바르지 않습니다.")
                    return library
        raise ValueError(
            "metadata.json의 library 정보가 너무 크거나 형식이 올바르지 않습니다."
        )

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
                if not isinstance(manifest, dict):
                    raise ValueError("패키지 manifest 형식이 올바르지 않습니다.")
                metadata_info = self._json_member_info(
                    archive,
                    "metadata.json",
                    MAX_METADATA_JSON_SIZE,
                )
                summary_only = (
                    metadata_info.file_size > MAX_INLINE_INSPECTION_METADATA_SIZE
                )
                metadata = (
                    None
                    if summary_only
                    else self._read_json_member(archive, "metadata.json")
                )
                library = (
                    self._read_library_metadata(archive)
                    if summary_only
                    else metadata.get("library")
                )
                covers_count = 0
                cover_uncompressed_size = 0
                for member in archive.infolist():
                    if member.filename.startswith("covers/") and not member.is_dir():
                        covers_count += 1
                        cover_uncompressed_size += member.file_size
        except zipfile.BadZipFile as error:
            raise ValueError(f"ZIP 패키지를 읽을 수 없습니다: {error}")

        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("패키지 메타데이터 형식이 올바르지 않습니다.")
        if str(manifest.get("export_version") or "") not in SUPPORTED_PACKAGE_VERSIONS:
            raise ValueError(
                f"지원하지 않는 패키지 버전입니다: {manifest.get('export_version')}"
            )
        media_kind = str(manifest.get("media_kind") or "book").strip().lower()
        db_type = str(manifest.get("db_type") or "general").strip().lower()
        is_audiobook = media_kind == "audiobook" or db_type == "audiobook"
        is_video = media_kind == "video" or db_type == "video"
        if not isinstance(library, dict):
            raise ValueError("패키지 library 형식이 올바르지 않습니다.")
        deferred_counts = []
        if summary_only:
            books_count = int(manifest.get("books_count") or 0)
            audiobooks_count = int(manifest.get("audiobooks_count") or 0)
            audiobook_tracks_count = int(
                manifest.get("audiobook_tracks_count") or 0
            )
            audiobook_progress_count = int(
                manifest.get("audiobook_progress_count") or 0
            )
            audiobook_track_progress_count = int(
                manifest.get("audiobook_track_progress_count") or 0
            )
            videos_count = int(manifest.get("videos_count") or 0)
            video_episodes_count = int(manifest.get("video_episodes_count") or 0)
            video_progress_count = int(manifest.get("video_progress_count") or 0)
            video_episode_progress_count = int(
                manifest.get("video_episode_progress_count") or 0
            )
            offsets_count = int(manifest.get("offsets_count") or 0)
            if "offsets_count" not in manifest and not (is_audiobook or is_video):
                deferred_counts.append("offsets_count")
            user_progress_count = int(manifest.get("user_progress_count") or 0)
            user_favorites_count = int(manifest.get("user_favorites_count") or 0)
        else:
            books = metadata.get("books", [])
            offsets = metadata.get("offsets", {})
            audiobooks = metadata.get("audiobooks", [])
            audiobook_tracks = metadata.get("audiobook_tracks", [])
            audiobook_progress = metadata.get("audiobook_progress", [])
            audiobook_track_progress = metadata.get("audiobook_track_progress", [])
            videos = metadata.get("videos", [])
            video_episodes = metadata.get("video_episodes", [])
            video_progress = metadata.get("video_progress", [])
            video_episode_progress = metadata.get("video_episode_progress", [])
            if is_audiobook:
                if not all(
                    isinstance(items, list)
                    for items in (
                        audiobooks,
                        audiobook_tracks,
                        audiobook_progress,
                        audiobook_track_progress,
                    )
                ):
                    raise ValueError("오디오북 패키지 데이터 형식이 올바르지 않습니다.")
            elif is_video:
                if not all(
                    isinstance(items, list)
                    for items in (
                        videos,
                        video_episodes,
                        video_progress,
                        video_episode_progress,
                    )
                ):
                    raise ValueError("비디오 패키지 데이터 형식이 올바르지 않습니다.")
            elif not isinstance(books, list) or not isinstance(offsets, dict):
                raise ValueError("패키지 books 또는 offsets 형식이 올바르지 않습니다.")
            books_count = len(books)
            audiobooks_count = len(audiobooks)
            audiobook_tracks_count = len(audiobook_tracks)
            audiobook_progress_count = len(audiobook_progress)
            audiobook_track_progress_count = len(audiobook_track_progress)
            videos_count = len(videos)
            video_episodes_count = len(video_episodes)
            video_progress_count = len(video_progress)
            video_episode_progress_count = len(video_episode_progress)
            offsets_count = sum(
                len(items) for items in offsets.values() if isinstance(items, list)
            )
            user_progress_count = len(metadata.get("user_progress", []))
            user_favorites_count = len(metadata.get("user_favorites", []))
        physical_paths = library.get("physical_paths", [])
        if not isinstance(physical_paths, list):
            raise ValueError("패키지 물리 경로 형식이 올바르지 않습니다.")

        return {
            "path": str(path),
            "filename": path.name,
            "size": path.stat().st_size,
            "export_version": str(manifest.get("export_version")),
            "created_at": manifest.get("created_at"),
            "db_type": "audiobook" if is_audiobook else ("video" if is_video else db_type),
            "media_kind": "audiobook" if is_audiobook else ("video" if is_video else "book"),
            "library_id": manifest.get("library_id"),
            "library_name": manifest.get("library_name") or library.get("name"),
            "books_count": books_count,
            "audiobooks_count": audiobooks_count,
            "audiobook_tracks_count": audiobook_tracks_count,
            "audiobook_progress_count": audiobook_progress_count,
            "audiobook_track_progress_count": audiobook_track_progress_count,
            "videos_count": videos_count,
            "video_episodes_count": video_episodes_count,
            "video_progress_count": video_progress_count,
            "video_episode_progress_count": video_episode_progress_count,
            "covers_count": covers_count,
            "offsets_count": offsets_count,
            "user_progress_count": user_progress_count,
            "user_favorites_count": user_favorites_count,
            "physical_paths": [str(path) for path in physical_paths],
            "root_paths_count": len(physical_paths),
            "cover_uncompressed_size": cover_uncompressed_size,
            "metadata_json_size": metadata_info.file_size,
            "metadata_summary_only": summary_only,
            "deferred_counts": deferred_counts,
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
        raw_path = Path(str(cover_image or "").split("?", 1)[0]).expanduser()
        if raw_path.is_absolute() and raw_path.is_file():
            try:
                raw_path.resolve().relative_to(self.cover_root)
                return raw_path.resolve()
            except ValueError:
                return None
        normalized = normalize_cover_path(cover_image)
        if not normalized:
            return None
        return resolve_cover_path(str(self.cover_root), normalized)

    @staticmethod
    def _same_file_content(left, right):
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    def _install_staged_covers(self, stage, final_cover_dir, staged_names, db_type):
        if not staged_names:
            return {}, [], False
        if os.path.lexists(final_cover_dir):
            if final_cover_dir.is_symlink() or not final_cover_dir.is_dir():
                raise RuntimeError("표지 대상 경로가 일반 디렉터리가 아닙니다.")
            created_directory = False
        else:
            final_cover_dir.mkdir(parents=True)
            created_directory = True
        installed = {}
        created = []
        try:
            for filename in sorted(staged_names):
                source = stage / filename
                source_name = Path(filename)
                sequence = 0
                while True:
                    if sequence == 0:
                        candidate_name = source_name.name
                    else:
                        suffix = f"__{db_type}" if sequence == 1 else f"__{db_type}_{sequence}"
                        candidate_name = f"{source_name.stem}{suffix}{source_name.suffix}"
                    destination = final_cover_dir / candidate_name
                    if os.path.lexists(destination):
                        if destination.is_symlink() or not destination.is_file():
                            raise RuntimeError(
                                f"표지 대상 경로가 일반 파일이 아닙니다: {candidate_name}"
                            )
                        if self._same_file_content(source, destination):
                            installed[filename] = candidate_name
                            break
                        sequence += 1
                        continue
                    try:
                        with source.open("rb") as source_handle, destination.open("xb") as target_handle:
                            shutil.copyfileobj(source_handle, target_handle, 1024 * 1024)
                    except FileExistsError:
                        continue
                    except Exception:
                        destination.unlink(missing_ok=True)
                        raise
                    created.append(candidate_name)
                    installed[filename] = candidate_name
                    break
            return installed, created, created_directory
        except Exception:
            for filename in created:
                (final_cover_dir / filename).unlink(missing_ok=True)
            if created_directory:
                try:
                    final_cover_dir.rmdir()
                except OSError:
                    pass
            raise

    @staticmethod
    def _safe_filename(name, fallback):
        cleaned = "".join(
            character
            for character in str(name or "")
            if character.isalnum() or character in {"_", "-"}
        ).strip()
        return cleaned or fallback

    def _package_output_path(self, library_name, db_type, library_id):
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
        return output

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
        if str(db_type or "").strip().lower() == "video":
            return self._export_video_one(db_path, library_id)
        if str(db_type or "").strip().lower() == "audiobook":
            return self._export_audiobook_one(db_path, library_id)
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

            output = self._package_output_path(library_name, db_type, library_id)
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
                "offsets_count": sum(len(items) for items in offsets_payload.values()),
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

    def _export_video_one(self, db_path, library_id):
        connection = self._database_connection(db_path, "video", readonly=True)
        partial_path = None
        try:
            tables = self._tables(connection)
            required = {"libraries", "videos", "video_episodes"}
            if not required.issubset(tables):
                raise RuntimeError(
                    "비디오 필수 테이블이 없습니다: "
                    + ", ".join(sorted(required - tables))
                )
            if hasattr(connection, "begin"):
                connection.begin()
            else:
                connection.execute("BEGIN")
            library_row = connection.execute(
                "SELECT * FROM libraries WHERE id = ?", (library_id,)
            ).fetchone()
            if library_row is None:
                raise ValueError(f"비디오 카테고리 ID {library_id}를 찾을 수 없습니다.")
            library = dict(library_row)
            library_name = library.get("name") or f"category_{library_id}"
            root_paths = self._root_paths(library.get("physical_path"))
            columns = self._columns(connection, "videos")
            active = (
                "(is_deleted IS NULL OR is_deleted = 0)"
                if "is_deleted" in columns else "1 = 1"
            )
            videos = [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM videos WHERE library_id = ? AND {active}",
                    (library_id,),
                ).fetchall()
            ]
            video_payload = []
            episode_payload = []
            progress_payload = []
            episode_progress_payload = []
            cover_paths = {}
            for video_index, video in enumerate(videos):
                self._check_stop()
                root_index, relative_folder = self._relative_path(
                    video.get("folder_path"), root_paths
                )
                item = dict(video)
                item.update(
                    root_index=root_index,
                    relative_folder_path=relative_folder,
                )
                video_payload.append(item)
                poster = str(video.get("poster") or "").strip()
                if poster and not poster.startswith(("http://", "https://")):
                    cover = self._cover_path(poster)
                    if cover is not None and cover.is_file():
                        try:
                            relative_cover = cover.resolve().relative_to(
                                self.cover_root
                            ).as_posix()
                        except ValueError:
                            relative_cover = cover.name
                        cover_paths[relative_cover] = cover
                number_by_id = {}
                for row in connection.execute(
                    "SELECT * FROM video_episodes WHERE video_id = ? "
                    "ORDER BY episode_number, id",
                    (video.get("id"),),
                ).fetchall():
                    episode = dict(row)
                    number_by_id[int(episode.get("id") or 0)] = int(
                        episode.get("episode_number") or 0
                    )
                    episode_root, relative_path = self._relative_path(
                        episode.get("file_path"), root_paths
                    )
                    episode.update(
                        video_export_index=video_index,
                        root_index=episode_root,
                        relative_path=relative_path,
                    )
                    episode_payload.append(episode)
                if "video_progress" in tables:
                    for row in connection.execute(
                        "SELECT * FROM video_progress WHERE video_id = ?",
                        (video.get("id"),),
                    ).fetchall():
                        progress = dict(row)
                        progress["video_export_index"] = video_index
                        progress["current_episode_number"] = number_by_id.get(
                            int(progress.get("current_episode_id") or 0), 0
                        )
                        progress_payload.append(progress)
                if "video_episode_progress" in tables:
                    for row in connection.execute(
                        "SELECT * FROM video_episode_progress WHERE video_id = ?",
                        (video.get("id"),),
                    ).fetchall():
                        progress = dict(row)
                        progress["video_export_index"] = video_index
                        progress["episode_number"] = number_by_id.get(
                            int(progress.get("episode_id") or 0), 0
                        )
                        episode_progress_payload.append(progress)
                if video_index % 100 == 0 or video_index + 1 == len(videos):
                    self._progress(
                        "export_videos", video_index + 1, len(videos),
                        f"{library_name} 비디오와 에피소드를 수집하고 있습니다.",
                    )
            output = self._package_output_path(library_name, "video", library_id)
            partial_path = output.with_name(output.name + ".part")
            manifest = {
                "export_version": PACKAGE_VERSION,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "db_type": "video", "media_kind": "video",
                "library_id": library_id, "library_name": library_name,
                "root_paths_count": len(root_paths), "books_count": 0,
                "videos_count": len(video_payload),
                "video_episodes_count": len(episode_payload),
                "video_progress_count": len(progress_payload),
                "video_episode_progress_count": len(episode_progress_payload),
                "covers_count": len(cover_paths), "user_progress_count": 0,
                "user_favorites_count": 0,
            }
            metadata = {
                "library": {
                    "id": library_id, "name": library_name,
                    "physical_paths": root_paths,
                    "cron_schedule": library.get("cron_schedule"),
                    "scan_status": library.get("scan_status", "ready"),
                    "is_remote": library.get("is_remote", 0),
                    "vfs_refresh_before_scan": library.get(
                        "vfs_refresh_before_scan", 0
                    ),
                    "rclone_rc_url": library.get("rclone_rc_url"),
                    "icon": library.get("icon", "fa-video"),
                    "color": library.get("color", "#94a3b8"),
                    "hide_cover": library.get("hide_cover", 0),
                },
                "books": [], "offsets": {}, "videos": video_payload,
                "video_episodes": episode_payload,
                "video_progress": progress_payload,
                "video_episode_progress": episode_progress_payload,
                "user_progress": [], "user_favorites": [],
            }
            with zipfile.ZipFile(
                str(partial_path), "w", compression=zipfile.ZIP_STORED
            ) as archive:
                archive.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                archive.writestr(
                    "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2)
                )
                for index, (relative, cover_path) in enumerate(
                    sorted(cover_paths.items()), 1
                ):
                    self._check_stop()
                    archive.write(str(cover_path), arcname=f"covers/{relative}")
                    self._progress(
                        "export_covers", index, len(cover_paths),
                        f"{library_name} 포스터를 패키징하고 있습니다.",
                    )
            os.replace(str(partial_path), str(output))
            partial_path = None
            return {**manifest, "path": str(output), "filename": output.name,
                    "size": output.stat().st_size}
        except Exception:
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def _export_audiobook_one(self, db_path, library_id):
        connection = self._database_connection(
            db_path,
            "audiobook",
            readonly=True,
        )
        partial_path = None
        try:
            tables = self._tables(connection)
            required_tables = {"libraries", "audiobooks", "audiobook_tracks"}
            if not required_tables.issubset(tables):
                missing = ", ".join(sorted(required_tables - tables))
                raise RuntimeError(f"오디오북 필수 테이블이 없습니다: {missing}")
            if hasattr(connection, "begin"):
                connection.begin()
            else:
                connection.execute("BEGIN")
            library_row = connection.execute(
                "SELECT * FROM libraries WHERE id = ?",
                (library_id,),
            ).fetchone()
            if library_row is None:
                raise ValueError(f"오디오북 카테고리 ID {library_id}를 찾을 수 없습니다.")
            library = dict(library_row)
            library_name = library.get("name") or f"category_{library_id}"
            root_paths = self._root_paths(library.get("physical_path"))
            audiobook_columns = self._columns(connection, "audiobooks")
            active = (
                "(is_deleted IS NULL OR is_deleted = 0)"
                if "is_deleted" in audiobook_columns
                else "1 = 1"
            )
            rows = connection.execute(
                f"SELECT * FROM audiobooks WHERE library_id = ? AND {active}",
                (library_id,),
            ).fetchall()
            audiobooks = [dict(row) for row in rows]
            audiobooks_payload = []
            tracks_payload = []
            progress_payload = []
            track_progress_payload = []
            cover_paths = {}

            for audiobook_index, audiobook in enumerate(audiobooks):
                self._check_stop()
                root_index, relative_folder = self._relative_path(
                    audiobook.get("folder_path"),
                    root_paths,
                )
                payload = dict(audiobook)
                payload["root_index"] = root_index
                payload["relative_folder_path"] = relative_folder
                audiobooks_payload.append(payload)

                poster = str(audiobook.get("poster") or "").strip()
                if poster and not poster.startswith(("http://", "https://")):
                    cover = self._cover_path(poster)
                    if cover is not None and cover.is_file():
                        try:
                            relative_cover = cover.resolve().relative_to(
                                self.cover_root
                            ).as_posix()
                        except ValueError:
                            relative_cover = cover.name
                        cover_paths[relative_cover] = cover

                track_number_by_id = {}
                track_rows = connection.execute(
                    "SELECT * FROM audiobook_tracks WHERE audiobook_id = ? "
                    "ORDER BY track_number, id",
                    (audiobook.get("id"),),
                ).fetchall()
                for track in track_rows:
                    item = dict(track)
                    track_id = int(item.get("id") or 0)
                    track_number_by_id[track_id] = int(
                        item.get("track_number") or 0
                    )
                    track_root_index, relative_path = self._relative_path(
                        item.get("file_path"),
                        root_paths,
                    )
                    item["audiobook_export_index"] = audiobook_index
                    item["root_index"] = track_root_index
                    item["relative_path"] = relative_path
                    tracks_payload.append(item)

                if "audiobook_progress" in tables:
                    for progress in connection.execute(
                        "SELECT * FROM audiobook_progress WHERE audiobook_id = ?",
                        (audiobook.get("id"),),
                    ).fetchall():
                        item = dict(progress)
                        item["audiobook_export_index"] = audiobook_index
                        item["current_track_number"] = track_number_by_id.get(
                            int(item.get("current_track_id") or 0),
                            0,
                        )
                        progress_payload.append(item)

                if "audiobook_track_progress" in tables:
                    for progress in connection.execute(
                        "SELECT * FROM audiobook_track_progress "
                        "WHERE audiobook_id = ?",
                        (audiobook.get("id"),),
                    ).fetchall():
                        item = dict(progress)
                        item["audiobook_export_index"] = audiobook_index
                        item["track_number"] = track_number_by_id.get(
                            int(item.get("track_id") or 0),
                            0,
                        )
                        track_progress_payload.append(item)

                if audiobook_index % 100 == 0 or audiobook_index + 1 == len(audiobooks):
                    self._progress(
                        "export_audiobooks",
                        audiobook_index + 1,
                        len(audiobooks),
                        f"{library_name} 오디오북과 트랙 정보를 수집하고 있습니다.",
                    )

            output = self._package_output_path(
                library_name,
                "audiobook",
                library_id,
            )
            partial_path = output.with_name(output.name + ".part")
            manifest = {
                "export_version": PACKAGE_VERSION,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "db_type": "audiobook",
                "media_kind": "audiobook",
                "library_id": library_id,
                "library_name": library_name,
                "root_paths_count": len(root_paths),
                "books_count": 0,
                "audiobooks_count": len(audiobooks_payload),
                "audiobook_tracks_count": len(tracks_payload),
                "audiobook_progress_count": len(progress_payload),
                "audiobook_track_progress_count": len(track_progress_payload),
                "user_progress_count": 0,
                "user_favorites_count": 0,
                "covers_count": len(cover_paths),
            }
            metadata = {
                "library": {
                    "id": library_id,
                    "name": library_name,
                    "physical_paths": root_paths,
                    "cron_schedule": library.get("cron_schedule"),
                    "scan_status": library.get("scan_status", "ready"),
                    "is_remote": library.get("is_remote", 0),
                    "vfs_refresh_before_scan": library.get(
                        "vfs_refresh_before_scan",
                        0,
                    ),
                    "rclone_rc_url": library.get("rclone_rc_url"),
                    "icon": library.get("icon", "fa-headphones"),
                    "color": library.get("color", "#94a3b8"),
                    "hide_cover": library.get("hide_cover", 0),
                },
                "books": [],
                "offsets": {},
                "audiobooks": audiobooks_payload,
                "audiobook_tracks": tracks_payload,
                "audiobook_progress": progress_payload,
                "audiobook_track_progress": track_progress_payload,
                "user_progress": [],
                "user_favorites": [],
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
                        f"{library_name} 포스터를 패키징하고 있습니다.",
                    )
            os.replace(str(partial_path), str(output))
            partial_path = None
            return {
                "path": str(output),
                "filename": output.name,
                "size": output.stat().st_size,
                "db_type": "audiobook",
                "media_kind": "audiobook",
                "library_id": library_id,
                "library_name": library_name,
                "books_count": 0,
                "audiobooks_count": len(audiobooks_payload),
                "audiobook_tracks_count": len(tracks_payload),
                "audiobook_progress_count": len(progress_payload),
                "audiobook_track_progress_count": len(track_progress_payload),
                "covers_count": len(cover_paths),
                "offsets_count": 0,
                "user_progress_count": 0,
                "user_favorites_count": 0,
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
    def _safe_optional_relative_path(value):
        text = str(value or "").replace("\\", "/").strip()
        if text in {"", "."}:
            return PurePosixPath()
        pure = PurePosixPath(text)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise ValueError(f"안전하지 않은 오디오북 상대 경로입니다: {value}")
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

    def _import_video_category(
        self, db_path, package_path, target_paths, name=None, merge_to=None,
        backup=True, inspection=None,
    ):
        inspection = inspection or self.inspect_package(package_path)
        paths = parse_paths(target_paths)
        if not paths:
            raise ValueError("가져올 대상 물리 경로를 하나 이상 입력해 주세요.")
        targets = []
        for raw_path in paths:
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                raise ValueError("대상 물리 경로는 절대 경로로 입력해 주세요.")
            target.mkdir(parents=True, exist_ok=True)
            targets.append(Path(os.path.abspath(str(target))))
        connection = self._database_connection(db_path, "video", readonly=False)
        try:
            self._check_required_sqlite_modules(connection)
            self._quick_check(connection, "video")
        finally:
            connection.close()
        package = self._work_path(package_path, must_exist=True)
        if inspection["cover_uncompressed_size"] > shutil.disk_usage(
            str(self.cover_root)
        ).free:
            raise OSError("포스터 복원에 필요한 디스크 여유 공간이 부족합니다.")
        backup_path = self._backup_database(db_path, "video") if backup else None
        stage = Path(tempfile.mkdtemp(
            prefix=".bookoasis_mate_video_import_", dir=str(self.cover_root)
        ))
        final_cover_dir = None
        rollback_cover_dir = None
        connection = None
        moved_stage = False
        moved_cover_names = []
        backed_up_cover_names = []
        created_merge_cover_dir = False
        committed = False
        try:
            with zipfile.ZipFile(str(package), "r") as archive:
                metadata = self._read_json_member(archive, "metadata.json")
                library_meta = metadata["library"]
                videos = metadata.get("videos", [])
                episodes = metadata.get("video_episodes", [])
                progress_rows = metadata.get("video_progress", [])
                episode_progress_rows = metadata.get("video_episode_progress", [])
                cover_members = [
                    member for member in archive.infolist()
                    if member.filename.startswith("covers/") and not member.is_dir()
                ]
                staged_names = set()
                for index, member in enumerate(cover_members, 1):
                    self._check_stop()
                    filename = Path(member.filename).name
                    if not filename or filename in staged_names:
                        raise ValueError(
                            f"중복되거나 잘못된 포스터 파일명입니다: {member.filename}"
                        )
                    staged_names.add(filename)
                    with archive.open(member) as source, (stage / filename).open("wb") as out:
                        shutil.copyfileobj(source, out, 1024 * 1024)
                    self._progress(
                        "import_covers", index, len(cover_members),
                        "비디오 포스터를 임시 디렉터리에 복원하고 있습니다.",
                    )
            connection = self._database_connection(db_path, "video", readonly=False)
            tables = self._tables(connection)
            required = {"libraries", "videos", "video_episodes"}
            if not required.issubset(tables):
                raise RuntimeError(
                    "비디오 필수 테이블이 없습니다: "
                    + ", ".join(sorted(required - tables))
                )
            library_columns = self._columns(connection, "libraries")
            video_columns = self._columns(connection, "videos")
            episode_columns = self._columns(connection, "video_episodes")
            if hasattr(connection, "begin"):
                connection.begin(immediate=True)
            else:
                connection.execute("BEGIN IMMEDIATE")
            merge_value = str(merge_to or "").strip()
            mode = "merge" if merge_value else "new"
            added_paths = []
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
                    raise ValueError(f"병합 대상 비디오 카테고리를 찾을 수 없습니다: {merge_value}")
                library_id = int(target_library["id"])
                library_name = str(target_library["name"])
                physical_paths = self._root_paths(target_library["physical_path"])
                for target in targets:
                    if str(target) not in physical_paths:
                        physical_paths.append(str(target))
                        added_paths.append(str(target))
                connection.execute(
                    "UPDATE libraries SET physical_path = ? WHERE id = ?",
                    ("\n".join(physical_paths), library_id),
                )
            else:
                library_name = str(
                    name or library_meta.get("name") or "Imported Videos"
                ).strip()
                if connection.execute(
                    "SELECT id FROM libraries WHERE name = ?", (library_name,)
                ).fetchone() is not None:
                    library_name += f" (Imported {datetime.now().strftime('%H%M%S')})"
                physical_paths = [str(target) for target in targets]
                added_paths = list(physical_paths)
                values = {
                    "name": library_name,
                    "physical_path": "\n".join(physical_paths),
                    "cron_schedule": library_meta.get("cron_schedule"),
                    "scan_status": "ready",
                    "is_remote": library_meta.get("is_remote", 0),
                    "vfs_refresh_before_scan": library_meta.get(
                        "vfs_refresh_before_scan", 0
                    ),
                    "rclone_rc_url": library_meta.get("rclone_rc_url"),
                    "icon": library_meta.get("icon", "fa-video"),
                    "color": library_meta.get("color", "#94a3b8"),
                    "hide_cover": library_meta.get("hide_cover", 0),
                }
                library_id = self._insert_row(
                    connection, "libraries",
                    {key: value for key, value in values.items() if key in library_columns},
                )
            existing_videos = int(connection.execute(
                "SELECT COUNT(*) FROM videos WHERE library_id = ?", (library_id,)
            ).fetchone()[0])
            existing_episodes = int(connection.execute(
                "SELECT COUNT(*) FROM video_episodes e JOIN videos v ON v.id=e.video_id "
                "WHERE v.library_id = ?", (library_id,)
            ).fetchone()[0])
            final_cover_dir = self.cover_root / str(library_id)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            index_to_id = {}
            index_to_root = {}
            episode_lookup = {}
            imported_videos = skipped_videos = imported_episodes = skipped_episodes = 0
            for index, video in enumerate(videos):
                self._check_stop()
                root_index = int(video.get("root_index") or 0)
                if root_index < 0 or root_index >= len(targets):
                    root_index = 0
                root = targets[root_index]
                index_to_root[index] = root
                relative = self._safe_optional_relative_path(
                    video.get("relative_folder_path")
                )
                folder_path = root.joinpath(*relative.parts)
                duplicate = connection.execute(
                    "SELECT id FROM videos WHERE folder_path = ?", (str(folder_path),)
                ).fetchone()
                if duplicate is not None:
                    video_id = int(duplicate["id"])
                    skipped_videos += 1
                else:
                    poster = str(video.get("poster") or "").strip()
                    if poster and not poster.startswith(("http://", "https://")):
                        poster = str(final_cover_dir / Path(poster).name)
                    values = {key: video.get(key) for key in VIDEO_COLUMNS}
                    values.update(
                        library_id=library_id,
                        title=video.get("title") or "Unknown Video",
                        poster=poster,
                        folder_name=video.get("folder_name") or folder_path.name,
                        folder_path=str(folder_path),
                        created_at=video.get("created_at") or now,
                        updated_at=video.get("updated_at") or now,
                    )
                    video_id = self._insert_row(
                        connection, "videos",
                        {key: value for key, value in values.items() if key in video_columns},
                    )
                    imported_videos += 1
                index_to_id[index] = int(video_id)
                if index % 100 == 0 or index + 1 == len(videos):
                    self._progress(
                        "import_videos", index + 1, len(videos),
                        "비디오 정보를 대상 카테고리에 저장하고 있습니다.",
                    )
            for index, episode in enumerate(episodes):
                video_index = int(episode.get("video_export_index", -1))
                if video_index not in index_to_id:
                    continue
                video_id = index_to_id[video_index]
                root_index = int(episode.get("root_index") or 0)
                root = targets[root_index] if 0 <= root_index < len(targets) else index_to_root[video_index]
                relative = self._safe_relative_path(episode.get("relative_path"))
                file_path = root.joinpath(*relative.parts)
                duplicate = connection.execute(
                    "SELECT id FROM video_episodes WHERE file_path = ?", (str(file_path),)
                ).fetchone()
                episode_number = int(episode.get("episode_number") or 0)
                if duplicate is not None:
                    episode_id = int(duplicate["id"])
                    skipped_episodes += 1
                else:
                    values = {key: episode.get(key) for key in VIDEO_EPISODE_COLUMNS}
                    values.update(
                        video_id=video_id,
                        filename=episode.get("filename") or file_path.name,
                        file_path=str(file_path),
                    )
                    episode_id = self._insert_row(
                        connection, "video_episodes",
                        {key: value for key, value in values.items() if key in episode_columns},
                    )
                    imported_episodes += 1
                if episode_number > 0:
                    episode_lookup[(video_id, episode_number)] = int(episode_id)
                if index % 200 == 0 or index + 1 == len(episodes):
                    self._progress(
                        "import_video_episodes", index + 1, len(episodes),
                        "비디오 에피소드 정보를 저장하고 있습니다.",
                    )
            valid_users = {
                int(row["id"]) for row in connection.execute(
                    "SELECT id FROM users"
                ).fetchall()
            } if "users" in tables else set()
            imported_progress = skipped_progress = 0
            if "video_progress" in tables:
                columns = self._columns(connection, "video_progress")
                for row in progress_rows:
                    index = int(row.get("video_export_index", -1))
                    user_id = int(row.get("user_id") or 0)
                    if index not in index_to_id or user_id not in valid_users:
                        skipped_progress += 1
                        continue
                    video_id = index_to_id[index]
                    episode_number = int(row.get("current_episode_number") or 0)
                    values = {
                        key: value for key, value in row.items()
                        if key not in {"id", "video_id", "video_export_index",
                                       "current_episode_id", "current_episode_number"}
                        and key in columns
                    }
                    values.update(
                        video_id=video_id, user_id=user_id,
                        current_episode_id=episode_lookup.get((video_id, episode_number)),
                    )
                    self._upsert_row(
                        connection, "video_progress", ("video_id", "user_id"), values
                    )
                    imported_progress += 1
            imported_episode_progress = skipped_episode_progress = 0
            if "video_episode_progress" in tables:
                columns = self._columns(connection, "video_episode_progress")
                for row in episode_progress_rows:
                    index = int(row.get("video_export_index", -1))
                    user_id = int(row.get("user_id") or 0)
                    if index not in index_to_id or user_id not in valid_users:
                        skipped_episode_progress += 1
                        continue
                    video_id = index_to_id[index]
                    episode_id = episode_lookup.get(
                        (video_id, int(row.get("episode_number") or 0))
                    )
                    if not episode_id:
                        skipped_episode_progress += 1
                        continue
                    values = {
                        key: value for key, value in row.items()
                        if key not in {"id", "video_id", "episode_id",
                                       "video_export_index", "episode_number"}
                        and key in columns
                    }
                    values.update(video_id=video_id, episode_id=episode_id, user_id=user_id)
                    self._upsert_row(
                        connection, "video_episode_progress",
                        ("video_id", "episode_id", "user_id"), values,
                    )
                    imported_episode_progress += 1
            if mode == "new":
                if final_cover_dir.exists():
                    raise FileExistsError("신규 비디오 포스터 디렉터리가 이미 존재합니다.")
                os.replace(str(stage), str(final_cover_dir))
                moved_stage = True
            elif staged_names:
                if not final_cover_dir.exists():
                    final_cover_dir.mkdir(parents=True)
                    created_merge_cover_dir = True
                rollback_cover_dir = Path(tempfile.mkdtemp(
                    prefix=".bookoasis_mate_video_cover_rollback_",
                    dir=str(self.cover_root),
                ))
                for filename in sorted(staged_names):
                    source, destination = stage / filename, final_cover_dir / filename
                    if destination.exists():
                        os.replace(str(destination), str(rollback_cover_dir / filename))
                        backed_up_cover_names.append(filename)
                    os.replace(str(source), str(destination))
                    moved_cover_names.append(filename)
            verified_videos = int(connection.execute(
                "SELECT COUNT(*) FROM videos WHERE library_id = ?", (library_id,)
            ).fetchone()[0])
            verified_episodes = int(connection.execute(
                "SELECT COUNT(*) FROM video_episodes e JOIN videos v ON v.id=e.video_id "
                "WHERE v.library_id = ?", (library_id,)
            ).fetchone()[0])
            if verified_videos != existing_videos + imported_videos:
                raise RuntimeError("가져온 비디오 수 검증에 실패했습니다.")
            if verified_episodes != existing_episodes + imported_episodes:
                raise RuntimeError("가져온 비디오 에피소드 수 검증에 실패했습니다.")
            connection.commit()
            committed = True
            if rollback_cover_dir is not None:
                shutil.rmtree(str(rollback_cover_dir), ignore_errors=True)
            return {
                "mode": mode, "db_type": "video", "media_kind": "video",
                "library_id": library_id, "library_name": library_name,
                "physical_paths": physical_paths, "added_physical_paths": added_paths,
                "videos_count": imported_videos,
                "skipped_duplicate_videos_count": skipped_videos,
                "video_episodes_count": imported_episodes,
                "skipped_duplicate_episodes_count": skipped_episodes,
                "video_progress_count": imported_progress,
                "skipped_video_progress_count": skipped_progress,
                "video_episode_progress_count": imported_episode_progress,
                "skipped_video_episode_progress_count": skipped_episode_progress,
                "covers_count": len(staged_names),
                "backup_path": str(backup_path) if backup_path else "",
                "package_path": str(package),
            }
        except Exception:
            if connection is not None and not committed:
                try:
                    connection.rollback()
                except Exception:
                    pass
            if not committed and final_cover_dir is not None:
                if moved_stage:
                    shutil.rmtree(str(final_cover_dir), ignore_errors=True)
                for filename in moved_cover_names:
                    (final_cover_dir / filename).unlink(missing_ok=True)
                if rollback_cover_dir is not None:
                    for filename in backed_up_cover_names:
                        backup_cover = rollback_cover_dir / filename
                        if backup_cover.exists():
                            os.replace(str(backup_cover), str(final_cover_dir / filename))
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

    def _import_audiobook_category(
        self,
        db_path,
        package_path,
        target_paths,
        name=None,
        merge_to=None,
        backup=True,
        inspection=None,
    ):
        inspection = inspection or self.inspect_package(package_path)
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
        self._progress("preflight", 0, 1, "오디오북 패키지와 대상 DB를 검사하고 있습니다.")
        connection = self._database_connection(db_path, "audiobook", readonly=False)
        try:
            self._check_required_sqlite_modules(connection)
            self._quick_check(connection, "audiobook")
        finally:
            connection.close()

        package = self._work_path(package_path, must_exist=True)
        free_space = shutil.disk_usage(str(self.cover_root)).free
        if inspection["cover_uncompressed_size"] > free_space:
            raise OSError("포스터 복원에 필요한 디스크 여유 공간이 부족합니다.")

        backup_path = None
        if backup:
            self._check_stop()
            self._progress("backup", 0, 1, "가져오기 전 오디오북 DB를 백업하고 있습니다.")
            backup_path = self._backup_database(db_path, "audiobook")
            self._progress("backup", 1, 1, "오디오북 DB 백업을 완료했습니다.")

        stage = Path(
            tempfile.mkdtemp(
                prefix=".bookoasis_mate_audio_import_",
                dir=str(self.cover_root),
            )
        )
        final_cover_dir = None
        connection = None
        created_cover_names = []
        created_cover_dir = False
        database_committed = False
        try:
            with zipfile.ZipFile(str(package), "r") as archive:
                metadata = self._read_json_member(archive, "metadata.json")
                library_meta = metadata["library"]
                audiobooks = metadata.get("audiobooks", [])
                tracks = metadata.get("audiobook_tracks", [])
                progress_rows = metadata.get("audiobook_progress", [])
                track_progress_rows = metadata.get("audiobook_track_progress", [])
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
                        raise ValueError(
                            f"중복되거나 잘못된 포스터 파일명입니다: {member.filename}"
                        )
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
                        "오디오북 포스터를 임시 디렉터리에 복원하고 있습니다.",
                    )

            self._check_stop()
            connection = self._database_connection(db_path, "audiobook", readonly=False)
            tables = self._tables(connection)
            required_tables = {"libraries", "audiobooks", "audiobook_tracks"}
            if not required_tables.issubset(tables):
                missing = ", ".join(sorted(required_tables - tables))
                raise RuntimeError(f"오디오북 필수 테이블이 없습니다: {missing}")
            library_columns = self._columns(connection, "libraries")
            audiobook_columns = self._columns(connection, "audiobooks")
            track_columns = self._columns(connection, "audiobook_tracks")
            if not {"name", "physical_path"}.issubset(library_columns):
                raise RuntimeError("libraries 필수 컬럼이 없습니다.")
            if not {"library_id", "title", "folder_path"}.issubset(audiobook_columns):
                raise RuntimeError("audiobooks 필수 컬럼이 없습니다.")
            if not {"audiobook_id", "track_number", "file_path"}.issubset(track_columns):
                raise RuntimeError("audiobook_tracks 필수 컬럼이 없습니다.")
            if progress_rows and "audiobook_progress" not in tables:
                raise RuntimeError("오디오북 진행률을 복원할 테이블이 없습니다.")
            if track_progress_rows and "audiobook_track_progress" not in tables:
                raise RuntimeError("오디오북 트랙 진행률을 복원할 테이블이 없습니다.")

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
                        f"병합 대상 오디오북 카테고리를 찾을 수 없습니다: {merge_value}"
                    )
                new_library_id = int(target_library["id"])
                target_name = str(target_library["name"])
                physical_paths = self._root_paths(target_library["physical_path"])
                for target in resolved_targets:
                    text = str(target)
                    if text not in physical_paths:
                        physical_paths.append(text)
                        added_physical_paths.append(text)
                connection.execute(
                    "UPDATE libraries SET physical_path = ? WHERE id = ?",
                    ("\n".join(physical_paths), new_library_id),
                )
            else:
                target_name = str(
                    name or library_meta.get("name") or "Imported Audiobooks"
                ).strip()
                if not target_name:
                    raise ValueError("신규 오디오북 카테고리 이름이 비어 있습니다.")
                if connection.execute(
                    "SELECT id FROM libraries WHERE name = ?",
                    (target_name,),
                ).fetchone() is not None:
                    base_name = f"{target_name} (Imported {datetime.now().strftime('%H%M%S')})"
                    target_name = base_name
                    suffix = 2
                    while connection.execute(
                        "SELECT id FROM libraries WHERE name = ?",
                        (target_name,),
                    ).fetchone() is not None:
                        target_name = f"{base_name}-{suffix}"
                        suffix += 1
                physical_paths = [str(target) for target in resolved_targets]
                added_physical_paths = list(physical_paths)
                library_values = {
                    "name": target_name,
                    "physical_path": "\n".join(physical_paths),
                    "cron_schedule": library_meta.get("cron_schedule"),
                    "scan_status": "ready",
                    "is_remote": library_meta.get("is_remote", 0),
                    "vfs_refresh_before_scan": library_meta.get(
                        "vfs_refresh_before_scan",
                        0,
                    ),
                    "rclone_rc_url": library_meta.get("rclone_rc_url"),
                    "icon": library_meta.get("icon", "fa-headphones"),
                    "color": library_meta.get("color", "#94a3b8"),
                    "hide_cover": library_meta.get("hide_cover", 0),
                }
                new_library_id = self._insert_row(
                    connection,
                    "libraries",
                    {
                        key: value
                        for key, value in library_values.items()
                        if key in library_columns
                    },
                )

            existing_audiobook_count = connection.execute(
                "SELECT COUNT(*) FROM audiobooks WHERE library_id = ?",
                (new_library_id,),
            ).fetchone()[0]
            existing_track_count = connection.execute(
                "SELECT COUNT(*) FROM audiobook_tracks t "
                "JOIN audiobooks a ON a.id = t.audiobook_id "
                "WHERE a.library_id = ?",
                (new_library_id,),
            ).fetchone()[0]
            final_cover_dir = self.cover_root / str(new_library_id)
            cover_name_map, created_cover_names, created_cover_dir = (
                self._install_staged_covers(
                    stage,
                    final_cover_dir,
                    staged_names,
                    "audiobook",
                )
            )
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            audiobook_index_to_id = {}
            audiobook_index_to_root = {}
            track_lookup = {}
            imported_audiobooks = 0
            skipped_duplicate_audiobooks = 0
            imported_tracks = 0
            skipped_duplicate_tracks = 0
            fallback_audiobooks = 0

            for audiobook_index, audiobook in enumerate(audiobooks):
                self._check_stop()
                relative_folder = self._safe_optional_relative_path(
                    audiobook.get("relative_folder_path")
                )
                root_index = int(audiobook.get("root_index") or 0)
                if root_index < 0 or root_index >= len(resolved_targets):
                    root_index = 0
                    fallback_audiobooks += 1
                target_root = resolved_targets[root_index]
                audiobook_index_to_root[audiobook_index] = target_root
                folder_path = target_root.joinpath(*relative_folder.parts)
                duplicate = connection.execute(
                    "SELECT id FROM audiobooks WHERE folder_path = ?",
                    (str(folder_path),),
                ).fetchone()
                if duplicate is not None:
                    new_audiobook_id = int(duplicate["id"])
                    skipped_duplicate_audiobooks += 1
                else:
                    poster = str(audiobook.get("poster") or "").strip()
                    if poster and not poster.startswith(("http://", "https://")):
                        poster_name = Path(poster).name
                        poster_name = cover_name_map.get(poster_name, poster_name)
                        poster = str(final_cover_dir / poster_name)
                    values = {
                        "library_id": new_library_id,
                        "title": audiobook.get("title") or "Unknown Audiobook",
                        "sort_title": audiobook.get("sort_title"),
                        "web_id": audiobook.get("web_id"),
                        "author": audiobook.get("author"),
                        "publisher": audiobook.get("publisher"),
                        "code": audiobook.get("code"),
                        "poster": poster,
                        "premiered": audiobook.get("premiered"),
                        "ratings": audiobook.get("ratings", 0.0),
                        "author_intro": audiobook.get("author_intro"),
                        "description": audiobook.get("description"),
                        "folder_name": audiobook.get("folder_name") or folder_path.name,
                        "folder_path": str(folder_path),
                        "total_duration": audiobook.get("total_duration", 0.0),
                        "total_tracks": audiobook.get("total_tracks", 0),
                        "file_type": audiobook.get("file_type", "multi"),
                        "is_favorite": audiobook.get("is_favorite", 0),
                        "created_at": audiobook.get("created_at") or now,
                        "updated_at": audiobook.get("updated_at") or now,
                        "is_deleted": audiobook.get("is_deleted", 0),
                        "deleted_at": audiobook.get("deleted_at"),
                    }
                    new_audiobook_id = self._insert_row(
                        connection,
                        "audiobooks",
                        {
                            key: value
                            for key, value in values.items()
                            if key in audiobook_columns and key in AUDIOBOOK_COLUMNS
                        },
                    )
                    imported_audiobooks += 1
                audiobook_index_to_id[audiobook_index] = int(new_audiobook_id)
                if audiobook_index % 100 == 0 or audiobook_index + 1 == len(audiobooks):
                    self._progress(
                        "import_audiobooks",
                        audiobook_index + 1,
                        len(audiobooks),
                        "오디오북 정보를 대상 카테고리에 저장하고 있습니다.",
                    )

            for track_index, track in enumerate(tracks):
                self._check_stop()
                audiobook_index = int(track.get("audiobook_export_index", -1))
                if audiobook_index not in audiobook_index_to_id:
                    continue
                new_audiobook_id = audiobook_index_to_id[audiobook_index]
                root_index = int(track.get("root_index") or 0)
                if root_index < 0 or root_index >= len(resolved_targets):
                    target_root = audiobook_index_to_root.get(
                        audiobook_index,
                        resolved_targets[0],
                    )
                else:
                    target_root = resolved_targets[root_index]
                relative_path = self._safe_relative_path(track.get("relative_path"))
                file_path = target_root.joinpath(*relative_path.parts)
                duplicate = connection.execute(
                    "SELECT id FROM audiobook_tracks WHERE file_path = ?",
                    (str(file_path),),
                ).fetchone()
                track_number = int(track.get("track_number") or 0)
                if duplicate is not None:
                    new_track_id = int(duplicate["id"])
                    skipped_duplicate_tracks += 1
                else:
                    values = {
                        "audiobook_id": new_audiobook_id,
                        "track_number": track_number,
                        "track_code": track.get("track_code"),
                        "title": track.get("title"),
                        "filename": track.get("filename") or file_path.name,
                        "file_path": str(file_path),
                        "file_mtime": track.get("file_mtime", 0.0),
                        "file_size": track.get("file_size", 0),
                        "duration": track.get("duration", 0.0),
                        "format": track.get("format", "mp3"),
                        "created_at": track.get("created_at") or now,
                    }
                    new_track_id = self._insert_row(
                        connection,
                        "audiobook_tracks",
                        {
                            key: value
                            for key, value in values.items()
                            if key in track_columns and key in AUDIOBOOK_TRACK_COLUMNS
                        },
                    )
                    imported_tracks += 1
                if track_number > 0:
                    track_lookup[(new_audiobook_id, track_number)] = int(new_track_id)
                if track_index % 200 == 0 or track_index + 1 == len(tracks):
                    self._progress(
                        "import_audiobook_tracks",
                        track_index + 1,
                        len(tracks),
                        "오디오북 트랙 정보를 저장하고 있습니다.",
                    )

            valid_users = set()
            if "users" in tables:
                valid_users = {
                    int(row["id"])
                    for row in connection.execute("SELECT id FROM users").fetchall()
                }
            imported_progress = 0
            skipped_progress = 0
            if "audiobook_progress" in tables:
                progress_columns = self._columns(connection, "audiobook_progress")
                for progress in progress_rows:
                    audiobook_index = int(progress.get("audiobook_export_index", -1))
                    user_id = int(progress.get("user_id") or 0)
                    if audiobook_index not in audiobook_index_to_id or user_id not in valid_users:
                        skipped_progress += 1
                        continue
                    new_audiobook_id = audiobook_index_to_id[audiobook_index]
                    track_number = int(progress.get("current_track_number") or 0)
                    values = {
                        key: value
                        for key, value in progress.items()
                        if key not in {
                            "id",
                            "audiobook_id",
                            "audiobook_export_index",
                            "current_track_id",
                            "current_track_number",
                        }
                        and key in progress_columns
                    }
                    values.update(
                        {
                            "audiobook_id": new_audiobook_id,
                            "user_id": user_id,
                            "current_track_id": track_lookup.get(
                                (new_audiobook_id, track_number)
                            ),
                        }
                    )
                    self._upsert_row(
                        connection,
                        "audiobook_progress",
                        ("audiobook_id", "user_id"),
                        values,
                    )
                    imported_progress += 1

            imported_track_progress = 0
            skipped_track_progress = 0
            if "audiobook_track_progress" in tables:
                track_progress_columns = self._columns(
                    connection,
                    "audiobook_track_progress",
                )
                for progress in track_progress_rows:
                    audiobook_index = int(progress.get("audiobook_export_index", -1))
                    user_id = int(progress.get("user_id") or 0)
                    if audiobook_index not in audiobook_index_to_id or user_id not in valid_users:
                        skipped_track_progress += 1
                        continue
                    new_audiobook_id = audiobook_index_to_id[audiobook_index]
                    track_number = int(progress.get("track_number") or 0)
                    new_track_id = track_lookup.get((new_audiobook_id, track_number))
                    if not new_track_id:
                        skipped_track_progress += 1
                        continue
                    values = {
                        key: value
                        for key, value in progress.items()
                        if key not in {
                            "id",
                            "audiobook_id",
                            "track_id",
                            "audiobook_export_index",
                            "track_number",
                        }
                        and key in track_progress_columns
                    }
                    values.update(
                        {
                            "audiobook_id": new_audiobook_id,
                            "track_id": new_track_id,
                            "user_id": user_id,
                        }
                    )
                    self._upsert_row(
                        connection,
                        "audiobook_track_progress",
                        ("audiobook_id", "track_id", "user_id"),
                        values,
                    )
                    imported_track_progress += 1

            verified_audiobooks = connection.execute(
                "SELECT COUNT(*) FROM audiobooks WHERE library_id = ?",
                (new_library_id,),
            ).fetchone()[0]
            verified_tracks = connection.execute(
                "SELECT COUNT(*) FROM audiobook_tracks t "
                "JOIN audiobooks a ON a.id = t.audiobook_id "
                "WHERE a.library_id = ?",
                (new_library_id,),
            ).fetchone()[0]
            if verified_audiobooks != existing_audiobook_count + imported_audiobooks:
                raise RuntimeError("가져온 오디오북 수 검증에 실패했습니다.")
            if verified_tracks != existing_track_count + imported_tracks:
                raise RuntimeError("가져온 오디오북 트랙 수 검증에 실패했습니다.")
            if not all(
                (final_cover_dir / filename).is_file()
                for filename in cover_name_map.values()
            ):
                raise RuntimeError("복원한 오디오북 포스터 수 검증에 실패했습니다.")

            connection.commit()
            database_committed = True
            self._progress("verify", 1, 1, "오디오북 가져오기 결과를 검증했습니다.")
            return {
                "mode": mode,
                "db_type": "audiobook",
                "media_kind": "audiobook",
                "library_id": new_library_id,
                "library_name": target_name,
                "physical_paths": physical_paths,
                "added_physical_paths": added_physical_paths,
                "source_books_count": 0,
                "books_count": 0,
                "source_audiobooks_count": len(audiobooks),
                "audiobooks_count": imported_audiobooks,
                "skipped_duplicate_audiobooks_count": skipped_duplicate_audiobooks,
                "fallback_audiobooks_count": fallback_audiobooks,
                "audiobook_tracks_count": imported_tracks,
                "skipped_duplicate_tracks_count": skipped_duplicate_tracks,
                "audiobook_progress_count": imported_progress,
                "skipped_audiobook_progress_count": skipped_progress,
                "audiobook_track_progress_count": imported_track_progress,
                "skipped_audiobook_track_progress_count": skipped_track_progress,
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
                if final_cover_dir is not None:
                    for filename in created_cover_names:
                        (final_cover_dir / filename).unlink(missing_ok=True)
                    if created_cover_dir and final_cover_dir.exists():
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
        package_is_audiobook = inspection.get("media_kind") == "audiobook"
        package_is_video = inspection.get("media_kind") == "video"
        if package_is_video != (selected_db_type == "video"):
            raise ValueError("패키지 미디어 유형과 대상 DB 유형이 일치하지 않습니다.")
        if package_is_audiobook != (selected_db_type == "audiobook"):
            raise ValueError("패키지 미디어 유형과 대상 DB 유형이 일치하지 않습니다.")
        if package_is_video:
            return self._import_video_category(
                db_path,
                package_path,
                target_paths,
                name=name,
                merge_to=merge_to,
                backup=backup,
                inspection=inspection,
            )
        if package_is_audiobook:
            return self._import_audiobook_category(
                db_path,
                package_path,
                target_paths,
                name=name,
                merge_to=merge_to,
                backup=backup,
                inspection=inspection,
            )
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
        created_cover_names = []
        created_cover_dir = False
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
            final_cover_dir = self.cover_root / str(new_library_id)
            cover_name_map, created_cover_names, created_cover_dir = (
                self._install_staged_covers(
                    stage,
                    final_cover_dir,
                    staged_names,
                    selected_db_type,
                )
            )
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
                        original_name = Path(cover_image).name
                        cover_image = (
                            f"{new_library_id}/"
                            f"{cover_name_map.get(original_name, original_name)}"
                        )
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
                for filename in cover_name_map.values()
            ):
                raise RuntimeError("복원한 표지 수 검증에 실패했습니다.")
            connection.commit()
            database_committed = True
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
                if final_cover_dir is not None:
                    for filename in created_cover_names:
                        (final_cover_dir / filename).unlink(missing_ok=True)
                    if created_cover_dir and final_cover_dir.exists():
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
