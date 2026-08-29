# Kavita DB의 메타데이터·표지·진행률을 기존 BookOasis 도서에 안전하게 적용합니다.
import hashlib
import os
import posixpath
import re
import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    from .migration_database import MigrationDatabaseContext
except ImportError:
    from migration_database import MigrationDatabaseContext


class KavitaMigrationStopped(RuntimeError):
    pass


BOOK_WRITE_BATCH_SIZE = 500
COVER_MAX_WORKERS = 4
DUPLICATE_DETAIL_LIMIT = 100


def normalize_media_path(value):
    return str(value or "").strip().replace("\\", "/").rstrip("/")


def parse_name_list(value):
    values = value if isinstance(value, (list, tuple)) else [value or ""]
    result = []
    for item in values:
        for part in str(item).replace("\r", "").split("\n"):
            text = part.strip()
            if text and text not in result:
                result.append(text)
    return result


def parse_mapping_lines(value):
    mappings = []
    for line in parse_name_list(value):
        if "=>" in line:
            source, target = line.split("=>", 1)
        elif "=" in line:
            source, target = line.split("=", 1)
        else:
            raise ValueError(f"경로·사용자 매핑 형식이 올바르지 않습니다: {line}")
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise ValueError(f"경로·사용자 매핑 값이 비어 있습니다: {line}")
        mappings.append((source, target))
    return mappings


def apply_path_mappings(path, mappings):
    original = normalize_media_path(path)
    normalized = [
        (normalize_media_path(source), normalize_media_path(target))
        for source, target in mappings
    ]
    for source, target in sorted(normalized, key=lambda item: len(item[0]), reverse=True):
        if original == source:
            return target
        if original.startswith(source + "/"):
            return target + original[len(source):]
    return original


