# BookOasis 라이브러리의 도서·메타데이터·용량 분포를 읽기 전용으로 집계합니다.
import hashlib
import json
import re
import time
from collections import Counter
from contextlib import closing
from datetime import datetime

try:
    from .bookoasis_db import BookOasisDatabaseAdapter
except ImportError:
    from bookoasis_db import BookOasisDatabaseAdapter


BOOK_METADATA_FIELDS = (
    ("cover_image", "표지", "text"),
    ("author", "저자", "text"),
    ("publisher", "출판사", "text"),
    ("summary", "소개", "text"),
    ("genre", "장르", "text"),
    ("tags", "태그", "text"),
    ("isbn", "ISBN", "text"),
    ("total_pages", "페이지 수", "number"),
    ("file_size", "파일 크기", "number"),
)

AUDIOBOOK_METADATA_FIELDS = (
    ("poster", "포스터", "text"),
    ("author", "저자", "text"),
    ("publisher", "출판사", "text"),
    ("description", "소개", "text"),
    ("total_tracks", "트랙 수", "number"),
    ("total_duration", "재생 시간", "number"),
)

SCORE_BUCKETS = (
    (0, 19, "0–19"),
    (20, 39, "20–39"),
    (40, 59, "40–59"),
    (60, 79, "60–79"),
    (80, 100, "80–100"),
)


class LibraryStatisticsCancelled(RuntimeError):
    """사용자가 라이브러리 통계 분석을 중지했음을 나타냅니다."""


def _positive_int(value):
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _positive_number(value):
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _metadata_present(value, value_type):
    if value_type == "number":
        return _positive_number(value)
    return bool(str(value or "").strip())


def _normalize_token(value):
    return " ".join(str(value or "").strip().split())


def _split_tokens(value):
    return [
        token
        for token in (_normalize_token(item) for item in re.split(r"[,;|]+", str(value or "")))
        if token
    ]


def _top_tokens(counter, labels, limit=15):
    rows = []
    sorted_items = sorted(counter.items(), key=lambda item: (-item[1], labels[item[0]].casefold()))
    for key, count in sorted_items[:limit]:
        rows.append({"label": labels[key], "count": int(count)})
    remainder = sum(count for unused_key, count in sorted_items[limit:])
    if remainder:
        rows.append({"label": "기타", "count": int(remainder)})
    return rows


def _year_from_value(value):
    match = re.search(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2})(?!\d)", str(value or ""))
    return match.group(1) if match else ""


