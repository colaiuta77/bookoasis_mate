# BookOasis DB를 읽기 전용으로 검사하고 진단 결과를 생성합니다.
import time
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

try:
    from .bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError
    from .cover_inspector import normalize_cover_path
    from .series_gap import find_series_gaps
except ImportError:
    from bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError
    from cover_inspector import normalize_cover_path
    from series_gap import find_series_gaps


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

    def _book_query_parts(self, connection):
        tables = self._tables(connection)
        if "books" not in tables:
            raise RuntimeError("books 테이블이 없습니다.")

        columns = self._columns(connection, "books")
        required = {"id", "title"}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError("books 필수 컬럼이 없습니다: " + ", ".join(missing))

        active = "COALESCE(b.is_deleted, 0) = 0" if "is_deleted" in columns else "1 = 1"
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
        if "isbn" in columns:
            duplicate_cte = """
                WITH isbn_counts AS (
                    SELECT TRIM(isbn) AS normalized_isbn, COUNT(*) AS item_count
                    FROM books
                    WHERE TRIM(COALESCE(isbn, '')) != ''
                      AND {active_unaliased}
                    GROUP BY TRIM(isbn)
                    HAVING COUNT(*) > 1
                )
            """.format(
                active_unaliased="COALESCE(is_deleted, 0) = 0" if "is_deleted" in columns else "1 = 1"
            )
            duplicate_join = "LEFT JOIN isbn_counts ic ON ic.normalized_isbn = TRIM(COALESCE(b.isbn, ''))"
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
        parts = self._book_query_parts(connection)
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
            {parts['duplicate_cte']}
            SELECT {', '.join(select_items)}
            FROM books b
            {parts['duplicate_join']}
            {parts['library_join']}
            WHERE {parts['active']}
        """
        row = connection.execute(query).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def _audiobook_summary(self, connection):
        tables = self._tables(connection)
        if "audiobooks" not in tables:
            raise RuntimeError("audiobooks 테이블이 없습니다.")
        columns = self._columns(connection, "audiobooks")
        active = "COALESCE(a.is_deleted, 0) = 0" if "is_deleted" in columns else "1 = 1"

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
            result["total_tracks"] = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM audiobook_tracks t
                    JOIN audiobooks a ON a.id = t.audiobook_id
                    WHERE {active}
                    """
                ).fetchone()[0]
                or 0
            )
            if "file_size" in track_columns:
                result["track_file_size_missing"] = int(
                    connection.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM audiobook_tracks t
                        JOIN audiobooks a ON a.id = t.audiobook_id
                        WHERE {active} AND COALESCE(t.file_size, 0) <= 0
                        """
                    ).fetchone()[0]
                    or 0
                )
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
                    "sql": "COALESCE(books.is_deleted, 0) = 0",
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
                media_kind = "audiobook" if target.db_type == "audiobook" else "book"
                summary = (
                    self._audiobook_summary(connection)
                    if media_kind == "audiobook"
                    else self._book_summary(connection)
                )
                scanner = self._scanner_summary(connection)
                engine_details = connection.engine_details()
                database_size = connection.database_size()
            problem_count = summary[
                "problem_audiobooks" if media_kind == "audiobook" else "problem_books"
            ]
            status = "healthy" if problem_count == 0 and scanner["failed_libraries"] == 0 and scanner["recent_failed_tasks"] == 0 else "warning"
            status_reasons = []
            if problem_count:
                status_reasons.append({
                    "code": "problem_audiobooks" if media_kind == "audiobook" else "problem_books",
                    "label": (
                        "점검이 필요한 오디오북"
                        if media_kind == "audiobook"
                        else "점검이 필요한 도서"
                    ),
                    "count": problem_count,
                    "unit": "개" if media_kind == "audiobook" else "권",
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
                "media_kind": "audiobook" if target.db_type == "audiobook" else "book",
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

    def build_report(self):
        databases = [self.inspect_target(target) for target in self.targets()]
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
        for database in databases:
            if database.get("media_kind") == "audiobook":
                for key in audiobook_totals:
                    audiobook_totals[key] += int(
                        database.get("summary", {}).get(key, 0) or 0
                    )
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
            or scanner_totals["failed_libraries"]
            or scanner_totals["recent_failed_tasks"]
        ):
            status = "warning"
        else:
            status = "healthy"
        healthy_books = max(0, totals["total_books"] - totals["problem_books"])
        totals["healthy_books"] = healthy_books
        totals["health_percent"] = round(healthy_books / totals["total_books"] * 100) if totals["total_books"] else 0
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "database_engine": self.database_adapter.public_info(),
            "status": status,
            "databases": databases,
            "totals": totals,
            "scanner": scanner_totals,
            "audiobook": audiobook_totals,
            "stale_days": self.stale_days,
            "check_missing_isbn": self.check_missing_isbn,
        }

    def list_issues(self, db_type="general", library_id=None, issue_type="all", search="", page=1, page_size=50):
        target = self.get_target(db_type)
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size, 50, 10, 200)
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

        with closing(self._connect(target)) as connection:
            parts = self._book_query_parts(connection)
            expressions = {
                key: value
                for key, value in parts["expressions"].items()
                if key in PROBLEM_BOOK_LABELS
            }
            if issue_type != "all" and issue_type not in expressions:
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 0}
            if selected_library_id is not None and "library_id" not in parts["columns"]:
                return {"items": [], "total": 0, "page": page, "page_size": page_size, "pages": 0}

            conditions = [parts["active"]]
            conditions.append(expressions[issue_type] if issue_type != "all" else "(" + " OR ".join(expressions.values()) + ")")
            params = []
            if selected_library_id is not None:
                conditions.append("b.library_id = ?")
                params.append(selected_library_id)
            if search:
                search_fields = [f"COALESCE(b.{name}, '')" for name in ("title", "series_name", "author") if name in parts["columns"]]
                if search_fields:
                    conditions.append("(" + " OR ".join(f"{field} LIKE ?" for field in search_fields) + ")")
                    params.extend([f"%{search}%"] * len(search_fields))

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
            where = " AND ".join(conditions)
            base_from = f"FROM books b {parts['duplicate_join']} {parts['library_join']}"
            total = connection.execute(
                f"{parts['duplicate_cte']} SELECT COUNT(*) {base_from} WHERE {where}",
                params,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                {parts['duplicate_cte']}
                SELECT {', '.join(value_select)}, {parts['library_name']},
                       {parts['duplicate_count']} AS isbn_count,
                       {', '.join(flag_select)}
                {base_from}
                WHERE {where}
                ORDER BY {('b.created_at DESC,' if 'created_at' in parts['columns'] else '')} b.id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()

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
        pages = (int(total) + page_size - 1) // page_size
        return {"items": items, "total": int(total), "page": page, "page_size": page_size, "pages": pages}

    def issue_book_ids(self, db_type="general", library_id=None, issue_type="all", search=""):
        target = self.get_target(db_type)
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

        with closing(self._connect(target)) as connection:
            parts = self._book_query_parts(connection)
            expressions = {
                key: value
                for key, value in parts["expressions"].items()
                if key in PROBLEM_BOOK_LABELS
            }
            if issue_type != "all" and issue_type not in expressions:
                return []
            if selected_library_id is not None and "library_id" not in parts["columns"]:
                return []

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

            rows = connection.execute(
                f"""
                {parts['duplicate_cte']}
                SELECT b.id
                FROM books b
                {parts['duplicate_join']}
                {parts['library_join']}
                WHERE {' AND '.join(conditions)}
                ORDER BY b.id
                """,
                params,
            )
            return [int(row[0]) for row in rows if int(row[0] or 0) > 0]

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
            rows = [dict(row) for row in connection.execute(
                f"""
                SELECT b.id, b.title, b.series_name, {library_id_expr} AS library_id,
                       {file_path} AS file_path, {cover_image}, {parts['library_name']}
                FROM books b
                {parts['library_join']}
                WHERE {' AND '.join(conditions)}
                ORDER BY b.series_name, b.id
                """,
                params,
            ).fetchall()]
        items = find_series_gaps(rows)
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
            "analyzed_books": len(rows),
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
                parts = self._book_query_parts(connection)
                if "cover_image" not in parts["columns"]:
                    continue
                conditions = [
                    parts["active"],
                    "TRIM(COALESCE(cover_image, '')) != ''",
                ]
                params = []
                if selected_ids:
                    if "library_id" in parts["columns"]:
                        placeholders = ", ".join("?" for _ in selected_ids)
                        conditions.append(f"b.library_id IN ({placeholders})")
                        params.extend(selected_ids)
                cursor = connection.execute(
                    "SELECT cover_image FROM books b WHERE "
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
                        normalized = normalize_cover_path(row[0])
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
                "media_kind": "audiobook" if target.db_type == "audiobook" else "book",
                "label": target.label,
                "path": target.path,
                "database": target.database,
                "engine": target.engine,
                "file_size": database_size,
                "libraries": libraries,
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
                "media_kind": "audiobook" if target.db_type == "audiobook" else "book",
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
