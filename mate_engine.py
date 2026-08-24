# BookOasis DB를 읽기 전용으로 검사하고 진단 결과를 생성합니다.
import hashlib
import json
import os
import time
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

try:
    import gevent
    from gevent import monkey as gevent_monkey
except ImportError:
    gevent = None
    gevent_monkey = None

try:
    from .bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError
    from .cover_inspector import normalize_cover_path
    from .series_gap import find_series_gaps
    from .statistics_cache import DEFAULT_SUMMARY_TARGET_CACHE
except ImportError:
    from bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError
    from cover_inspector import normalize_cover_path
    from series_gap import find_series_gaps
    from statistics_cache import DEFAULT_SUMMARY_TARGET_CACHE


def _run_concurrent(items, task, max_workers=3, thread_name_prefix="bookoasis"):
    items = list(items or [])
    if not items:
        return []
    if (
        gevent is not None
        and gevent_monkey is not None
        and gevent_monkey.is_module_patched("threading")
    ):
        greenlets = [gevent.spawn(task, item) for item in items]
        gevent.joinall(greenlets)
        for greenlet in greenlets:
            if greenlet.exception is not None:
                raise greenlet.exception
        return [greenlet.value for greenlet in greenlets]
    with ThreadPoolExecutor(
        max_workers=max(1, min(int(max_workers or 1), len(items))),
        thread_name_prefix=thread_name_prefix,
    ) as executor:
        futures = [executor.submit(task, item) for item in items]
        return [future.result() for future in futures]


ISSUE_LABELS = {
    "cover": "표지 없음",
    "author": "저자 없음",
    "publisher": "출판사 없음",
    "summary": "소개 없음",
    "isbn": "ISBN 없음",
    "pages": "페이지 수 미기록",
    "file_size": "파일 크기 미기록",
    "duplicate_isbn": "ISBN 중복 후보",
}

PROBLEM_BOOK_LABELS = {
    key: label for key, label in ISSUE_LABELS.items() if key != "cover"
}

ISSUE_DIAGNOSTIC_RULES = {
    "author": {
        "column": "books.author",
        "value_key": "author",
        "rule": "공백을 제거한 값이 비어 있음",
        "sql": "TRIM(COALESCE(books.author, '')) = ''",
    },
    "publisher": {
        "column": "books.publisher",
        "value_key": "publisher",
        "rule": "공백을 제거한 값이 비어 있음",
        "sql": "TRIM(COALESCE(books.publisher, '')) = ''",
    },
    "summary": {
        "column": "books.summary",
        "value_key": "summary",
        "rule": "공백을 제거한 값이 비어 있음",
        "sql": "TRIM(COALESCE(books.summary, '')) = ''",
    },
    "isbn": {
        "column": "books.isbn",
        "value_key": "isbn",
        "rule": "ISBN 누락 검사가 활성화되고 공백을 제거한 값이 비어 있음",
        "sql": "TRIM(COALESCE(books.isbn, '')) = ''",
    },
    "pages": {
        "column": "books.total_pages",
        "value_key": "total_pages",
        "rule": "NULL 또는 0 이하이며 원격 ZIP/CBZ 분석 제한 대상이 아님",
        "sql": (
            "COALESCE(books.total_pages, 0) <= 0 AND NOT "
            "(COALESCE(libraries.is_remote, 0) = 1 AND "
            "LOWER(COALESCE(books.file_format, '')) IN ('zip', 'cbz'))"
        ),
    },
    "file_size": {
        "column": "books.file_size",
        "value_key": "file_size",
        "rule": "NULL 또는 0 이하",
        "sql": "COALESCE(books.file_size, 0) <= 0",
    },
    "duplicate_isbn": {
        "column": "books.isbn",
        "value_key": "isbn",
        "rule": "같은 비어 있지 않은 ISBN을 사용하는 활성 도서가 2권 이상",
        "sql": "COUNT(*) OVER normalized ISBN > 1",
    },
}


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value, default, minimum, maximum):
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