class LibraryStatisticsEngine:
    """SQLite와 MariaDB에서 공통으로 동작하는 라이브러리 통계 엔진입니다."""

    def __init__(self, settings, database_adapter=None):
        self.settings = dict(settings or {})
        self.database_adapter = database_adapter or BookOasisDatabaseAdapter(self.settings)

    def _target(self, db_type):
        labels = {"general": "일반", "adult": "성인", "audiobook": "오디오북"}
        db_type = str(db_type or "general").strip().lower()
        if db_type not in labels:
            raise ValueError("지원하지 않는 DB 유형입니다.")
        if db_type == "adult" and not bool(self.settings.get("adult_enabled")):
            raise ValueError("성인 DB 검사가 비활성화되어 있습니다.")
        path_key = {
            "general": "general_db_path",
            "adult": "adult_db_path",
            "audiobook": "audiobook_db_path",
        }[db_type]
        return self.database_adapter.target(db_type, labels[db_type], self.settings.get(path_key))

    @staticmethod
    def _selected_library_id(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            library_id = int(text)
        except (TypeError, ValueError) as error:
            raise ValueError("보관함 ID가 올바르지 않습니다.") from error
        if library_id < 1:
            raise ValueError("보관함 ID가 올바르지 않습니다.")
        return library_id

    @staticmethod
    def _check_cancel(should_stop):
        if should_stop and should_stop():
            raise LibraryStatisticsCancelled("라이브러리 통계 분석을 중지했습니다.")

    @staticmethod
    def _notify(callback, stage, current, total, message):
        if callback:
            callback(stage, int(current or 0), int(total or 0), message)

    def catalog(self, db_type="general"):
        target = self._target(db_type)
        with closing(self.database_adapter.connect(target)) as connection:
            tables = connection.tables()
            libraries = []
            if "libraries" in tables:
                columns = connection.columns("libraries")
                if {"id", "name"}.issubset(columns):
                    libraries = [
                        {"id": int(row["id"]), "name": str(row["name"] or "")}
                        for row in connection.execute(
                            "SELECT id, name FROM libraries ORDER BY name, id"
                        ).fetchall()
                    ]
            return {
                "db_type": target.db_type,
                "engine": target.engine,
                "database": target.database or target.path,
                "media_kind": "audiobook" if "audiobooks" in tables and target.db_type == "audiobook" else "book",
                "libraries": libraries,
            }

    def source_fingerprint(self, db_type="general", library_id=None):
        target = self._target(db_type)
        selected_library_id = self._selected_library_id(library_id)
        with closing(self.database_adapter.connect(target)) as connection:
            tables = connection.tables()
            if target.db_type == "audiobook" and "audiobooks" in tables:
                table = "audiobooks"
                alias = "a"
                progress_table = "audiobook_progress"
                progress_time = "last_listened_at"
            elif "books" in tables:
                table = "books"
                alias = "b"
                progress_table = "user_progress"
                progress_time = "last_read_at"
            else:
                raise RuntimeError("통계 원본 테이블을 찾을 수 없습니다.")
            columns = connection.columns(table)
            where_sql, params = self._where(
                alias,
                "is_deleted" if "is_deleted" in columns else "",
                "library_id" if "library_id" in columns else "",
                selected_library_id,
            )
            version_select = ["COUNT(*) AS source_rows", f"MAX({alias}.id) AS max_id"]
            for column in ("created_at", "cover_updated_at", "file_mtime"):
                if column in columns:
                    version_select.append(f"MAX({alias}.{column}) AS max_{column}")
            row = connection.execute(
                f"SELECT {', '.join(version_select)} FROM {table} {alias} "
                f"WHERE {where_sql}",
                params,
            ).fetchone()
            payload = {key: row[key] for key in row.keys()}
            if "libraries" in tables:
                library_columns = connection.columns("libraries")
                if "last_scanned_at" in library_columns:
                    if selected_library_id is None:
                        library_row = connection.execute(
                            "SELECT MAX(last_scanned_at) AS last_scanned_at FROM libraries"
                        ).fetchone()
                    else:
                        library_row = connection.execute(
                            "SELECT last_scanned_at FROM libraries WHERE id = ?",
                            (selected_library_id,),
                        ).fetchone()
                    payload["last_scanned_at"] = (
                        library_row["last_scanned_at"] if library_row else None
                    )
            if progress_table in tables:
                progress_columns = connection.columns(progress_table)
                if progress_time in progress_columns:
                    progress_row = connection.execute(
                        f"SELECT COUNT(*) AS progress_rows, "
                        f"MAX({progress_time}) AS latest_progress FROM {progress_table}"
                    ).fetchone()
                    payload["progress_rows"] = progress_row["progress_rows"]
                    payload["latest_progress"] = progress_row["latest_progress"]
            if table == "audiobooks" and "audiobook_tracks" in tables:
                track_row = connection.execute(
                    "SELECT COUNT(*) AS track_rows, MAX(id) AS max_track_id "
                    "FROM audiobook_tracks"
                ).fetchone()
                payload["track_rows"] = track_row["track_rows"]
                payload["max_track_id"] = track_row["max_track_id"]

        identity = {
            "engine": target.engine,
            "database": target.database or target.path,
            "db_type": target.db_type,
            "library_id": selected_library_id,
            "version": payload,
        }
        encoded = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
        return {
            **identity,
            "source_rows": int(payload.get("source_rows") or 0),
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
        }

    def analyze(self, db_type="general", library_id=None, on_progress=None, should_stop=None):
        started = time.monotonic()
        target = self._target(db_type)
        selected_library_id = self._selected_library_id(library_id)
        self._notify(on_progress, "connect", 0, 100, "BookOasis DB에 연결하고 있습니다.")
        with closing(self.database_adapter.connect(target)) as connection:
            tables = connection.tables()
            self._check_cancel(should_stop)
            if target.db_type == "audiobook" and "audiobooks" in tables:
                result = self._analyze_audiobooks(
                    connection,
                    target,
                    selected_library_id,
                    on_progress,
                    should_stop,
                )
            elif "books" in tables:
                result = self._analyze_books(
                    connection,
                    target,
                    selected_library_id,
                    on_progress,
                    should_stop,
                )
            else:
                raise RuntimeError("통계를 계산할 도서 테이블이 없습니다.")
        result["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        result["duration_ms"] = round((time.monotonic() - started) * 1000)
        self._notify(on_progress, "complete", 100, 100, "라이브러리 통계 분석을 완료했습니다.")
        return result

    def _resolve_library(self, connection, library_id):
        if library_id is None:
            return "전체 보관함"
        tables = connection.tables()
        if "libraries" not in tables or not {"id", "name"}.issubset(connection.columns("libraries")):
            raise ValueError("보관함 필터를 지원하지 않는 DB 스키마입니다.")
        row = connection.execute(
            "SELECT name FROM libraries WHERE id = ?",
            (library_id,),
        ).fetchone()
        if row is None:
            raise ValueError("선택한 보관함을 찾을 수 없습니다.")
        return str(row["name"] or f"보관함 {library_id}")

    @staticmethod
    def _where(alias, active_column, library_column, library_id):
        conditions = []
        params = []
        if active_column:
            conditions.append(
                f"({alias}.{active_column} = 0 OR {alias}.{active_column} IS NULL)"
            )
        if library_id is not None:
            if not library_column:
                raise ValueError("보관함 필터를 지원하지 않는 DB 스키마입니다.")
            conditions.append(f"{alias}.{library_column} = ?")
            params.append(library_id)
        return " AND ".join(conditions) or "1 = 1", params

    @staticmethod
    def _library_distribution(connection, table, alias, where_sql, params, size_expr):
        tables = connection.tables()
        columns = connection.columns(table)
        if "libraries" not in tables or "library_id" not in columns:
            return []
        library_columns = connection.columns("libraries")
        if not {"id", "name"}.issubset(library_columns):
            return []
        rows = connection.execute(
            f"""
            SELECT l.id, l.name, COUNT(*) AS item_count, {size_expr} AS size_bytes
            FROM {table} {alias}
            JOIN libraries l ON l.id = {alias}.library_id
            WHERE {where_sql}
            GROUP BY l.id, l.name
            ORDER BY item_count DESC, l.name
            """,
            params,
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"] or ""),
                "count": int(row["item_count"] or 0),
                "size_bytes": int(row["size_bytes"] or 0),
            }
            for row in rows
        ]

    @staticmethod
    def _added_timeline(connection, table, alias, columns, where_sql, params):
        if "created_at" not in columns:
            return []
        rows = connection.execute(
            f"""
            SELECT SUBSTR({alias}.created_at, 1, 7) AS period, COUNT(*) AS item_count
            FROM {table} {alias}
            WHERE {where_sql} AND {alias}.created_at IS NOT NULL
            GROUP BY SUBSTR({alias}.created_at, 1, 7)
            ORDER BY period
            """,
            params,
        ).fetchall()
        return [
            {"period": str(row["period"] or ""), "count": int(row["item_count"] or 0)}
            for row in rows
            if re.fullmatch(r"\d{4}-\d{2}", str(row["period"] or ""))
        ][-60:]

    @staticmethod
    def _progress_funnel(connection, media_kind, where_sql, params):
        if media_kind == "audiobook":
            table = "audiobook_progress"
            item_table = "audiobooks"
            item_alias = "a"
            item_key = "audiobook_id"
        else:
            table = "user_progress"
            item_table = "books"
            item_alias = "b"
            item_key = "book_id"
        if table not in connection.tables():
            return {"not_started": 0, "in_progress": 0, "completed": 0, "started": 0}
        progress_columns = connection.columns(table)
        activity_parts = ["COALESCE(p.is_completed, 0) = 1"]
        if media_kind == "audiobook":
            if "current_time" in progress_columns:
                activity_parts.append("COALESCE(p.current_time, 0) > 0")
            if "total_progress_pct" in progress_columns:
                activity_parts.append("COALESCE(p.total_progress_pct, 0) > 0")
        elif "pages_read" in progress_columns:
            activity_parts.append("COALESCE(p.pages_read, 0) > 0")
        activity = "(" + " OR ".join(activity_parts) + ")"
        total_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM {item_table} {item_alias} WHERE {where_sql}",
            params,
        ).fetchone()
        total = int(total_row["total"] or 0)
        row = connection.execute(
            f"""
            SELECT
                SUM(CASE WHEN progress.completed > 0 THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN progress.completed = 0 THEN 1 ELSE 0 END) AS in_progress
            FROM (
                SELECT p.{item_key} AS item_id, MAX(COALESCE(p.is_completed, 0)) AS completed
                FROM {table} p
                JOIN {item_table} {item_alias} ON {item_alias}.id = p.{item_key}
                WHERE {where_sql} AND {activity}
                GROUP BY p.{item_key}
            ) progress
            """,
            params,
        ).fetchone()
        completed = int(row["completed"] or 0)
        in_progress = int(row["in_progress"] or 0)
        started = completed + in_progress
        return {
            "not_started": max(0, total - started),
            "in_progress": in_progress,
            "completed": completed,
            "started": started,
        }

    @staticmethod
    def _series_count(connection, library_id):
        tables = connection.tables()
        if {"series_summary", "series_summary_state"}.issubset(tables):
            state_columns = connection.columns("series_summary_state")
            summary_columns = connection.columns("series_summary")
            if "is_ready" in state_columns and "library_id" in summary_columns:
                state = connection.execute(
                    "SELECT is_ready FROM series_summary_state WHERE id = 1"
                ).fetchone()
                if state is not None and int(state["is_ready"] or 0) == 1:
                    if library_id is None:
                        row = connection.execute(
                            "SELECT COUNT(*) AS series_count FROM series_summary"
                        ).fetchone()
                    else:
                        row = connection.execute(
                            "SELECT COUNT(*) AS series_count "
                            "FROM series_summary WHERE library_id = ?",
                            (library_id,),
                        ).fetchone()
                    return int(row["series_count"] or 0)

        book_columns = connection.columns("books")
        conditions = (
            ["(b.is_deleted = 0 OR b.is_deleted IS NULL)"]
            if "is_deleted" in book_columns
            else ["1 = 1"]
        )
        params = []
        if library_id is not None:
            conditions.append("b.library_id = ?")
            params.append(library_id)
        series_key = (
            "COALESCE(NULLIF(b.series_name, ''), b.title)"
            if "series_name" in book_columns
            else "b.title"
        )
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS series_count
            FROM (
                SELECT 1
                FROM books b
                WHERE {' AND '.join(conditions)}
                GROUP BY {('b.library_id' if 'library_id' in book_columns else '0')},
                         {series_key}
            ) AS grouped_series
            """,
            params,
        ).fetchone()
        return int(row["series_count"] or 0)

    def _metadata_stream(
        self,
        connection,
        table,
        alias,
        columns,
        fields,
        where_sql,
        params,
        total,
        on_progress,
        should_stop,
        include_taxonomy=False,
        year_column="",
        distinct_fields=(),
    ):
        available = [field for field in fields if field[0] in columns]
        if not available:
            return [], [], [], [], [], {}
        select_columns = [f"{alias}.{name} AS {name}" for name, unused_label, unused_type in available]
        if year_column and year_column in columns and year_column not in {field[0] for field in available}:
            select_columns.append(f"{alias}.{year_column} AS {year_column}")
        cursor = connection.execute(
            f"SELECT {', '.join(select_columns)} FROM {table} {alias} WHERE {where_sql}",
            params,
        )
        score_counts = Counter()
        missing_counts = Counter()
        genre_counter = Counter()
        genre_labels = {}
        tag_counter = Counter()
        tag_labels = {}
        year_counter = Counter()
        distinct_values = {
            name: set()
            for name in distinct_fields
            if name in columns
        }
        current = 0
        denominator = max(1, len(available))
        while True:
            rows = cursor.fetchmany(2000)
            if not rows:
                break
            for row in rows:
                present = 0
                for name, label, value_type in available:
                    if _metadata_present(row.get(name), value_type):
                        present += 1
                    else:
                        missing_counts[label] += 1
                score = round(present / denominator * 100)
                for minimum, maximum, bucket_label in SCORE_BUCKETS:
                    if minimum <= score <= maximum:
                        score_counts[bucket_label] += 1
                        break
                if include_taxonomy:
                    for token in _split_tokens(row.get("genre")):
                        key = token.casefold()
                        genre_labels.setdefault(key, token)
                        genre_counter[key] += 1
                    for token in _split_tokens(row.get("tags")):
                        key = token.casefold()
                        tag_labels.setdefault(key, token)
                        tag_counter[key] += 1
                if year_column:
                    year = _year_from_value(row.get(year_column))
                    if year:
                        year_counter[year] += 1
                for name, values in distinct_values.items():
                    value = str(row.get(name) or "").strip()
                    if value:
                        values.add(value)
            current += len(rows)
            self._check_cancel(should_stop)
            percent = 20 + round((current / max(1, total)) * 50)
            self._notify(
                on_progress,
                "metadata",
                percent,
                100,
                f"메타데이터 {current:,}/{total:,}건을 분석했습니다.",
            )
        score_rows = [
            {"label": label, "count": int(score_counts[label])}
            for unused_minimum, unused_maximum, label in SCORE_BUCKETS
        ]
        missing_rows = [
            {"label": label, "count": int(missing_counts[label])}
            for unused_name, label, unused_type in available
        ]
        year_rows = [
            {"label": year, "count": int(count)}
            for year, count in sorted(year_counter.items())
        ]
        return (
            score_rows,
            missing_rows,
            _top_tokens(genre_counter, genre_labels),
            _top_tokens(tag_counter, tag_labels, limit=20),
            year_rows,
            {name: len(values) for name, values in distinct_values.items()},
        )

    def _analyze_books(self, connection, target, library_id, on_progress, should_stop):
        columns = connection.columns("books")
        active_column = "is_deleted" if "is_deleted" in columns else ""
        library_column = "library_id" if "library_id" in columns else ""
        where_sql, params = self._where("b", active_column, library_column, library_id)
        library_name = self._resolve_library(connection, library_id)
        storage = "COALESCE(SUM(COALESCE(b.file_size, 0)), 0)" if "file_size" in columns else "0"
        current_year = str(datetime.now().year)
        added_this_year = (
            "SUM(CASE WHEN b.created_at >= ? THEN 1 ELSE 0 END)"
            if "created_at" in columns
            else "0"
        )
        summary_params = []
        if "created_at" in columns:
            summary_params.append(f"{current_year}-01-01 00:00:00")
        summary_params.extend(params)
        self._notify(on_progress, "summary", 3, 100, "도서 수와 저장 용량을 집계하고 있습니다.")
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total_items,
                   0 AS total_authors,
                   0 AS total_publishers,
                   {storage} AS storage_bytes,
                   {added_this_year} AS added_this_year
            FROM books b
            WHERE {where_sql}
            """,
            summary_params,
        ).fetchone()
        summary = {key: int(row[key] or 0) for key in row.keys()}
        summary["total_series"] = self._series_count(connection, library_id)
        total = summary["total_items"]
        self._notify(on_progress, "distribution", 10, 100, "포맷과 보관함 분포를 집계하고 있습니다.")
        format_rows = []
        if "file_format" in columns:
            size_expr = "COALESCE(SUM(COALESCE(b.file_size, 0)), 0)" if "file_size" in columns else "0"
            format_rows = [
                {
                    "label": str(item["format_name"] or "알 수 없음").upper(),
                    "count": int(item["item_count"] or 0),
                    "size_bytes": int(item["size_bytes"] or 0),
                }
                for item in connection.execute(
                    f"""
                    SELECT LOWER(TRIM(COALESCE(b.file_format, ''))) AS format_name,
                           COUNT(*) AS item_count, {size_expr} AS size_bytes
                    FROM books b
                    WHERE {where_sql}
                    GROUP BY LOWER(TRIM(COALESCE(b.file_format, '')))
                    ORDER BY item_count DESC, format_name
                    """,
                    params,
                ).fetchall()
            ]
        library_rows = self._library_distribution(
            connection,
            "books",
            "b",
            where_sql,
            params,
            "COALESCE(SUM(COALESCE(b.file_size, 0)), 0)" if "file_size" in columns else "0",
        )
        self._check_cancel(should_stop)
        metadata, missing, genres, tags, years, distinct_counts = self._metadata_stream(
            connection,
            "books",
            "b",
            columns,
            BOOK_METADATA_FIELDS,
            where_sql,
            params,
            total,
            on_progress,
            should_stop,
            include_taxonomy=True,
            year_column="release_date",
            distinct_fields=("author", "publisher"),
        )
        summary["total_authors"] = int(distinct_counts.get("author") or 0)
        summary["total_publishers"] = int(distinct_counts.get("publisher") or 0)
        self._notify(on_progress, "ranking", 80, 100, "대용량 도서와 독서 진행 상태를 집계하고 있습니다.")
        title_expr = "b.title" if "title" in columns else "''"
        series_expr = "b.series_name" if "series_name" in columns else "''"
        size_expr = "COALESCE(b.file_size, 0)" if "file_size" in columns else "0"
        library_join = ""
        library_name_expr = "''"
        if "libraries" in connection.tables() and "library_id" in columns:
            library_columns = connection.columns("libraries")
            if {"id", "name"}.issubset(library_columns):
                library_join = "LEFT JOIN libraries l ON l.id = b.library_id"
                library_name_expr = "COALESCE(l.name, '')"
        largest = [
            {
                "id": int(item["id"]),
                "title": str(item["title"] or ""),
                "series_name": str(item["series_name"] or ""),
                "library_name": str(item["library_name"] or ""),
                "size_bytes": int(item["size_bytes"] or 0),
            }
            for item in connection.execute(
                f"""
                SELECT b.id, {title_expr} AS title, {series_expr} AS series_name,
                       {library_name_expr} AS library_name, {size_expr} AS size_bytes
                FROM books b
                {library_join}
                WHERE {where_sql}
                ORDER BY {size_expr} DESC, b.id
                LIMIT 20
                """,
                params,
            ).fetchall()
        ]
        progress = self._progress_funnel(connection, "book", where_sql, params)
        timeline = self._added_timeline(connection, "books", "b", columns, where_sql, params)
        self._check_cancel(should_stop)
        return {
            "db_type": target.db_type,
            "engine": target.engine,
            "database": target.database or target.path,
            "media_kind": "book",
            "library_id": library_id,
            "library_name": library_name,
            "summary": summary,
            "formats": format_rows,
            "libraries": library_rows,
            "metadata_scores": metadata,
            "metadata_missing": missing,
            "genres": genres,
            "tags": tags,
            "publication_years": years,
            "added_over_time": timeline,
            "largest_items": largest,
            "progress": progress,
        }

    def _analyze_audiobooks(self, connection, target, library_id, on_progress, should_stop):
        columns = connection.columns("audiobooks")
        active_column = "is_deleted" if "is_deleted" in columns else ""
        library_column = "library_id" if "library_id" in columns else ""
        where_sql, params = self._where("a", active_column, library_column, library_id)
        library_name = self._resolve_library(connection, library_id)
        current_year = str(datetime.now().year)
        added_this_year = (
            "SUM(CASE WHEN a.created_at >= ? THEN 1 ELSE 0 END)"
            if "created_at" in columns
            else "0"
        )
        summary_params = []
        if "created_at" in columns:
            summary_params.append(f"{current_year}-01-01 00:00:00")
        summary_params.extend(params)
        self._notify(on_progress, "summary", 3, 100, "오디오북 수와 재생 시간을 집계하고 있습니다.")
        row = connection.execute(
            f"""
            SELECT COUNT(*) AS total_items,
                   0 AS total_series,
                   0 AS total_authors,
                   0 AS total_publishers,
                   {added_this_year} AS added_this_year,
                   {('COALESCE(SUM(COALESCE(a.total_duration, 0)), 0)' if 'total_duration' in columns else '0')} AS total_duration
            FROM audiobooks a
            WHERE {where_sql}
            """,
            summary_params,
        ).fetchone()
        summary = {key: int(float(row[key] or 0)) for key in row.keys()}
        total = summary["total_items"]
        summary["total_tracks"] = 0
        summary["storage_bytes"] = 0
        format_rows = []
        if "audiobook_tracks" in connection.tables():
            track_columns = connection.columns("audiobook_tracks")
            track_filter = where_sql
            track_params = list(params)
            track_size = "COALESCE(t.file_size, 0)" if "file_size" in track_columns else "0"
            track_format = "LOWER(TRIM(COALESCE(t.format, '')))" if "format" in track_columns else "''"
            format_rows = [
                {
                    "label": str(item["format_name"] or "알 수 없음").upper(),
                    "count": int(item["item_count"] or 0),
                    "size_bytes": int(item["size_bytes"] or 0),
                }
                for item in connection.execute(
                    f"""
                    SELECT {track_format} AS format_name, COUNT(*) AS item_count,
                           COALESCE(SUM({track_size}), 0) AS size_bytes
                    FROM audiobook_tracks t
                    JOIN audiobooks a ON a.id = t.audiobook_id
                    WHERE {track_filter}
                    GROUP BY {track_format}
                    ORDER BY item_count DESC, format_name
                    """,
                    track_params,
                ).fetchall()
            ]
            summary["total_tracks"] = sum(item["count"] for item in format_rows)
            summary["storage_bytes"] = sum(
                item["size_bytes"] for item in format_rows
            )
        self._notify(on_progress, "distribution", 15, 100, "오디오 포맷과 보관함 분포를 집계했습니다.")
        library_rows = []
        if "libraries" in connection.tables() and "library_id" in columns:
            library_columns = connection.columns("libraries")
            if {"id", "name"}.issubset(library_columns):
                track_join = ""
                size_expr = "0"
                if "audiobook_tracks" in connection.tables():
                    track_columns = connection.columns("audiobook_tracks")
                    if "file_size" in track_columns:
                        track_join = """
                            LEFT JOIN (
                                SELECT audiobook_id,
                                       SUM(COALESCE(file_size, 0)) AS size_bytes
                                FROM audiobook_tracks
                                GROUP BY audiobook_id
                            ) track_sizes ON track_sizes.audiobook_id = a.id
                        """
                        size_expr = "COALESCE(SUM(track_sizes.size_bytes), 0)"
                library_rows = [
                    {
                        "id": int(item["id"]),
                        "name": str(item["name"] or ""),
                        "count": int(item["item_count"] or 0),
                        "size_bytes": int(item["size_bytes"] or 0),
                    }
                    for item in connection.execute(
                        f"""
                        SELECT l.id, l.name, COUNT(*) AS item_count,
                               {size_expr} AS size_bytes
                        FROM audiobooks a
                        JOIN libraries l ON l.id = a.library_id
                        {track_join}
                        WHERE {where_sql}
                        GROUP BY l.id, l.name
                        ORDER BY item_count DESC, l.name
                        """,
                        params,
                    ).fetchall()
                ]
        metadata, missing, unused_genres, unused_tags, years, distinct_counts = self._metadata_stream(
            connection,
            "audiobooks",
            "a",
            columns,
            AUDIOBOOK_METADATA_FIELDS,
            where_sql,
            params,
            total,
            on_progress,
            should_stop,
            include_taxonomy=False,
            year_column="premiered",
            distinct_fields=("author", "publisher"),
        )
        summary["total_authors"] = int(distinct_counts.get("author") or 0)
        summary["total_publishers"] = int(distinct_counts.get("publisher") or 0)
        self._notify(on_progress, "ranking", 80, 100, "대용량 오디오북과 청취 상태를 집계하고 있습니다.")
        library_join = ""
        library_name_expr = "''"
        if "libraries" in connection.tables() and "library_id" in columns:
            library_columns = connection.columns("libraries")
            if {"id", "name"}.issubset(library_columns):
                library_join = "LEFT JOIN libraries l ON l.id = a.library_id"
                library_name_expr = "COALESCE(l.name, '')"
        if "audiobook_tracks" in connection.tables():
            track_columns = connection.columns("audiobook_tracks")
            size_expr = "COALESCE(SUM(COALESCE(t.file_size, 0)), 0)" if "file_size" in track_columns else "0"
            largest_rows = connection.execute(
                f"""
                SELECT a.id, a.title, {library_name_expr} AS library_name, {size_expr} AS size_bytes
                FROM audiobooks a
                {library_join}
                LEFT JOIN audiobook_tracks t ON t.audiobook_id = a.id
                WHERE {where_sql}
                GROUP BY a.id, a.title, {library_name_expr}
                ORDER BY size_bytes DESC, a.id
                LIMIT 20
                """,
                params,
            ).fetchall()
        else:
            largest_rows = connection.execute(
                f"""
                SELECT a.id, a.title, {library_name_expr} AS library_name, 0 AS size_bytes
                FROM audiobooks a
                {library_join}
                WHERE {where_sql}
                ORDER BY a.id
                LIMIT 20
                """,
                params,
            ).fetchall()
        largest = [
            {
                "id": int(item["id"]),
                "title": str(item["title"] or ""),
                "series_name": "",
                "library_name": str(item["library_name"] or ""),
                "size_bytes": int(item["size_bytes"] or 0),
            }
            for item in largest_rows
        ]
        progress = self._progress_funnel(connection, "audiobook", where_sql, params)
        timeline = self._added_timeline(connection, "audiobooks", "a", columns, where_sql, params)
        self._check_cancel(should_stop)
        return {
            "db_type": target.db_type,
            "engine": target.engine,
            "database": target.database or target.path,
            "media_kind": "audiobook",
            "library_id": library_id,
            "library_name": library_name,
            "summary": summary,
            "formats": format_rows,
            "libraries": library_rows,
            "metadata_scores": metadata,
            "metadata_missing": missing,
            "genres": [],
            "tags": [],
            "publication_years": years,
            "added_over_time": timeline,
            "largest_items": largest,
            "progress": progress,
        }