class KavitaMigrationEngine:
    def __init__(
        self,
        kavita_db_path,
        kavita_cover_root,
        bookoasis_db_path,
        bookoasis_cover_root,
        work_dir,
        should_stop=None,
        on_progress=None,
        database_settings=None,
        target_db_type="general",
        database_context=None,
    ):
        self.kavita_db_path = self._required_file(kavita_db_path, "Kavita DB 파일")
        self.kavita_cover_root = self._optional_directory(
            kavita_cover_root, "Kavita 표지 디렉터리"
        )
        self.target_db_type = str(target_db_type or "general").strip().lower()
        self.database_context = database_context
        if self.database_context is None and database_settings:
            context_settings = dict(database_settings)
            context_settings.setdefault(
                f"{self.target_db_type}_db_path",
                bookoasis_db_path,
            )
            self.database_context = MigrationDatabaseContext(
                context_settings,
                work_dir=work_dir,
                should_stop=should_stop,
                on_progress=on_progress,
            )
        if self.database_context is not None and self.database_context.engine == "mariadb":
            self.bookoasis_db_path = None
        else:
            self.bookoasis_db_path = self._required_file(
                bookoasis_db_path, "BookOasis DB 파일"
            )
        self.bookoasis_cover_root = self._optional_directory(
            bookoasis_cover_root, "BookOasis 표지 디렉터리"
        )
        self.work_dir = self._required_directory(work_dir, "이관 작업 디렉터리", True)
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress

    @staticmethod
    def _required_file(value, label):
        path = Path(str(value or "").strip()).expanduser()
        if not str(value or "").strip() or not path.is_file():
            raise FileNotFoundError(f"{label}을 찾을 수 없습니다.")
        return path.resolve()

    @staticmethod
    def _optional_directory(value, label):
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"{label}를 찾을 수 없습니다.")
        return path.resolve()

    @staticmethod
    def _required_directory(value, label, create=False):
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

    def _target_connect(self, readonly=False):
        if self.database_context is not None:
            return self.database_context.connect(
                self.target_db_type,
                readonly=readonly,
            )
        return self._connect(self.bookoasis_db_path, readonly=readonly)

    @staticmethod
    def _tables(connection):
        if hasattr(connection, "tables"):
            return connection.tables()
        return {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    @staticmethod
    def _columns(connection, table):
        if hasattr(connection, "columns"):
            return connection.columns(table)
        safe = str(table).replace('"', '""')
        return {
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{safe}")').fetchall()
        }

    @staticmethod
    def _iter_cursor(cursor, batch_size=1000):
        while True:
            rows = cursor.fetchmany(max(1, int(batch_size)))
            if not rows:
                return
            yield from rows

    @staticmethod
    def _matching_name(values, *candidates):
        names = {str(value).casefold(): str(value) for value in values}
        for candidate in candidates:
            matched = names.get(str(candidate).casefold())
            if matched:
                return matched
        return None

    def _check_stop(self):
        if self.should_stop():
            raise KavitaMigrationStopped("사용자가 Kavita 이관 중지를 요청했습니다.")

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

    @staticmethod
    def _people_subquery(tables, role, alias):
        if {"SeriesMetadataPeople", "Person", "SeriesMetadata"}.issubset(tables):
            return f"""
                (SELECT GROUP_CONCAT(pe.Name, ', ')
                 FROM SeriesMetadataPeople smp
                 JOIN Person pe ON smp.PersonId = pe.Id
                 JOIN SeriesMetadata sm ON smp.SeriesMetadataId = sm.Id
                 WHERE sm.SeriesId = s.Id AND smp.Role = {int(role)}) AS {alias}
            """
        if {"PersonSeriesMetadata", "Person", "SeriesMetadata"}.issubset(tables):
            return f"""
                (SELECT GROUP_CONCAT(pe.Name, ', ')
                 FROM PersonSeriesMetadata psm
                 JOIN Person pe ON psm.PeopleId = pe.Id
                 JOIN SeriesMetadata sm ON psm.SeriesMetadatasId = sm.Id
                 WHERE sm.SeriesId = s.Id AND pe.Role = {int(role)}) AS {alias}
            """
        return f"NULL AS {alias}"

    @staticmethod
    def _clean_title(value):
        text = str(value or "").strip()
        try:
            if text and float(text) <= -100000:
                return ""
        except ValueError:
            pass
        return text

    @classmethod
    def _book_title(cls, row):
        volume = cls._clean_title(row["VolumeName"])
        chapter = cls._clean_title(row["ChapterTitle"])
        file_title = os.path.splitext(os.path.basename(row["FilePath"] or ""))[0].strip()
        file_title = re.sub(r"#\d+$", "", file_title).strip()
        file_title = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", file_title).strip()
        if chapter:
            return f"{volume} - {chapter}" if volume and volume != chapter else chapter
        return file_title or volume or str(row["SeriesName"] or "").strip()

    def _read_source(self, selected_libraries=None):
        selected = set(parse_name_list(selected_libraries))
        connection = self._connect(self.kavita_db_path, readonly=True)
        try:
            tables = self._tables(connection)
            required = {"Library", "Series", "Volume", "Chapter", "MangaFile"}
            if not required.issubset(tables):
                missing = ", ".join(sorted(required - tables))
                raise RuntimeError(f"Kavita 필수 테이블이 없습니다: {missing}")

            authors = self._people_subquery(tables, 3, "Authors")
            publisher = self._people_subquery(tables, 10, "Publisher")
            release = (
                "(SELECT CASE WHEN sm.ReleaseYear > 0 THEN sm.ReleaseYear || '-01-01' ELSE NULL END "
                "FROM SeriesMetadata sm WHERE sm.SeriesId=s.Id LIMIT 1) AS ReleaseDate"
                if "SeriesMetadata" in tables
                else "NULL AS ReleaseDate"
            )
            summary = (
                "(SELECT sm.Summary FROM SeriesMetadata sm "
                "WHERE sm.SeriesId=s.Id LIMIT 1) AS Summary"
                if "SeriesMetadata" in tables
                else "NULL AS Summary"
            )
            genres = (
                "(SELECT GROUP_CONCAT(g.Title, ', ') FROM GenreSeriesMetadata gsm "
                "JOIN Genre g ON gsm.GenresId=g.Id "
                "JOIN SeriesMetadata sm ON gsm.SeriesMetadatasId=sm.Id "
                "WHERE sm.SeriesId=s.Id) AS Genres"
                if {"GenreSeriesMetadata", "Genre", "SeriesMetadata"}.issubset(tables)
                else "NULL AS Genres"
            )
            tags = (
                "(SELECT GROUP_CONCAT(t.Title, ', ') FROM SeriesMetadataTag smt "
                "JOIN Tag t ON smt.TagsId=t.Id "
                "JOIN SeriesMetadata sm ON smt.SeriesMetadatasId=sm.Id "
                "WHERE sm.SeriesId=s.Id) AS Tags"
                if {"SeriesMetadataTag", "Tag", "SeriesMetadata"}.issubset(tables)
                else "NULL AS Tags"
            )
            rows = self._iter_cursor(connection.execute(
                f"""
                SELECT l.Name AS LibraryName, s.Name AS SeriesName,
                       v.Name AS VolumeName, c.Title AS ChapterTitle,
                       m.FilePath, m.Pages AS TotalPages,
                       c.CoverImage AS ChapterCover,
                       v.CoverImage AS VolumeCover,
                       s.CoverImage AS SeriesCover,
                       {authors}, {publisher}, {release}, {summary}, {genres}, {tags}
                FROM MangaFile m
                JOIN Chapter c ON m.ChapterId=c.Id
                JOIN Volume v ON c.VolumeId=v.Id
                JOIN Series s ON v.SeriesId=s.Id
                JOIN Library l ON s.LibraryId=l.Id
                ORDER BY l.Id, s.Id, v.Id, c.Id, m.Id
                """
            ))
            total_rows = int(connection.execute(
                "SELECT COUNT(*) FROM MangaFile"
            ).fetchone()[0])
            self._progress(
                "source_read",
                0,
                total_rows,
                "Kavita 도서 정보를 배치로 읽고 있습니다.",
            )

            books = []
            source_paths = set()
            for row_index, row in enumerate(rows, 1):
                if row_index % 1000 == 0:
                    self._check_stop()
                    self._progress(
                        "source_read",
                        row_index,
                        total_rows,
                        (
                            f"Kavita 도서 정보 {row_index}/"
                            f"{total_rows}건을 읽었습니다."
                        ),
                    )
                library_name = str(row["LibraryName"] or "").strip()
                if selected and library_name not in selected:
                    continue
                source_path = normalize_media_path(row["FilePath"])
                if not source_path:
                    continue
                source_paths.add(source_path)
                books.append(
                    {
                        "source_order": row_index,
                        "library_name": library_name,
                        "series_name": str(row["SeriesName"] or "").strip(),
                        "title": self._book_title(row),
                        "file_path": source_path,
                        "total_pages": int(row["TotalPages"] or 0),
                        "author": str(row["Authors"] or "").strip(),
                        "publisher": str(row["Publisher"] or "").strip(),
                        "release_date": str(row["ReleaseDate"] or "").strip(),
                        "summary": str(row["Summary"] or "").strip(),
                        "genre": str(row["Genres"] or "").strip(),
                        "tags": str(row["Tags"] or "").strip(),
                        "covers": [
                            value
                            for value in (
                                row["ChapterCover"],
                                row["VolumeCover"],
                                row["SeriesCover"],
                            )
                            if value
                        ],
                    }
                )
            if total_rows:
                self._progress(
                    "source_read",
                    total_rows,
                    total_rows,
                    f"Kavita 도서 정보 {total_rows}건을 읽었습니다.",
                )

            users = {}
            user_table = self._matching_name(
                tables,
                "AspNetUsers",
                "AppUser",
            )
            if user_table:
                columns = self._columns(connection, user_table)
                id_column = self._matching_name(columns, "Id")
                username_column = self._matching_name(
                    columns,
                    "UserName",
                    "Username",
                    "Name",
                )
                if id_column and username_column:
                    safe_table = user_table.replace('"', '""')
                    safe_id = id_column.replace('"', '""')
                    safe_username = username_column.replace('"', '""')
                    for row in self._iter_cursor(connection.execute(
                        f'SELECT "{safe_id}" AS Id, '
                        f'"{safe_username}" AS Username '
                        f'FROM "{safe_table}"'
                    )):
                        username = str(row["Username"] or "").strip()
                        if username:
                            users[int(row["Id"])] = username

            progress = []
            progress_table = self._matching_name(
                tables,
                "AppUserProgresses",
            )
            if progress_table:
                progress_columns = self._columns(connection, progress_table)
                user_column = self._matching_name(
                    progress_columns,
                    "AppUserId",
                    "UserId",
                )
                last_modified_column = self._matching_name(
                    progress_columns,
                    "LastModifiedUtc",
                    "LastModified",
                )
                if user_column:
                    safe_progress_table = progress_table.replace('"', '""')
                    safe_user_column = user_column.replace('"', '""')
                    if last_modified_column:
                        safe_last_modified = last_modified_column.replace(
                            '"',
                            '""',
                        )
                        last_modified_sql = f'p."{safe_last_modified}"'
                    else:
                        last_modified_sql = "''"
                    for row in self._iter_cursor(connection.execute(
                        f"""
                        SELECT m.FilePath,
                               p."{safe_user_column}" AS UserId,
                               p.PagesRead,
                               {last_modified_sql} AS LastModified
                        FROM "{safe_progress_table}" p
                        JOIN Chapter c ON p.ChapterId=c.Id
                        JOIN MangaFile m ON m.ChapterId=c.Id
                        WHERE COALESCE(p.PagesRead, 0) > 0
                        """
                    )):
                        source_path = normalize_media_path(row["FilePath"])
                        if source_path not in source_paths:
                            continue
                        progress.append(
                            {
                                "file_path": source_path,
                                "source_user": users.get(
                                    int(row["UserId"]), f"ID {row['UserId']}"
                                ),
                                "pages_read": int(row["PagesRead"] or 0),
                                "last_read_at": str(row["LastModified"] or "").strip(),
                            }
                        )

            library_paths = {}
            if "FolderPath" in tables:
                for row in self._iter_cursor(connection.execute(
                    """
                    SELECT l.Name AS LibraryName, fp.Path AS FolderPath
                    FROM Library l JOIN FolderPath fp ON fp.LibraryId=l.Id
                    WHERE COALESCE(fp.Path, '') != ''
                    ORDER BY l.Id, fp.Id
                    """
                )):
                    name = str(row["LibraryName"] or "").strip()
                    if selected and name not in selected:
                        continue
                    library_paths.setdefault(name, [])
                    path = normalize_media_path(row["FolderPath"])
                    if path and path not in library_paths[name]:
                        library_paths[name].append(path)
            return books, progress, users, library_paths
        finally:
            connection.close()

    def _target_snapshot(self):
        connection = self._target_connect(readonly=True)
        try:
            tables = self._tables(connection)
            if not {"books", "libraries", "users"}.issubset(tables):
                raise RuntimeError("BookOasis books, libraries 또는 users 테이블이 없습니다.")
            books = {}
            for row in self._iter_cursor(connection.execute(
                """
                SELECT b.id, b.file_path, b.cover_image, b.total_pages,
                       b.library_id, l.name AS library_name
                FROM books b LEFT JOIN libraries l ON l.id=b.library_id
                WHERE COALESCE(b.is_deleted, 0)=0
                """
            )):
                books.setdefault(normalize_media_path(row["file_path"]), dict(row))
            users = {
                str(row["username"]): int(row["id"])
                for row in connection.execute(
                    "SELECT id, username FROM users ORDER BY id"
                ).fetchall()
            }
            library_paths = []
            if "physical_path" in self._columns(connection, "libraries"):
                for row in self._iter_cursor(connection.execute(
                    """
                    SELECT physical_path FROM libraries
                    WHERE COALESCE(physical_path, '') != ''
                    ORDER BY id
                    """
                )):
                    for value in str(row["physical_path"] or "").splitlines():
                        path = normalize_media_path(value)
                        if path and path not in library_paths:
                            library_paths.append(path)
            return books, users, library_paths
        finally:
            connection.close()

    @staticmethod
    def _mapped_library_paths(books, library_paths, mappings):
        mapped = {}
        books_by_library = {}
        for book in books:
            books_by_library.setdefault(book["library_name"], []).append(book)
        for library_name in books_by_library:
            roots = [
                apply_path_mappings(path, mappings)
                for path in library_paths.get(library_name, [])
            ]
            roots = list(dict.fromkeys(path for path in roots if path))
            if not roots:
                paths = [
                    apply_path_mappings(book["file_path"], mappings)
                    for book in books_by_library[library_name]
                ]
                try:
                    common = normalize_media_path(posixpath.commonpath(paths))
                except (TypeError, ValueError):
                    common = ""
                if common in paths:
                    common = normalize_media_path(posixpath.dirname(common))
                if common:
                    roots = [common]
            mapped[library_name] = roots
        return mapped

    @staticmethod
    def _path_contains(root, path):
        root = normalize_media_path(root)
        path = normalize_media_path(path)
        return bool(root) and (path == root or path.startswith(root + "/"))

    @staticmethod
    def _path_depth(path):
        return len([part for part in normalize_media_path(path).split("/") if part])

    @classmethod
    def _resolve_migration_books(
        cls,
        books,
        library_paths,
        mappings,
        target_books,
    ):
        mapped_library_paths = cls._mapped_library_paths(
            books,
            library_paths,
            mappings,
        )
        groups = {}
        for fallback_order, book in enumerate(books, 1):
            mapped_path = apply_path_mappings(book["file_path"], mappings)
            if mapped_path in {"", ".", "/"} or not posixpath.basename(mapped_path):
                raise ValueError(
                    "Kavita 도서의 최종 경로를 처리할 수 없습니다. "
                    f"보관함: {book.get('library_name') or '-'} · "
                    f"원본: {book.get('file_path') or '-'} · "
                    f"최종 경로: {mapped_path or '-'}"
                )
            groups.setdefault(mapped_path, []).append(
                (book, int(book.get("source_order") or fallback_order))
            )

        migration_books = []
        duplicate_details = []
        duplicate_groups = 0
        duplicate_source_rows = 0
        merged_extra_sources = 0
        ambiguous_groups = 0
        for mapped_path, grouped_sources in groups.items():
            sources = []
            for book, source_order in grouped_sources:
                library_name = str(book.get("library_name") or "").strip()
                roots = [
                    root
                    for root in mapped_library_paths.get(library_name, [])
                    if cls._path_contains(root, mapped_path)
                ]
                if not library_name or not roots:
                    raise ValueError(
                        "Kavita 도서의 최종 보관함을 결정할 수 없습니다. "
                        f"보관함: {library_name or '-'} · "
                        f"원본: {book.get('file_path') or '-'} · "
                        f"최종 경로: {mapped_path}"
                    )
                best_root = max(
                    roots,
                    key=lambda root: (cls._path_depth(root), len(root)),
                )
                sources.append(
                    {
                        "book": book,
                        "source_order": source_order,
                        "library_name": library_name,
                        "source_path": book["file_path"],
                        "mapped_library_roots": roots,
                        "best_root": best_root,
                    }
                )
            ranked = sorted(
                sources,
                key=lambda source: (
                    -cls._path_depth(source["best_root"]),
                    -len(source["best_root"]),
                    source["source_order"],
                ),
            )
            selected = ranked[0]
            ambiguous = any(
                source["library_name"] != selected["library_name"]
                and source["best_root"] == selected["best_root"]
                for source in ranked[1:]
            )
            canonical = min(
                (
                    source
                    for source in sources
                    if source["library_name"] == selected["library_name"]
                ),
                key=lambda source: source["source_order"],
            )
            migration_books.append(
                (canonical["book"], mapped_path, target_books.get(mapped_path))
            )
            if len(sources) == 1:
                continue
            duplicate_groups += 1
            duplicate_source_rows += len(sources)
            merged_extra_sources += len(sources) - 1
            ambiguous_groups += int(ambiguous)
            if len(duplicate_details) < DUPLICATE_DETAIL_LIMIT:
                duplicate_details.append(
                    {
                        "mapped_path": mapped_path,
                        "source_count": len(sources),
                        "selected_library": selected["library_name"],
                        "selection_reason": (
                            "stable_source_order"
                            if ambiguous
                            else "most_specific_library_root"
                        ),
                        "ambiguous": ambiguous,
                        "sources": [
                            {
                                "library_name": source["library_name"],
                                "source_path": source["source_path"],
                                "mapped_library_roots": source[
                                    "mapped_library_roots"
                                ],
                            }
                            for source in sorted(
                                sources,
                                key=lambda source: source["source_order"],
                            )
                        ],
                    }
                )

        return migration_books, {
            "source_books_count": len(books),
            "unique_mapped_books_count": len(migration_books),
            "duplicate_path_groups_count": duplicate_groups,
            "duplicate_source_rows_count": duplicate_source_rows,
            "merged_extra_sources_count": merged_extra_sources,
            "auto_merged_books_count": duplicate_groups,
            "ambiguous_library_groups_count": ambiguous_groups,
            "duplicate_path_groups": duplicate_details,
            "duplicate_path_groups_truncated": (
                duplicate_groups > len(duplicate_details)
            ),
        }

    @staticmethod
    def _resolve_mapped_progress(
        progress,
        mappings,
        mapped_paths,
        user_rules,
        target_users,
    ):
        resolved = {}
        for item in progress:
            target_username = user_rules.get(item["source_user"])
            if not target_username:
                continue
            mapped_path = apply_path_mappings(item["file_path"], mappings)
            if mapped_path not in mapped_paths:
                continue
            target_user_id = target_users[target_username]
            key = (mapped_path, target_user_id)
            rank = (
                int(item.get("pages_read") or 0),
                str(item.get("last_read_at") or ""),
            )
            previous = resolved.get(key)
            if previous is None or rank > previous[0]:
                resolved[key] = (rank, item)
        return [
            (item, mapped_path, target_user_id)
            for (mapped_path, target_user_id), (_, item) in resolved.items()
        ]

    @staticmethod
    def _suggest_path_mappings(
        books,
        library_paths,
        target_books,
        target_library_paths=None,
    ):
        source_library_paths = [
            path
            for paths in library_paths.values()
            for path in paths
        ]

        def gdrive_parents(paths):
            parents = set()
            for path in paths:
                normalized = normalize_media_path(path)
                index = normalized.casefold().find("/gdrive")
                if index >= 0:
                    parents.add(normalized[:index] or "/")
            return parents

        source_gdrive_parents = gdrive_parents(source_library_paths)
        target_gdrive_parents = gdrive_parents(target_library_paths or [])
        if (
            len(source_gdrive_parents) == 1
            and len(target_gdrive_parents) == 1
        ):
            source_parent = next(iter(source_gdrive_parents))
            target_parent = next(iter(target_gdrive_parents))
            if source_parent != target_parent:
                return [(source_parent, target_parent)]

        target_by_name = {}
        for target_path in target_books:
            name = target_path.rsplit("/", 1)[-1].casefold()
            target_by_name.setdefault(name, []).append(target_path)

        books_by_library = {}
        for book in books:
            books_by_library.setdefault(book["library_name"], []).append(book)

        target_counts_by_source = {}
        for library_name in sorted(library_paths):
            library_books = books_by_library.get(library_name, [])
            for source_root in library_paths[library_name]:
                source_root = normalize_media_path(source_root)
                target_counts = target_counts_by_source.setdefault(
                    source_root,
                    {},
                )
                for book in library_books:
                    source_path = normalize_media_path(book["file_path"])
                    source_prefix = (
                        source_root
                        if source_root.endswith("/")
                        else source_root + "/"
                    )
                    if not source_path.startswith(source_prefix):
                        continue
                    relative = source_path[len(source_prefix):]
                    suffix = "/" + relative
                    candidates = target_by_name.get(
                        relative.rsplit("/", 1)[-1].casefold(),
                        [],
                    )
                    for target_path in candidates:
                        if not target_path.casefold().endswith(suffix.casefold()):
                            continue
                        target_root = normalize_media_path(
                            target_path[:-len(suffix)]
                        )
                        if target_root:
                            target_counts[target_root] = (
                                target_counts.get(target_root, 0) + 1
                            )

        suggestions = []
        for source_root in sorted(target_counts_by_source):
            target_counts = target_counts_by_source[source_root]
            ranked = sorted(
                target_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if not ranked:
                continue
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                continue
            target_root = ranked[0][0]
            if source_root != target_root:
                suggestions.append((source_root, target_root))
        gdrive_parents = set()
        for source_root, target_root in suggestions:
            source_index = source_root.casefold().find("/gdrive")
            target_index = target_root.casefold().find("/gdrive")
            if source_index < 0 or target_index < 0:
                gdrive_parents.clear()
                break
            gdrive_parents.add(
                (
                    source_root[:source_index] or "/",
                    target_root[:target_index] or "/",
                )
            )
        if suggestions and len(gdrive_parents) == 1:
            source_parent, target_parent = next(iter(gdrive_parents))
            if source_parent != target_parent:
                return [(source_parent, target_parent)]
        return suggestions

    def inspect(self, path_mappings=None, selected_libraries=None):
        configured_mappings = [
            (source, target)
            for source, target in parse_mapping_lines(path_mappings)
            if normalize_media_path(source) != normalize_media_path(target)
        ]
        books, progress, users, library_paths = self._read_source(selected_libraries)
        targets, target_users, target_library_paths = self._target_snapshot()
        suggested_mappings = self._suggest_path_mappings(
            books,
            library_paths,
            targets,
            target_library_paths,
        )
        mappings = configured_mappings or suggested_mappings
        migration_books, duplicate_analysis = self._resolve_migration_books(
            books,
            library_paths,
            mappings,
            targets,
        )
        libraries = {}
        matched = 0
        cover_candidates = 0
        for book, mapped, target in migration_books:
            is_matched = target is not None
            matched += int(is_matched)
            cover_candidates += int(bool(book["covers"]) and is_matched)
            item = libraries.setdefault(
                book["library_name"],
                {
                    "name": book["library_name"],
                    "books": 0,
                    "matched": 0,
                    "new": 0,
                    "unmatched": 0,
                    "source_paths": library_paths.get(book["library_name"], []),
                },
            )
            item["books"] += 1
            item["matched" if is_matched else "new"] += 1
        source_users = sorted(set(users.values()))
        target_usernames = sorted(target_users.keys())
        targets_by_casefold = {
            username.casefold(): username for username in target_usernames
        }
        suggested_user_mappings = [
            (source_user, targets_by_casefold[source_user.casefold()])
            for source_user in source_users
            if source_user.casefold() in targets_by_casefold
        ]
        return {
            "libraries": sorted(libraries.values(), key=lambda item: item["name"]),
            "books_count": len(migration_books),
            "target_books_count": len(targets),
            "matched_books_count": matched,
            "new_books_count": len(migration_books) - matched,
            "unmatched_books_count": 0,
            "cover_candidates_count": cover_candidates,
            "progress_count": len(progress),
            "source_users": source_users,
            "target_users": target_usernames,
            "suggested_path_mappings": [
                f"{source} => {target}"
                for source, target in suggested_mappings
            ],
            "effective_path_mappings": [
                f"{source} => {target}"
                for source, target in mappings
            ],
            "path_mappings_auto_applied": bool(
                suggested_mappings and not configured_mappings
            ),
            "suggested_user_mappings": [
                f"{source} => {target}"
                for source, target in suggested_user_mappings
            ],
            "source_paths": sorted({
                path
                for paths in library_paths.values()
                for path in paths
            }),
            "target_paths": target_library_paths,
            **duplicate_analysis,
        }

    def _backup_database(self):
        if self.database_context is not None:
            return self.database_context.backup(
                self.target_db_type,
                reason="before_kavita",
            )
        backup_dir = self.work_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{self.bookoasis_db_path.stem}_before_kavita_{timestamp}.db"
        source = self._connect(self.bookoasis_db_path, readonly=True)
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return target

    def _cover_source(self, candidates):
        if self.kavita_cover_root is None:
            return None
        for name in candidates:
            candidate = (self.kavita_cover_root / str(name)).resolve()
            try:
                candidate.relative_to(self.kavita_cover_root)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _convert_cover(source, target):
        try:
            from PIL import Image
        except ImportError as error:
            raise RuntimeError(
                "Kavita 표지 이관에는 FlaskFarm의 Pillow 패키지가 필요합니다."
            ) from error

        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(str(source)) as image:
            image.save(str(target), "WEBP", quality=80)

    def _convert_cover_job(self, job):
        source, target = job
        self._convert_cover(source, target)

    def _stage_covers(
        self,
        migration_books,
        staging,
        max_workers=None,
        progress_offset=0,
        progress_total=None,
    ):
        total = len(migration_books)
        if total == 0:
            return 0
        progress_total = int(progress_total or total)
        worker_count = min(
            COVER_MAX_WORKERS,
            max(1, int(max_workers or os.cpu_count() or 1)),
        )
        converted_root = staging / "_converted"
        converted_root.mkdir(parents=True, exist_ok=True)
        job_limit = worker_count * 4
        jobs = []
        converted = 0

        def flush_jobs(executor):
            nonlocal converted
            if not jobs:
                return
            for _ in executor.map(self._convert_cover_job, jobs):
                converted += 1
            jobs.clear()

        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="kavita-cover",
        ) as executor:
            for index, (book, mapped_path, _) in enumerate(
                migration_books,
                1,
            ):
                self._check_stop()
                source = self._cover_source(book["covers"])
                if source is not None:
                    digest = hashlib.md5(
                        mapped_path.encode("utf-8")
                    ).hexdigest()
                    jobs.append(
                        (
                            source,
                            converted_root / f"book_{digest}.webp",
                        )
                    )
                if len(jobs) >= job_limit:
                    flush_jobs(executor)
                if index % BOOK_WRITE_BATCH_SIZE == 0 or index == total:
                    flush_jobs(executor)
                    self._progress(
                        "covers",
                        int(progress_offset) + index,
                        progress_total,
                        (
                            f"Kavita 표지 {converted}개를 변환했습니다. "
                            f"({index}/{total}권 확인)"
                        ),
                    )
        return converted

    @staticmethod
    def _execute_statement_batches(connection, statement_batches):
        for statement, parameters in statement_batches.items():
            if parameters:
                connection.executemany(statement, parameters)

    def migrate(
        self,
        path_mappings=None,
        user_mappings=None,
        selected_libraries=None,
        import_covers=True,
        import_progress=True,
        lock_metadata=True,
        backup=True,
        dry_run=True,
    ):
        path_rules = parse_mapping_lines(path_mappings)
        user_rules = dict(parse_mapping_lines(user_mappings))
        books, progress, _, library_paths = self._read_source(selected_libraries)
        target_books, target_users, _ = self._target_snapshot()
        invalid_users = sorted(set(user_rules.values()) - set(target_users))
        if import_progress and invalid_users:
            raise ValueError(
                "BookOasis에 없는 사용자입니다: " + ", ".join(invalid_users)
            )

        migration_books, duplicate_analysis = self._resolve_migration_books(
            books,
            library_paths,
            path_rules,
            target_books,
        )
        mapped_paths = {mapped_path for _, mapped_path, _ in migration_books}

        mapped_progress = (
            self._resolve_mapped_progress(
                progress,
                path_rules,
                mapped_paths,
                user_rules,
                target_users,
            )
            if import_progress
            else []
        )

        existing_count = sum(1 for _, _, target in migration_books if target)
        new_count = len(migration_books) - existing_count
        cover_candidates = (
            sum(
                1
                for book, _, _ in migration_books
                if self._cover_source(book["covers"])
            )
            if import_covers and dry_run
            else 0
        )
        preview = {
            "dry_run": bool(dry_run),
            "source_books_count": len(books),
            "target_books_count": len(target_books),
            "matched_books_count": existing_count,
            "new_books_count": new_count,
            "unmatched_books_count": 0,
            "progress_count": len(mapped_progress),
            "cover_candidates_count": cover_candidates,
            "unmatched": [],
            "unmatched_truncated": False,
            "backup_path": "",
            **duplicate_analysis,
        }
        if duplicate_analysis["duplicate_path_groups_count"]:
            self._progress(
                "prepare",
                0,
                len(migration_books),
                (
                    "Kavita 동일 경로 중복을 자동 병합했습니다. "
                    f"원본 {len(books)}건 · 고유 도서 {len(migration_books)}권 · "
                    "중복 그룹 "
                    f"{duplicate_analysis['duplicate_path_groups_count']}건 · "
                    "보관함 선택 경고 "
                    f"{duplicate_analysis['ambiguous_library_groups_count']}건"
                ),
            )
        if dry_run:
            return preview

        if import_covers and self.bookoasis_cover_root is None:
            raise ValueError("BookOasis 표지 디렉터리가 설정되지 않았습니다.")
        backup_path = self._backup_database() if backup else None
        staging = Path(tempfile.mkdtemp(prefix="bookoasis_mate_kavita_stage_"))
        rollback = Path(tempfile.mkdtemp(prefix="bookoasis_mate_kavita_rollback_"))
        applied_files = []
        connection = None
        converted_covers = 0
        created_books = 0
        updated_books = 0
        try:
            self._progress(
                "prepare",
                0,
                len(migration_books),
                "Kavita 데이터를 준비하고 있습니다.",
            )
            schema_connection = self._target_connect(readonly=True)
            try:
                columns = self._columns(schema_connection, "books")
                library_columns = self._columns(
                    schema_connection,
                    "libraries",
                )
            finally:
                schema_connection.close()
            if import_covers and "cover_image" in columns:
                cover_progress_units = len(migration_books)
            else:
                cover_progress_units = 0
            overall_progress_total = (
                cover_progress_units
                + len(migration_books)
                + (len(mapped_progress) if import_progress else 0)
            )
            if cover_progress_units:
                converted_covers = self._stage_covers(
                    migration_books,
                    staging,
                    progress_total=overall_progress_total,
                )
            preview["cover_candidates_count"] = converted_covers

            connection = self._target_connect(readonly=False)
            if hasattr(connection, "begin"):
                connection.begin(immediate=True)
            else:
                connection.execute("BEGIN IMMEDIATE")
            mapped_library_paths = self._mapped_library_paths(
                books,
                library_paths,
                path_rules,
            )
            library_cache = {
                str(row["name"]): int(row["id"])
                for row in connection.execute(
                    "SELECT id, name FROM libraries"
                ).fetchall()
            }
            for library_name, paths in mapped_library_paths.items():
                physical_path = "\n".join(paths)
                library_id = library_cache.get(library_name)
                if library_id is None:
                    insert_cursor = connection.execute(
                        "INSERT INTO libraries (name, physical_path) VALUES (?, ?)",
                        (library_name, physical_path),
                    )
                    library_id = int(insert_cursor.lastrowid)
                    library_cache[library_name] = library_id
                elif "physical_path" in library_columns:
                    connection.execute(
                        "UPDATE libraries SET physical_path=? WHERE id=?",
                        (physical_path, library_id),
                    )

            metadata_fields = [
                field
                for field in (
                    "title",
                    "series_name",
                    "author",
                    "publisher",
                    "summary",
                    "release_date",
                    "genre",
                    "tags",
                )
                if field in columns
            ]
            resolved_targets = {}
            for batch_start in range(
                0,
                len(migration_books),
                BOOK_WRITE_BATCH_SIZE,
            ):
                self._check_stop()
                batch = migration_books[
                    batch_start:batch_start + BOOK_WRITE_BATCH_SIZE
                ]
                statement_batches = {}
                new_targets = {}
                for book, mapped_path, target in batch:
                    library_id = library_cache[book["library_name"]]
                    total_pages = int(book["total_pages"] or 0)
                    file_format = (
                        posixpath.splitext(mapped_path)[1]
                        .lstrip(".")
                        .lower()
                        or "zip"
                    )
                    active_fields = [
                        field
                        for field in metadata_fields
                        if field in {"title", "series_name"} or book.get(field)
                    ]
                    values = [
                        (
                            book[field]
                            if book[field]
                            else (
                                "Kavita Author"
                                if field == "author"
                                else "Kavita Publisher"
                            )
                        )
                        for field in active_fields
                    ]
                    assignments = [
                        f'`{field}`=?' for field in active_fields
                    ]
                    for field, value in (
                        ("library_id", library_id),
                        ("file_format", file_format),
                        ("total_pages", total_pages),
                    ):
                        if field in columns:
                            assignments.append(f'`{field}`=?')
                            values.append(value)
                    if lock_metadata and "metadata_locked" in columns:
                        assignments.append("metadata_locked=1")

                    digest = hashlib.md5(
                        mapped_path.encode("utf-8")
                    ).hexdigest()
                    converted_cover = (
                        staging
                        / "_converted"
                        / f"book_{digest}.webp"
                    )
                    has_cover = (
                        "cover_image" in columns
                        and converted_cover.is_file()
                    )
                    if has_cover:
                        relative = f"{library_id}/book_{digest}.webp"
                        staged_cover = staging / relative
                        staged_cover.parent.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                        converted_cover.replace(staged_cover)
                        assignments.append("cover_image=?")
                        if "cover_updated_at" in columns:
                            assignments.append(
                                "cover_updated_at=CURRENT_TIMESTAMP"
                            )
                        values.append(relative)

                    if target is not None:
                        statement = (
                            "UPDATE books SET "
                            f"{', '.join(assignments)} WHERE id=?"
                        )
                        statement_batches.setdefault(
                            statement,
                            [],
                        ).append(
                            tuple(values) + (int(target["id"]),)
                        )
                        resolved_targets[mapped_path] = {
                            "id": int(target["id"]),
                            "library_id": library_id,
                            "total_pages": total_pages,
                        }
                        updated_books += 1
                    else:
                        insert_values = {
                            "library_id": library_id,
                            "title": (
                                book["title"]
                                or posixpath.basename(mapped_path)
                            ),
                            "series_name": book["series_name"],
                            "author": (
                                book["author"] or "Kavita Author"
                            ),
                            "publisher": (
                                book["publisher"] or "Kavita Publisher"
                            ),
                            "file_path": mapped_path,
                            "file_format": file_format,
                            "total_pages": total_pages,
                            "summary": book["summary"],
                            "release_date": book["release_date"],
                            "genre": book["genre"],
                            "tags": book["tags"],
                        }
                        if lock_metadata:
                            insert_values["metadata_locked"] = 1
                        if has_cover:
                            insert_values["cover_image"] = relative
                        insert_values = {
                            field: value
                            for field, value in insert_values.items()
                            if field in columns
                        }
                        fields = list(insert_values)
                        placeholders = ", ".join("?" for _ in fields)
                        quoted_fields = ", ".join(
                            f'`{field}`' for field in fields
                        )
                        statement = (
                            "INSERT INTO books "
                            f"({quoted_fields}) "
                            f"VALUES ({placeholders})"
                        )
                        statement_batches.setdefault(
                            statement,
                            [],
                        ).append(
                            tuple(
                                insert_values[field]
                                for field in fields
                            )
                        )
                        new_targets[mapped_path] = {
                            "library_id": library_id,
                            "total_pages": total_pages,
                        }
                        created_books += 1

                self._execute_statement_batches(
                    connection,
                    statement_batches,
                )
                if new_targets:
                    placeholders = ", ".join(
                        "?" for _ in new_targets
                    )
                    inserted = {
                        str(row["file_path"]): int(row["id"])
                        for row in connection.execute(
                            (
                                "SELECT id, file_path FROM books "
                                f"WHERE file_path IN ({placeholders})"
                            ),
                            tuple(new_targets),
                        ).fetchall()
                    }
                    missing_paths = set(new_targets) - set(inserted)
                    if missing_paths:
                        raise RuntimeError(
                            "신규 도서 ID를 확인하지 못했습니다."
                        )
                    for mapped_path, target_data in new_targets.items():
                        resolved_targets[mapped_path] = {
                            "id": inserted[mapped_path],
                            **target_data,
                        }

                current = batch_start + len(batch)
                self._progress(
                    "metadata",
                    cover_progress_units + current,
                    overall_progress_total,
                    (
                        f"Kavita 도서 {current}/"
                        f"{len(migration_books)}권을 이관했습니다."
                    ),
                )

            if import_progress and mapped_progress:
                if self.database_context is not None and self.database_context.engine == "mariadb":
                    progress_statement = """
                        INSERT INTO user_progress
                        (book_id, user_id, pages_read, is_completed, last_read_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON DUPLICATE KEY UPDATE
                            pages_read=VALUES(pages_read),
                            is_completed=VALUES(is_completed),
                            last_read_at=VALUES(last_read_at)
                    """
                else:
                    progress_statement = """
                        INSERT INTO user_progress
                        (book_id, user_id, pages_read, is_completed, last_read_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(book_id, user_id) DO UPDATE SET
                            pages_read=excluded.pages_read,
                            is_completed=excluded.is_completed,
                            last_read_at=excluded.last_read_at
                    """
                for batch_start in range(
                    0,
                    len(mapped_progress),
                    BOOK_WRITE_BATCH_SIZE,
                ):
                    self._check_stop()
                    batch = mapped_progress[
                        batch_start:batch_start + BOOK_WRITE_BATCH_SIZE
                    ]
                    parameters = []
                    for item, mapped_path, target_user_id in batch:
                        target = resolved_targets[mapped_path]
                        total_pages = int(
                            target.get("total_pages") or 0
                        )
                        pages_read = int(item["pages_read"] or 0)
                        if total_pages > 0:
                            pages_read = min(
                                pages_read,
                                total_pages,
                            )
                        completed = int(
                            total_pages > 0
                            and pages_read >= total_pages
                        )
                        parameters.append((
                            int(target["id"]),
                            int(target_user_id),
                            pages_read,
                            completed,
                            item["last_read_at"] or datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                        ))
                    connection.executemany(
                        progress_statement,
                        parameters,
                    )
                    current = batch_start + len(batch)
                    self._progress(
                        "progress",
                        (
                            cover_progress_units
                            + len(migration_books)
                            + current
                        ),
                        overall_progress_total,
                        (
                            f"독서 진행률 {current}/"
                            f"{len(mapped_progress)}건을 적용했습니다."
                        ),
                    )

            for staged_cover in staging.rglob("*.webp"):
                relative = staged_cover.relative_to(staging)
                if relative.parts[0] == "_converted":
                    continue
                self._check_stop()
                destination = self.bookoasis_cover_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup_file = rollback / relative
                if destination.exists():
                    backup_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(destination), str(backup_file))
                    applied_files.append((destination, backup_file))
                else:
                    applied_files.append((destination, None))
                shutil.copy2(str(staged_cover), str(destination))
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            for destination, backup_file in reversed(applied_files):
                if backup_file is not None and backup_file.exists():
                    shutil.copy2(str(backup_file), str(destination))
                elif destination.exists():
                    destination.unlink()
            raise
        finally:
            if connection is not None:
                connection.close()
            shutil.rmtree(str(staging), ignore_errors=True)
            shutil.rmtree(str(rollback), ignore_errors=True)

        preview.update(
            {
                "dry_run": False,
                "books_created_count": created_books,
                "books_updated_count": updated_books,
                "metadata_updated_count": len(migration_books),
                "progress_updated_count": len(mapped_progress),
                "covers_updated_count": converted_covers,
                "backup_path": str(backup_path) if backup_path else "",
            }
        )
        return preview