class BookOasisMateEngine:
    """BookOasis DB에 쓰기 작업을 하지 않는 진단 엔진입니다."""

    def __init__(self, settings, database_adapter=None):
        self.settings = dict(settings or {})
        self.database_adapter = database_adapter or BookOasisDatabaseAdapter(
            self.settings
        )
        self.stale_days = _as_int(self.settings.get("stale_days"), 14, 1, 3650)
        self.check_missing_isbn = _as_bool(self.settings.get("check_missing_isbn"), False)
        self.bookoasis_url = str(self.settings.get("bookoasis_url") or "").strip().rstrip("/")
        parsed_url = urlparse(self.bookoasis_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            self.bookoasis_url = ""

    def targets(self):
        targets = [
            self.database_adapter.target(
                "general", "일반", self.settings.get("general_db_path")
            ),
        ]
        if _as_bool(self.settings.get("adult_enabled"), False):
            targets.append(
                self.database_adapter.target(
                    "adult", "성인", self.settings.get("adult_db_path")
                )
            )
        if str(self.settings.get("audiobook_db_path") or "").strip():
            targets.append(
                self.database_adapter.target(
                    "audiobook",
                    "오디오북",
                    self.settings.get("audiobook_db_path"),
                )
            )
        if str(self.settings.get("video_db_path") or "").strip():
            targets.append(
                self.database_adapter.target(
                    "video", "비디오", self.settings.get("video_db_path")
                )
            )
        return targets

    def get_target(self, db_type):
        for target in self.targets():
            if target.db_type == db_type:
                return target
        raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_type}")

    def _connect(self, target):
        return self.database_adapter.connect(target)

    @staticmethod
    def _tables(connection):
        return connection.tables()

    @staticmethod
    def _columns(connection, table):
        return connection.columns(table)

    @staticmethod
    def _column_expr(columns, column, expression, alias):
        return f"{expression} AS {alias}" if column in columns else f"0 AS {alias}"

    @staticmethod
    def _value_expr(columns, column, alias=None):
        alias = alias or column
        return f"b.{column} AS {alias}" if column in columns else f"NULL AS {alias}"

    def _book_query_parts(self, connection, include_duplicate_isbn=False):
        tables = self._tables(connection)
        if "books" not in tables:
            raise RuntimeError("books 테이블이 없습니다.")

        columns = self._columns(connection, "books")
        required = {"id", "title"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError("books 필수 컬럼이 없습니다: " + ", ".join(missing))

        active = (
            "(b.is_deleted = 0 OR b.is_deleted IS NULL)"
            if "is_deleted" in columns
            else "1 = 1"
        )
        expressions = {}
        if "cover_image" in columns:
            expressions["cover"] = "TRIM(COALESCE(b.cover_image, '')) = ''"
        if "author" in columns:
            expressions["author"] = "TRIM(COALESCE(b.author, '')) = ''"
        if "publisher" in columns:
            expressions["publisher"] = "TRIM(COALESCE(b.publisher, '')) = ''"
        if "summary" in columns:
            expressions["summary"] = "TRIM(COALESCE(b.summary, '')) = ''"
        if "total_pages" in columns:
            expressions["pages"] = "COALESCE(b.total_pages, 0) <= 0"
        if "file_size" in columns:
            expressions["file_size"] = "COALESCE(b.file_size, 0) <= 0"
        if self.check_missing_isbn and "isbn" in columns:
            expressions["isbn"] = "TRIM(COALESCE(b.isbn, '')) = ''"

        duplicate_cte = ""
        duplicate_join = ""
        duplicate_count = "0"
        if include_duplicate_isbn and "isbn" in columns:
            duplicate_cte = """
                WITH isbn_counts AS (
                    SELECT isbn AS normalized_isbn, COUNT(*) AS item_count
                    FROM books
                    WHERE isbn IS NOT NULL AND isbn != ''
                      AND {active_unaliased}
                    GROUP BY isbn
                    HAVING COUNT(*) > 1
                )
            """.format(
                active_unaliased=(
                    "(is_deleted = 0 OR is_deleted IS NULL)"
                    if "is_deleted" in columns
                    else "1 = 1"
                )
            )
            duplicate_join = "LEFT JOIN isbn_counts ic ON ic.normalized_isbn = b.isbn"
            duplicate_count = "COALESCE(ic.item_count, 0)"
            expressions["duplicate_isbn"] = "COALESCE(ic.item_count, 0) > 1"

        library_join = ""
        library_name = "NULL AS library_name"
        library_columns = set()
        if "libraries" in tables and "library_id" in columns:
            library_columns = self._columns(connection, "libraries")
            if {"id", "name"}.issubset(library_columns):
                library_join = "LEFT JOIN libraries l ON l.id = b.library_id"
                library_name = "l.name AS library_name"

        informational_expressions = {}
        if library_join and "is_remote" in library_columns and "file_format" in columns:
            remote_archive = (
                "(COALESCE(l.is_remote, 0) = 1 AND "
                "LOWER(TRIM(COALESCE(b.file_format, ''))) IN ('zip', 'cbz'))"
            )
            if "pages" in expressions:
                missing_pages = expressions["pages"]
                expressions["pages"] = f"({missing_pages}) AND NOT {remote_archive}"
                informational_expressions["remote_page_limited"] = (
                    f"({missing_pages}) AND {remote_archive}"
                )
            if "cover" in expressions:
                missing_cover = expressions["cover"]
                expressions["cover"] = f"({missing_cover}) AND NOT {remote_archive}"
                informational_expressions["remote_cover_limited"] = (
                    f"({missing_cover}) AND {remote_archive}"
                )

        return {
            "tables": tables,
            "columns": columns,
            "active": active,
            "expressions": expressions,
            "duplicate_cte": duplicate_cte,
            "duplicate_join": duplicate_join,
            "duplicate_count": duplicate_count,
            "library_join": library_join,
            "library_name": library_name,
            "informational_expressions": informational_expressions,
        }

    def _book_summary(self, connection):
        parts = self._book_query_parts(connection, include_duplicate_isbn=False)
        expressions = parts["expressions"]
        select_items = ["COUNT(*) AS total_books"]
        for issue_type in ISSUE_LABELS:
            expression = expressions.get(issue_type)
            select_items.append(
                f"SUM(CASE WHEN {expression} THEN 1 ELSE 0 END) AS {issue_type}"
                if expression
                else f"0 AS {issue_type}"
            )
        for key in ("remote_page_limited", "remote_cover_limited"):
            expression = parts["informational_expressions"].get(key)
            select_items.append(
                f"SUM(CASE WHEN {expression} THEN 1 ELSE 0 END) AS {key}"
                if expression
                else f"0 AS {key}"
            )

        problem_expression = " OR ".join(expressions.values()) or "0"
        select_items.append(
            f"SUM(CASE WHEN {problem_expression} THEN 1 ELSE 0 END) AS problem_books"
        )
        query = f"""
            SELECT {', '.join(select_items)}
            FROM books b
            {parts['library_join']}
            WHERE {parts['active']}
        """
        row = connection.execute(query).fetchone()
        result = {key: int(row[key] or 0) for key in row.keys()}
        for issue_type in ISSUE_LABELS:
            result.setdefault(issue_type, 0)
        result["duplicate_isbn_deferred"] = "isbn" in parts["columns"]
        return result

    def _audiobook_summary(self, connection):
        tables = self._tables(connection)
        if "audiobooks" not in tables:
            raise RuntimeError("audiobooks 테이블이 없습니다.")
        columns = self._columns(connection, "audiobooks")
        active = (
            "(a.is_deleted = 0 OR a.is_deleted IS NULL)"
            if "is_deleted" in columns
            else "1 = 1"
        )

        def missing(column):
            return f"TRIM(COALESCE(a.{column}, '')) = ''" if column in columns else "0"

        cover = missing("poster")
        author = missing("author")
        publisher = missing("publisher")
        summary = missing("description")
        tracks = "COALESCE(a.total_tracks, 0) <= 0" if "total_tracks" in columns else "0"
        problem = " OR ".join((cover, author, publisher, summary, tracks))
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total_audiobooks,
                SUM(CASE WHEN {cover} THEN 1 ELSE 0 END) AS cover,
                SUM(CASE WHEN {author} THEN 1 ELSE 0 END) AS author,
                SUM(CASE WHEN {publisher} THEN 1 ELSE 0 END) AS publisher,
                SUM(CASE WHEN {summary} THEN 1 ELSE 0 END) AS summary,
                SUM(CASE WHEN {tracks} THEN 1 ELSE 0 END) AS tracks,
                SUM(CASE WHEN {problem} THEN 1 ELSE 0 END) AS problem_audiobooks
            FROM audiobooks a
            WHERE {active}
            """
        ).fetchone()
        result = {key: int(row[key] or 0) for key in row.keys()}
        result["total_tracks"] = 0
        result["track_file_size_missing"] = 0
        if "audiobook_tracks" in tables:
            track_columns = self._columns(connection, "audiobook_tracks")
            missing_size = (
                "SUM(CASE WHEN COALESCE(t.file_size, 0) <= 0 THEN 1 ELSE 0 END)"
                if "file_size" in track_columns
                else "0"
            )
            track_row = connection.execute(
                f"""
                SELECT COUNT(*) AS total_tracks,
                       {missing_size} AS track_file_size_missing
                FROM audiobook_tracks t
                JOIN audiobooks a ON a.id = t.audiobook_id
                WHERE {active}
                """
            ).fetchone()
            result["total_tracks"] = int(track_row["total_tracks"] or 0)
            result["track_file_size_missing"] = int(
                track_row["track_file_size_missing"] or 0
            )
        return result

    def _video_summary(self, connection):
        tables = self._tables(connection)
        if "videos" not in tables:
            raise RuntimeError("videos 테이블이 없습니다.")
        columns = self._columns(connection, "videos")
        active = (
            "(v.is_deleted = 0 OR v.is_deleted IS NULL)"
            if "is_deleted" in columns
            else "1 = 1"
        )

        def missing(column):
            return f"TRIM(COALESCE(v.{column}, '')) = ''" if column in columns else "0"

        poster = missing("poster")
        description = missing("description")
        genres = missing("genres")
        episodes = "COALESCE(v.total_episodes, 0) <= 0" if "total_episodes" in columns else "0"
        duration = "COALESCE(v.total_duration, 0) <= 0" if "total_duration" in columns else "0"
        problem = " OR ".join((poster, description, genres, episodes, duration))
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total_videos,
                   SUM(CASE WHEN {poster} THEN 1 ELSE 0 END) AS poster,
                   SUM(CASE WHEN {description} THEN 1 ELSE 0 END) AS description,
                   SUM(CASE WHEN {genres} THEN 1 ELSE 0 END) AS genres,
                   SUM(CASE WHEN {episodes} THEN 1 ELSE 0 END) AS episodes,
                   SUM(CASE WHEN {duration} THEN 1 ELSE 0 END) AS duration,
                   SUM(CASE WHEN {problem} THEN 1 ELSE 0 END) AS problem_videos
            FROM videos v WHERE {active}
            """
        ).fetchone()
        result = {key: int(row[key] or 0) for key in row.keys()}
        result.update(
            total_episodes=0,
            episode_file_size_missing=0,
            episode_duration_missing=0,
            episode_resolution_missing=0,
            needs_transcode=0,
            container_unverified=0,
        )
        if "video_episodes" not in tables:
            return result
        episode_columns = self._columns(connection, "video_episodes")

        def count_if(column, expression):
            return (
                f"SUM(CASE WHEN {expression} THEN 1 ELSE 0 END)"
                if column in episode_columns
                else "0"
            )

        episode_row = connection.execute(
            f"""
            SELECT COUNT(*) AS total_episodes,
                   {count_if('file_size', 'COALESCE(e.file_size, 0) <= 0')} AS episode_file_size_missing,
                   {count_if('duration', 'COALESCE(e.duration, 0) <= 0')} AS episode_duration_missing,
                   {count_if('width', 'COALESCE(e.width, 0) <= 0 OR COALESCE(e.height, 0) <= 0')} AS episode_resolution_missing,
                   {count_if('needs_transcode', 'COALESCE(e.needs_transcode, 0) = 1')} AS needs_transcode,
                   {count_if('container_verified', 'COALESCE(e.container_verified, 0) = 0')} AS container_unverified
            FROM video_episodes e
            JOIN videos v ON v.id = e.video_id
            WHERE {active}
            """
        ).fetchone()
        for key in (
            "total_episodes",
            "episode_file_size_missing",
            "episode_duration_missing",
            "episode_resolution_missing",
            "needs_transcode",
            "container_unverified",
        ):
            result[key] = int(episode_row[key] or 0)
        return result

    def _issue_diagnostics(self, item, issue_keys):
        reasons = []
        for issue_type in issue_keys:
            definition = ISSUE_DIAGNOSTIC_RULES[issue_type]
            reason = {
                "type": issue_type,
                "label": PROBLEM_BOOK_LABELS[issue_type],
                "column": definition["column"],
                "value": item.get(definition["value_key"]),
                "rule": definition["rule"],
                "sql": definition["sql"],
            }
            if issue_type == "duplicate_isbn":
                reason["matching_active_books"] = int(item.get("isbn_count") or 0)
            reasons.append(reason)

        return {
            "book": {
                "id": item.get("id"),
                "db_type": item.get("db_type"),
                "title": item.get("title"),
                "series_name": item.get("series_name"),
                "library_id": item.get("library_id"),
                "library_name": item.get("library_name"),
                "metadata_locked": item.get("metadata_locked"),
            },
            "classification": {
                "active_filter": {
                    "rule": "삭제되지 않은 활성 도서",
                    "sql": "books.is_deleted = 0 OR books.is_deleted IS NULL",
                    "matched": True,
                },
                "matched_issue_count": len(reasons),
                "reasons": reasons,
                "note": (
                    "BookOasis Mate가 "
                    f"{self.database_adapter.engine}를 읽기 전용으로 조회한 판정 근거입니다."
                ),
            },
        }

    def _scanner_summary(self, connection):
        tables = self._tables(connection)
        result = {
            "total_libraries": 0,
            "stale_libraries": 0,
            "failed_libraries": 0,
            "active_libraries": 0,
            "recent_failed_tasks": 0,
            "pending_tasks": 0,
            "running_tasks": 0,
        }

        if "libraries" in tables:
            columns = self._columns(connection, "libraries")
            if "id" in columns:
                last_scan = "last_scanned_at" in columns
                scan_status = "scan_status" in columns
                stale_expression = (
                    "SUM(CASE WHEN last_scanned_at IS NULL "
                    "OR last_scanned_at < ? "
                    "THEN 1 ELSE 0 END)"
                    if last_scan
                    else "0"
                )
                failed_expression = (
                    "SUM(CASE WHEN scan_status IN ('failed', 'interrupted') THEN 1 ELSE 0 END)"
                    if scan_status
                    else "0"
                )
                active_expression = (
                    "SUM(CASE WHEN scan_status IN ('scanning', 'cancelling') THEN 1 ELSE 0 END)"
                    if scan_status
                    else "0"
                )
                row = connection.execute(
                    f"""
                    SELECT
                        COUNT(*) AS total_libraries,
                        {stale_expression} AS stale_libraries,
                        {failed_expression} AS failed_libraries,
                        {active_expression} AS active_libraries
                    FROM libraries
                    """,
                    (
                        (
                            datetime.now(timezone.utc) - timedelta(days=self.stale_days)
                        ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    if last_scan
                    else (),
                ).fetchone()
                for key in ("total_libraries", "stale_libraries", "failed_libraries", "active_libraries"):
                    result[key] = int(row[key] or 0)

        if "scanner_tasks" in tables:
            columns = self._columns(connection, "scanner_tasks")
            if "status" in columns:
                recent_condition = (
                    "enqueue_at >= ? OR status IN ('pending', 'running', 'exit_pending')"
                    if "enqueue_at" in columns
                    else "status IN ('pending', 'running', 'exit_pending', 'failed')"
                )
                params = (
                    (
                        datetime.now(timezone.utc) - timedelta(days=7)
                    ).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S"),
                ) if "enqueue_at" in columns else ()
                row = connection.execute(
                    f"""
                    SELECT
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS recent_failed_tasks,
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_tasks,
                        SUM(CASE WHEN status IN ('running', 'exit_pending') THEN 1 ELSE 0 END) AS running_tasks
                    FROM scanner_tasks
                    WHERE {recent_condition}
                    """,
                    params,
                ).fetchone()
                for key in ("recent_failed_tasks", "pending_tasks", "running_tasks"):
                    result[key] = int(row[key] or 0)
        return result

    def inspect_target(self, target):
        started = datetime.now(timezone.utc)
        try:
            with closing(self._connect(target)) as connection:
                tables = self._tables(connection)
                media_kind = target.db_type if target.db_type in {"audiobook", "video"} else "book"
                if media_kind == "audiobook":
                    summary = self._audiobook_summary(connection)
                elif media_kind == "video":
                    summary = self._video_summary(connection)
                else:
                    summary = self._book_summary(connection)
                scanner = self._scanner_summary(connection)
                engine_details = connection.engine_details()
                database_size = connection.database_size()
            problem_key = {
                "audiobook": "problem_audiobooks",
                "video": "problem_videos",
            }.get(media_kind, "problem_books")
            problem_count = summary[problem_key]
            status = "healthy" if problem_count == 0 and scanner["failed_libraries"] == 0 and scanner["recent_failed_tasks"] == 0 else "warning"
            status_reasons = []
            if problem_count:
                status_reasons.append({
                    "code": problem_key,
                    "label": (
                        "점검이 필요한 오디오북"
                        if media_kind == "audiobook"
                        else "점검이 필요한 비디오"
                        if media_kind == "video"
                        else "점검이 필요한 도서"
                    ),
                    "count": problem_count,
                    "unit": "개" if media_kind in {"audiobook", "video"} else "권",
                })
            if scanner["failed_libraries"]:
                status_reasons.append({
                    "code": "failed_libraries",
                    "label": "스캔 실패·중단 보관함",
                    "count": scanner["failed_libraries"],
                    "unit": "개",
                })
            if scanner["recent_failed_tasks"]:
                status_reasons.append({
                    "code": "recent_failed_tasks",
                    "label": "최근 7일 스캔 작업 실패",
                    "count": scanner["recent_failed_tasks"],
                    "unit": "건",
                })
            return {
                "db_type": target.db_type,
                "media_kind": media_kind,
                "label": target.label,
                "path": target.path,
                "database": target.database,
                "engine": target.engine,
                "connected": True,
                "status": status,
                "status_reasons": status_reasons,
                "file_size": database_size,
                **engine_details,
                "tables": sorted(tables),
                "summary": summary,
                "scanner": scanner,
                "error": None,
                "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            }
        except (
            OSError,
            sqlite3.Error,
            BookOasisDatabaseError,
            RuntimeError,
            ValueError,
        ) as error:
            return {
                "db_type": target.db_type,
                "media_kind": target.db_type if target.db_type in {"audiobook", "video"} else "book",
                "label": target.label,
                "path": target.path,
                "database": target.database,
                "engine": target.engine,
                "connected": False,
                "status": "error",
                "status_reasons": [{
                    "code": "connection_error",
                    "label": "DB 연결 실패",
                    "count": 1,
                    "unit": "건",
                }],
                "summary": {},
                "scanner": {},
                "error": str(error),
                "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
            }

    def _target_source_fingerprint(self, target):
        if target.engine == "sqlite":
            try:
                stat = os.stat(target.path)
                payload = {
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                }
            except OSError as error:
                payload = {"error": type(error).__name__}
        else:
            try:
                with closing(self._connect(target)) as connection:
                    rows = connection.execute(
                        """
                        SELECT table_name, table_rows, update_time,
                               data_length, index_length
                        FROM information_schema.tables
                        WHERE table_schema = ?
                          AND table_name IN (
                            'books', 'audiobooks', 'audiobook_tracks',
                            'videos', 'video_episodes',
                            'libraries', 'scanner_tasks'
                          )
                        ORDER BY table_name
                        """,
                        (target.database,),
                    ).fetchall()
                payload = [dict(row) for row in rows]
            except (BookOasisDatabaseError, RuntimeError, ValueError) as error:
                payload = {"error": str(error)}
        encoded = json.dumps(
            {
                "db_type": target.db_type,
                "engine": target.engine,
                "database": target.database or target.path,
                "source": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def source_fingerprint(self):
        targets = self.targets()
        fingerprints = _run_concurrent(
            targets,
            self._target_source_fingerprint,
            max_workers=3,
            thread_name_prefix="bookoasis-summary-source",
        )
        target_fingerprints = {
            target.db_type: fingerprint
            for target, fingerprint in zip(targets, fingerprints)
        }
        encoded = json.dumps(
            target_fingerprints,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return {
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
            "targets": target_fingerprints,
        }

    def build_report(self, source=None, target_cache=None, force=False):
        targets = self.targets()
        source = source or self.source_fingerprint()
        target_cache = target_cache or DEFAULT_SUMMARY_TARGET_CACHE

        def inspect(target):
            if force:
                return self.inspect_target(target)
            key = (
                "summary-target",
                target.db_type,
                str((source.get("targets") or {}).get(target.db_type) or ""),
                bool(self.check_missing_isbn),
                int(self.stale_days),
            )
            return target_cache.get_or_compute(
                key,
                lambda: self.inspect_target(target),
            )

        databases = _run_concurrent(
            targets,
            inspect,
            max_workers=3,
            thread_name_prefix="bookoasis-summary",
        )
        totals = {
            key: 0
            for key in [
                "total_books",
                "problem_books",
                *ISSUE_LABELS.keys(),
                "remote_page_limited",
                "remote_cover_limited",
            ]
        }
        scanner_totals = {
            key: 0
            for key in (
                "total_libraries",
                "stale_libraries",
                "failed_libraries",
                "active_libraries",
                "recent_failed_tasks",
                "pending_tasks",
                "running_tasks",
            )
        }
        audiobook_totals = {
            "total_audiobooks": 0,
            "problem_audiobooks": 0,
            "cover": 0,
            "author": 0,
            "publisher": 0,
            "summary": 0,
            "tracks": 0,
            "total_tracks": 0,
            "track_file_size_missing": 0,
        }
        video_totals = {
            key: 0
            for key in (
                "total_videos", "problem_videos", "poster", "description",
                "genres", "episodes", "duration", "total_episodes",
                "episode_file_size_missing", "episode_duration_missing",
                "episode_resolution_missing", "needs_transcode", "container_unverified",
            )
        }
        for database in databases:
            if database.get("media_kind") == "audiobook":
                for key in audiobook_totals:
                    audiobook_totals[key] += int(
                        database.get("summary", {}).get(key, 0) or 0
                    )
            elif database.get("media_kind") == "video":
                for key in video_totals:
                    video_totals[key] += int(database.get("summary", {}).get(key, 0) or 0)
            else:
                for key in totals:
                    totals[key] += int(database.get("summary", {}).get(key, 0) or 0)
            for key in scanner_totals:
                scanner_totals[key] += int(database.get("scanner", {}).get(key, 0) or 0)

        errors = sum(1 for database in databases if not database["connected"])
        if errors:
            status = "error"
        elif (
            totals["problem_books"]
            or audiobook_totals["problem_audiobooks"]
            or video_totals["problem_videos"]
            or scanner_totals["failed_libraries"]
            or scanner_totals["recent_failed_tasks"]
        ):
            status = "warning"
        else:
            status = "healthy"
        healthy_books = max(0, totals["total_books"] - totals["problem_books"])
        totals["healthy_books"] = healthy_books
        totals["health_percent"] = round(healthy_books / totals["total_books"] * 100) if totals["total_books"] else 0
        duplicate_isbn_deferred = any(
            database.get("media_kind") == "book"
            and bool(database.get("summary", {}).get("duplicate_isbn_deferred"))
            for database in databases
        )
        totals["duplicate_isbn_deferred"] = duplicate_isbn_deferred
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database_engine": self.database_adapter.public_info(),
            "status": status,
            "databases": databases,
            "totals": totals,
            "scanner": scanner_totals,
            "audiobook": audiobook_totals,
            "video": video_totals,
            "stale_days": self.stale_days,
            "check_missing_isbn": self.check_missing_isbn,
            "duplicate_isbn_deferred": duplicate_isbn_deferred,
        }

    def _issue_query_context(
        self,
        connection,
        library_id=None,
        issue_type="all",
        search="",
        include_duplicate_isbn=None,
    ):
        search = str(search or "").strip()
        issue_type = issue_type if issue_type in PROBLEM_BOOK_LABELS else "all"
        selected_library_id = None
        if str(library_id or "").strip():
            try:
                selected_library_id = int(library_id)
            except (TypeError, ValueError):
                raise ValueError("보관함 ID가 올바르지 않습니다.")
            if selected_library_id <= 0:
                raise ValueError("보관함 ID가 올바르지 않습니다.")

        include_duplicate = (
            issue_type in {"all", "duplicate_isbn"}
            if include_duplicate_isbn is None
            else bool(include_duplicate_isbn)
        )
        parts = self._book_query_parts(
            connection,
            include_duplicate_isbn=include_duplicate,
        )
        expressions = {
            key: value
            for key, value in parts["expressions"].items()
            if key in PROBLEM_BOOK_LABELS
        }
        if issue_type != "all" and issue_type not in expressions:
            return None
        if selected_library_id is not None and "library_id" not in parts["columns"]:
            return None
        if not expressions:
            return None

        conditions = [parts["active"]]
        conditions.append(
            expressions[issue_type]
            if issue_type != "all"
            else "(" + " OR ".join(expressions.values()) + ")"
        )
        params = []
        if selected_library_id is not None:
            conditions.append("b.library_id = ?")
            params.append(selected_library_id)
        if search:
            search_fields = [
                f"COALESCE(b.{name}, '')"
                for name in ("title", "series_name", "author")
                if name in parts["columns"]
            ]
            if search_fields:
                conditions.append(
                    "(" + " OR ".join(f"{field} LIKE ?" for field in search_fields) + ")"
                )
                params.extend([f"%{search}%"] * len(search_fields))
        return {
            "parts": parts,
            "expressions": expressions,
            "conditions": conditions,
            "params": params,
            "where": " AND ".join(conditions),
            "base_from": (
                f"FROM books b {parts['duplicate_join']} {parts['library_join']}"
            ),
        }

    def count_issues(
        self,
        db_type="general",
        library_id=None,
        issue_type="all",
        search="",
    ):
        target = self.get_target(db_type)
        with closing(self._connect(target)) as connection:
            normalized_issue_type = (
                issue_type if issue_type in PROBLEM_BOOK_LABELS else "all"
            )
            if normalized_issue_type == "all":
                base_context = self._issue_query_context(
                    connection,
                    library_id=library_id,
                    issue_type=issue_type,
                    search=search,
                    include_duplicate_isbn=False,
                )
                total = 0
                if base_context is not None:
                    base_row = connection.execute(
                        f"""
                        SELECT COUNT(*) AS filtered_total
                        {base_context['base_from']}
                        WHERE {base_context['where']}
                        """,
                        base_context["params"],
                    ).fetchone()
                    total = int(base_row["filtered_total"] or 0)

                duplicate_context = self._issue_query_context(
                    connection,
                    library_id=library_id,
                    issue_type="duplicate_isbn",
                    search=search,
                )
                if duplicate_context is None:
                    return total
                duplicate_parts = duplicate_context["parts"]
                base_problem = (
                    " OR ".join(base_context["expressions"].values())
                    if base_context is not None
                    else "0"
                ) or "0"
                duplicate_row = connection.execute(
                    f"""
                    {duplicate_parts['duplicate_cte']}
                    SELECT COUNT(*) AS filtered_total
                    {duplicate_context['base_from']}
                    WHERE {duplicate_context['where']}
                      AND NOT ({base_problem})
                    """,
                    duplicate_context["params"],
                ).fetchone()
                return total + int(duplicate_row["filtered_total"] or 0)

            context = self._issue_query_context(
                connection,
                library_id=library_id,
                issue_type=issue_type,
                search=search,
            )
            if context is None:
                return 0
            parts = context["parts"]
            row = connection.execute(
                f"""
                {parts['duplicate_cte']}
                SELECT COUNT(*) AS filtered_total
                {context['base_from']}
                WHERE {context['where']}
                """,
                context["params"],
            ).fetchone()
            return int(row["filtered_total"] or 0)

    def list_issues(
        self,
        db_type="general",
        library_id=None,
        issue_type="all",
        search="",
        page=1,
        page_size=50,
        known_total=None,
        skip_count=False,
    ):
        target = self.get_target(db_type)
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size, 50, 10, 200)
        with closing(self._connect(target)) as connection:
            context = self._issue_query_context(
                connection,
                library_id=library_id,
                issue_type=issue_type,
                search=search,
            )
            if context is None:
                return {
                    "items": [],
                    "total": 0,
                    "total_exact": True,
                    "count_status": "exact",
                    "has_more": False,
                    "page": page,
                    "page_size": page_size,
                    "pages": 0,
                }
            parts = context["parts"]
            expressions = context["expressions"]
            if known_total is None and not skip_count:
                total_row = connection.execute(
                    f"""
                    {parts['duplicate_cte']}
                    SELECT COUNT(*) AS filtered_total
                    {context['base_from']}
                    WHERE {context['where']}
                    """,
                    context["params"],
                ).fetchone()
                total = int(total_row["filtered_total"] or 0)
                count_status = "exact"
            elif known_total is None:
                total = None
                count_status = "unavailable"
            else:
                total = max(0, int(known_total or 0))
                count_status = "exact"

            flag_select = [
                f"CASE WHEN {expressions[key]} THEN 1 ELSE 0 END AS issue_{key}"
                if key in expressions
                else f"0 AS issue_{key}"
                for key in PROBLEM_BOOK_LABELS
            ]
            value_select = [
                self._value_expr(parts["columns"], name)
                for name in ("id", "title", "series_name", "library_id", "author", "publisher", "isbn", "cover_image", "total_pages", "file_size", "created_at", "metadata_locked")
            ]
            rows = connection.execute(
                f"""
                {parts['duplicate_cte']}
                SELECT {', '.join(value_select)}, {parts['library_name']},
                       {parts['duplicate_count']} AS isbn_count,
                       {', '.join(flag_select)}
                {context['base_from']}
                WHERE {context['where']}
                ORDER BY {('b.created_at DESC,' if 'created_at' in parts['columns'] else '')} b.id DESC
                LIMIT ? OFFSET ?
                """,
                [*context["params"], page_size + 1, (page - 1) * page_size],
            ).fetchall()

        has_more = len(rows) > page_size
        if has_more:
            rows = rows[:page_size]

        items = []
        for row in rows:
            item = dict(row)
            issue_keys = [key for key in PROBLEM_BOOK_LABELS if item.pop(f"issue_{key}", 0)]
            item["issues"] = [{"type": key, "label": PROBLEM_BOOK_LABELS[key]} for key in issue_keys]
            item["db_type"] = db_type
            item["diagnostics"] = self._issue_diagnostics(item, issue_keys)
            cover = str(item.get("cover_image") or "").replace("\\", "/").lstrip("/")
            item["cover_url"] = f"{self.bookoasis_url}/covers/{quote(cover, safe='/')}" if cover and self.bookoasis_url else ""
            items.append(item)
        pages = (
            (int(total) + page_size - 1) // page_size
            if total is not None
            else page + (1 if has_more else 0)
        )
        return {
            "items": items,
            "total": int(total) if total is not None else None,
            "total_exact": total is not None,
            "count_status": count_status,
            "has_more": has_more,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def issue_book_batch(
        self,
        db_type="general",
        library_id=None,
        issue_type="all",
        search="",
        after_id=0,
        batch_size=500,
    ):
        target = self.get_target(db_type)
        after_id = _as_int(after_id, 0, 0, 2147483647)
        batch_size = _as_int(batch_size, 500, 1, 2000)
        with closing(self._connect(target)) as connection:
            context = self._issue_query_context(
                connection,
                library_id=library_id,
                issue_type=issue_type,
                search=search,
            )
            if context is None:
                return []
            parts = context["parts"]
            rows = connection.execute(
                f"""
                {parts['duplicate_cte']}
                SELECT b.id, b.title
                {context['base_from']}
                WHERE {context['where']} AND b.id > ?
                ORDER BY b.id
                LIMIT ?
                """,
                [*context["params"], after_id, batch_size],
            ).fetchall()
            return [
                {"id": int(row["id"]), "title": str(row["title"] or "")}
                for row in rows
                if int(row["id"] or 0) > 0
            ]

    def iter_issue_book_batches(
        self,
        db_type="general",
        library_id=None,
        issue_type="all",
        search="",
        after_id=0,
        batch_size=500,
    ):
        last_id = max(0, int(after_id or 0))
        while True:
            batch = self.issue_book_batch(
                db_type=db_type,
                library_id=library_id,
                issue_type=issue_type,
                search=search,
                after_id=last_id,
                batch_size=batch_size,
            )
            if not batch:
                break
            yield batch
            last_id = int(batch[-1]["id"])

    def issue_book_ids(self, db_type="general", library_id=None, issue_type="all", search=""):
        return [
            int(item["id"])
            for batch in self.iter_issue_book_batches(
                db_type=db_type,
                library_id=library_id,
                issue_type=issue_type,
                search=search,
            )
            for item in batch
        ]

    def scanner_status(self, db_type="general", limit=100):
        target = self.get_target(db_type)
        limit = _as_int(limit, 100, 10, 500)
        with closing(self._connect(target)) as connection:
            tables = self._tables(connection)
            libraries = []
            tasks = []
            if "libraries" in tables:
                columns = self._columns(connection, "libraries")
                select = [
                    f"{column}" if column in columns else f"NULL AS {column}"
                    for column in (
                        "id",
                        "name",
                        "last_scanned_at",
                        "scan_status",
                        "is_remote",
                        "cron_schedule",
                        "vfs_refresh_before_scan",
                        "rclone_rc_url",
                    )
                ]
                libraries = [dict(row) for row in connection.execute(f"SELECT {', '.join(select)} FROM libraries ORDER BY name").fetchall()]
                checkpoint_counts = {}
                if "scanner_progress" in tables:
                    progress_columns = self._columns(connection, "scanner_progress")
                    if "library_id" in progress_columns:
                        checkpoint_counts = {
                            str(row["library_id"]): int(row["folder_count"] or 0)
                            for row in connection.execute(
                                """
                                SELECT library_id, COUNT(*) AS folder_count
                                FROM scanner_progress
                                GROUP BY library_id
                                """
                            ).fetchall()
                        }
                for library in libraries:
                    rc_url = str(library.pop("rclone_rc_url", "") or "").strip()
                    library["rclone_rc_configured"] = bool(rc_url)
                    library["checkpoint_folders"] = checkpoint_counts.get(
                        str(library.get("id")), 0
                    )
            if "scanner_tasks" in tables:
                columns = self._columns(connection, "scanner_tasks")
                select = [
                    f"{column}" if column in columns else f"NULL AS {column}"
                    for column in ("id", "task_type", "task_key", "status", "stage", "enqueue_at", "started_at", "finished_at", "error_message")
                ]
                order = "enqueue_at DESC" if "enqueue_at" in columns else "id DESC"
                tasks = [dict(row) for row in connection.execute(f"SELECT {', '.join(select)} FROM scanner_tasks ORDER BY {order} LIMIT ?", (limit,)).fetchall()]
        return {"db_type": db_type, "libraries": libraries, "tasks": tasks}

    def analyze_series_gaps(self, db_type="general", library_id=None, search=""):
        started = time.monotonic()
        target = self.get_target(db_type)
        search = str(search or "").strip()
        selected_library_id = None
        if str(library_id or "").strip():
            try:
                selected_library_id = int(library_id)
            except (TypeError, ValueError):
                raise ValueError("보관함 ID가 올바르지 않습니다.")
            if selected_library_id <= 0:
                raise ValueError("보관함 ID가 올바르지 않습니다.")

        with closing(self._connect(target)) as connection:
            parts = self._book_query_parts(connection)
            if "series_name" not in parts["columns"]:
                return {
                    "items": [], "total": 0,
                    "analyzed_books": 0, "duration_ms": round((time.monotonic() - started) * 1000),
                }
            if selected_library_id is not None and "library_id" not in parts["columns"]:
                return {
                    "items": [], "total": 0,
                    "analyzed_books": 0, "duration_ms": round((time.monotonic() - started) * 1000),
                }
            library_id_expr = "b.library_id" if "library_id" in parts["columns"] else "NULL"
            file_path = "b.file_path" if "file_path" in parts["columns"] else "NULL"
            cover_image = self._value_expr(parts["columns"], "cover_image")
            conditions = [parts["active"], "TRIM(COALESCE(b.series_name, '')) != ''"]
            params = []
            if selected_library_id is not None:
                conditions.append("b.library_id = ?")
                params.append(selected_library_id)
            if search:
                search_fields = ["COALESCE(b.series_name, '')"]
                if parts["library_join"]:
                    search_fields.append("COALESCE(l.name, '')")
                conditions.append("(" + " OR ".join(f"{field} LIKE ?" for field in search_fields) + ")")
                params.extend([f"%{search}%"] * len(search_fields))
            cursor = connection.execute(
                f"""
                SELECT b.id, b.title, b.series_name, {library_id_expr} AS library_id,
                       {file_path} AS file_path, {cover_image}, {parts['library_name']}
                FROM books b
                {parts['library_join']}
                WHERE {' AND '.join(conditions)}
                ORDER BY {('b.library_id,' if 'library_id' in parts['columns'] else '')}
                         b.series_name, b.id
                """,
                params,
            )
            items = []
            analyzed_books = 0
            current_key = None
            current_rows = []
            while True:
                rows = cursor.fetchmany(2000)
                if not rows:
                    break
                for raw_row in rows:
                    row = dict(raw_row)
                    key = (
                        row.get("library_id"),
                        str(row.get("series_name") or "").strip(),
                    )
                    if current_key is not None and key != current_key:
                        items.extend(find_series_gaps(current_rows))
                        current_rows = []
                    current_key = key
                    current_rows.append(row)
                    analyzed_books += 1
            if current_rows:
                items.extend(find_series_gaps(current_rows))
        items.sort(
            key=lambda item: (
                -len(item.get("missing") or []),
                str(item.get("series_name") or "").lower(),
            )
        )
        for item in items:
            relative = normalize_cover_path(item.get("cover_image"))
            item["cover_url"] = (
                f"{self.bookoasis_url}/covers/{quote(relative, safe='/')}"
                if relative and relative != "NO_COVER" and self.bookoasis_url
                else ""
            )
            item["db_type"] = db_type
            item.pop("cover_image", None)
        total = len(items)
        return {
            "items": items,
            "total": total,
            "analyzed_books": analyzed_books,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

    def series_gaps(
        self,
        db_type="general",
        library_id=None,
        search="",
        page=1,
        page_size=50,
    ):
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size, 50, 10, 200)
        data = self.analyze_series_gaps(
            db_type=db_type,
            library_id=library_id,
            search=search,
        )
        offset = (page - 1) * page_size
        return {
            "items": data["items"][offset:offset + page_size],
            "total": data["total"],
            "page": page,
            "page_size": page_size,
            "pages": (data["total"] + page_size - 1) // page_size,
            "analyzed_books": data["analyzed_books"],
            "duration_ms": data["duration_ms"],
        }

    def cover_items(self, db_type="general", library_id=None, search="", page=1, page_size=50):
        target = self.get_target(db_type)
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size, 50, 10, 1000)
        search = str(search or "").strip()
        selected_library_id = None
        if str(library_id or "").strip():
            try:
                selected_library_id = int(library_id)
            except (TypeError, ValueError):
                raise ValueError("보관함 ID가 올바르지 않습니다.")
            if selected_library_id <= 0:
                raise ValueError("보관함 ID가 올바르지 않습니다.")
        with closing(self._connect(target)) as connection:
            parts = self._book_query_parts(connection)
            if "cover_image" not in parts["columns"]:
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 0}
            if selected_library_id is not None and "library_id" not in parts["columns"]:
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 0}
            conditions = [parts["active"]]
            params = []
            if selected_library_id is not None:
                conditions.append("b.library_id = ?")
                params.append(selected_library_id)
            if search:
                fields = [f"COALESCE(b.{name}, '')" for name in ("title", "series_name") if name in parts["columns"]]
                if fields:
                    conditions.append("(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")")
                    params.extend([f"%{search}%"] * len(fields))
            where = " AND ".join(conditions)
            library_id = "b.library_id" if "library_id" in parts["columns"] else "NULL"
            cover_updated = "b.cover_updated_at" if "cover_updated_at" in parts["columns"] else "NULL"
            total = int(connection.execute(f"SELECT COUNT(*) FROM books b WHERE {where}", params).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT b.id, b.title, {self._value_expr(parts['columns'], 'series_name')},
                       {library_id} AS library_id, b.cover_image, {cover_updated} AS cover_updated_at,
                       {parts['library_name']}
                FROM books b
                {parts['library_join']}
                WHERE {where}
                ORDER BY b.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            relative = normalize_cover_path(item.get("cover_image"))
            item["cover_path"] = relative
            item["cover_url"] = f"{self.bookoasis_url}/covers/{quote(relative, safe='/')}" if relative and self.bookoasis_url else ""
            item.pop("cover_image", None)
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": (total + page_size - 1) // page_size}

    def all_cover_references(
        self,
        include_inactive_adult=False,
        library_ids=None,
        batch_size=2000,
        on_progress=None,
        should_stop=None,
    ):
        references = set()
        selected_ids = sorted({
            int(value)
            for value in (library_ids or [])
            if str(value or "").strip().isdigit() and int(value) > 0
        })
        batch_size = _as_int(batch_size, 2000, 100, 10000)
        read_count = 0
        targets = self.targets()
        adult_path = self.settings.get("adult_db_path")
        if include_inactive_adult and not any(target.db_type == "adult" for target in targets):
            if self.database_adapter.engine == "mariadb" or adult_path:
                targets.append(
                    self.database_adapter.target("adult", "성인", adult_path)
                )
        for target in targets:
            if should_stop and should_stop():
                break
            with closing(self._connect(target)) as connection:
                if target.db_type == "audiobook":
                    table, alias, field = "audiobooks", "a", "poster"
                elif target.db_type == "video":
                    table, alias, field = "videos", "v", "poster"
                else:
                    table, alias, field = "books", "b", "cover_image"
                if table not in self._tables(connection):
                    continue
                columns = self._columns(connection, table)
                if field not in columns:
                    continue
                conditions = [f"TRIM(COALESCE({alias}.{field}, '')) != ''"]
                if "is_deleted" in columns:
                    conditions.append(
                        f"({alias}.is_deleted = 0 OR {alias}.is_deleted IS NULL)"
                    )
                params = []
                if selected_ids and "library_id" in columns:
                    placeholders = ", ".join("?" for _ in selected_ids)
                    conditions.append(f"{alias}.library_id IN ({placeholders})")
                    params.extend(selected_ids)
                cursor = connection.execute(
                    f"SELECT {alias}.{field} FROM {table} {alias} WHERE "
                    + " AND ".join(conditions),
                    params,
                )
                while True:
                    if should_stop and should_stop():
                        return references
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    for row in rows:
                        value = str(row[0] or "").strip()
                        if value.lower().startswith(("http://", "https://")):
                            continue
                        raw_path = Path(value.split("?", 1)[0]).expanduser()
                        if raw_path.is_absolute():
                            cover_root_value = str(
                                self.settings.get("cover_root_path") or ""
                            ).strip()
                            if not cover_root_value:
                                continue
                            cover_root = Path(cover_root_value).expanduser()
                            try:
                                value = raw_path.resolve().relative_to(
                                    cover_root.resolve()
                                ).as_posix()
                            except ValueError:
                                continue
                        normalized = normalize_cover_path(value)
                        if normalized == "NO_COVER":
                            continue
                        if normalized:
                            references.add(normalized)
                    read_count += len(rows)
                    if on_progress:
                        on_progress(read_count, len(references))
        return references

    def quick_check(self, db_type="general"):
        target = self.get_target(db_type)
        started = datetime.now(timezone.utc)
        try:
            with closing(self._connect(target)) as connection:
                if target.engine == "sqlite":
                    rows = [
                        str(row[0])
                        for row in connection.execute("PRAGMA quick_check").fetchall()
                    ]
                    success = rows == ["ok"]
                    message = (
                        "DB quick_check 결과가 정상입니다."
                        if success
                        else "DB quick_check에서 문제가 발견되었습니다."
                    )
                else:
                    rows = []
                    success = True
                    for table in sorted(connection.tables()):
                        for item in connection.execute(
                            f"CHECK TABLE `{table}` QUICK"
                        ).fetchall():
                            message_type = str(
                                item.get("Msg_type") or item.get("msg_type") or ""
                            )
                            message_text = str(
                                item.get("Msg_text") or item.get("msg_text") or ""
                            )
                            rows.append(f"{table}: {message_type} - {message_text}")
                            if (
                                message_type.lower() not in {"status", "note"}
                                or message_text.lower() != "ok"
                            ):
                                success = False
                    message = (
                        "MariaDB CHECK TABLE QUICK 결과가 정상입니다."
                        if success
                        else "MariaDB CHECK TABLE QUICK에서 문제가 발견되었습니다."
                    )
            return {
                "success": success,
                "db_type": db_type,
                "engine": target.engine,
                "database": target.database,
                "result": rows,
                "duration_ms": round((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                "message": message,
            }
        except (
            OSError,
            sqlite3.Error,
            BookOasisDatabaseError,
            RuntimeError,
            ValueError,
        ) as error:
            return {
                "success": False,
                "db_type": db_type,
                "engine": target.engine,
                "database": target.database,
                "result": [],
                "message": str(error),
            }

    def database_details(self, db_type="general"):
        target = self.get_target(db_type)
        started = datetime.now(timezone.utc)
        try:
            with closing(self._connect(target)) as connection:
                tables = self._tables(connection)
                database_size = connection.database_size()
                engine_details = connection.engine_details()
                libraries = []
                if "libraries" in tables:
                    columns = self._columns(connection, "libraries")
                    if {"id", "name"}.issubset(columns):
                        libraries = [
                            {"id": int(row["id"]), "name": str(row["name"] or "")}
                            for row in connection.execute(
                                "SELECT id, name FROM libraries ORDER BY name, id"
                            ).fetchall()
                        ]
            return {
                "success": True,
                "db_type": target.db_type,
                "media_kind": target.db_type if target.db_type in {"audiobook", "video"} else "book",
                "label": target.label,
                "path": target.path,
                "database": target.database,
                "engine": target.engine,
                "file_size": database_size,
                "libraries": libraries,
                **engine_details,
                "duration_ms": round(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000
                ),
            }
        except (
            OSError,
            sqlite3.Error,
            BookOasisDatabaseError,
            RuntimeError,
            ValueError,
        ) as error:
            return {
                "success": False,
                "db_type": target.db_type,
                "media_kind": target.db_type if target.db_type in {"audiobook", "video"} else "book",
                "label": target.label,
                "path": target.path,
                "database": target.database,
                "engine": target.engine,
                "file_size": 0,
                "libraries": [],
                "message": str(error),
                "duration_ms": round(
                    (datetime.now(timezone.utc) - started).total_seconds() * 1000
                ),
            }
