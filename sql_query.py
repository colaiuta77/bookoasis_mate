# BookOasis SQLite와 MariaDB에 제한된 읽기 전용 SQL 진단 기능을 제공합니다.
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

try:
    from .bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError
except ImportError:
    from bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError


SENSITIVE_COLUMNS = {
    ("users", "password_hash"),
    ("settings", "value"),
}

FORBIDDEN_KEYWORDS = {
    "ALTER",
    "ANALYZE",
    "ATTACH",
    "CREATE",
    "DELETE",
    "DETACH",
    "DROP",
    "INSERT",
    "PRAGMA",
    "REINDEX",
    "REPLACE",
    "SAVEPOINT",
    "TRANSACTION",
    "UPDATE",
    "VACUUM",
}

DENIED_AUTHORIZER_ACTIONS = {
    value
    for name in (
        "SQLITE_ATTACH",
        "SQLITE_DETACH",
        "SQLITE_INSERT",
        "SQLITE_UPDATE",
        "SQLITE_DELETE",
        "SQLITE_ALTER_TABLE",
        "SQLITE_TRANSACTION",
        "SQLITE_SAVEPOINT",
    )
    if (value := getattr(sqlite3, name, None)) is not None
}


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

SQL_PRESETS = [
    {
        "id": "recent_progress_diagnosis",
        "name": "최근 진행 기록 사용자·권한 진단",
        "description": "최근 읽기 기록이 사용자와 보관함 권한에 정상 연결되는지 확인합니다.",
        "sql": """SELECT
    p.id AS progress_id,
    p.user_id,
    u.username,
    b.id AS book_id,
    b.title,
    l.name AS library_name,
    p.pages_read,
    p.last_read_at,
    CASE
        WHEN u.id IS NULL THEN 'USER_MISSING'
        WHEN perm.id IS NULL THEN 'PERMISSION_MISSING'
        WHEN COALESCE(perm.has_access, 0) != 1 THEN 'ACCESS_DENIED'
        ELSE 'OK'
    END AS diagnosis
FROM user_progress p
JOIN books b ON b.id = p.book_id
LEFT JOIN libraries l ON l.id = b.library_id
LEFT JOIN users u ON u.id = p.user_id
LEFT JOIN user_category_permissions perm
    ON perm.user_id = p.user_id
   AND perm.library_id = b.library_id
ORDER BY p.last_read_at DESC""",
    },
    {
        "id": "orphan_progress_users",
        "name": "존재하지 않는 사용자 진행 기록",
        "description": "users에 없는 user_id를 참조하는 읽기 기록을 사용자별로 집계합니다.",
        "sql": """SELECT
    p.user_id,
    COUNT(*) AS progress_count,
    MAX(p.last_read_at) AS latest_read_at
FROM user_progress p
LEFT JOIN users u ON u.id = p.user_id
WHERE u.id IS NULL
GROUP BY p.user_id
ORDER BY progress_count DESC, p.user_id""",
    },
    {
        "id": "permission_problems",
        "name": "읽기 기록 보관함 권한 누락",
        "description": "유효한 사용자의 읽기 기록 중 보관함 권한이 없거나 차단된 항목을 집계합니다.",
        "sql": """SELECT
    u.id AS user_id,
    u.username,
    l.id AS library_id,
    l.name AS library_name,
    CASE
        WHEN perm.id IS NULL THEN 'PERMISSION_MISSING'
        ELSE 'ACCESS_DENIED'
    END AS diagnosis,
    COUNT(*) AS progress_count,
    MAX(p.last_read_at) AS latest_read_at
FROM user_progress p
JOIN users u ON u.id = p.user_id
JOIN books b ON b.id = p.book_id
LEFT JOIN libraries l ON l.id = b.library_id
LEFT JOIN user_category_permissions perm
    ON perm.user_id = p.user_id
   AND perm.library_id = b.library_id
WHERE perm.id IS NULL OR COALESCE(perm.has_access, 0) != 1
GROUP BY u.id, u.username, l.id, l.name, diagnosis
ORDER BY latest_read_at DESC""",
    },
    {
        "id": "users",
        "name": "사용자 ID 목록",
        "description": "비밀번호 해시를 제외한 사용자 ID와 권한 정보를 확인합니다.",
        "sql": """SELECT
    id,
    username,
    role,
    has_adult_access,
    created_at
FROM users
ORDER BY id""",
    },
    {
        "id": "library_book_counts",
        "name": "보관함별 도서 수와 스캔 상태",
        "description": "보관함별 활성·삭제 도서 수와 마지막 스캔 상태를 확인합니다.",
        "sql": """SELECT
    l.id AS library_id,
    l.name AS library_name,
    l.physical_path,
    l.scan_status,
    l.last_scanned_at,
    SUM(CASE WHEN COALESCE(b.is_deleted, 0) = 0 THEN 1 ELSE 0 END) AS active_books,
    SUM(CASE WHEN COALESCE(b.is_deleted, 0) != 0 THEN 1 ELSE 0 END) AS deleted_books
FROM libraries l
LEFT JOIN books b ON b.library_id = l.id
GROUP BY l.id, l.name, l.physical_path, l.scan_status, l.last_scanned_at
ORDER BY l.id""",
    },
    {
        "id": "recent_reading_progress",
        "name": "최근 읽은 도서",
        "description": "사용자별 최근 읽은 도서와 저장된 페이지 진행률을 확인합니다.",
        "sql": """SELECT
    p.id AS progress_id,
    u.username,
    b.id AS book_id,
    b.title,
    l.name AS library_name,
    p.pages_read,
    b.total_pages,
    p.is_completed,
    p.last_read_at
FROM user_progress p
JOIN users u ON u.id = p.user_id
JOIN books b ON b.id = p.book_id
LEFT JOIN libraries l ON l.id = b.library_id
ORDER BY p.last_read_at DESC""",
    },
    {
        "id": "recent_scanner_tasks",
        "name": "최근 스캐너 작업",
        "description": "최근 스캐너 작업의 상태·단계·오류를 확인합니다.",
        "sql": """SELECT
    id,
    task_type,
    task_key,
    status,
    stage,
    enqueue_at,
    started_at,
    finished_at,
    error_message
FROM scanner_tasks
ORDER BY id DESC""",
    },
    {
        "id": "unrecorded_file_metrics",
        "name": "페이지·파일 크기 미기록 도서",
        "description": "페이지 수 또는 파일 크기가 0 이하인 활성 도서를 확인합니다.",
        "sql": """SELECT
    b.id AS book_id,
    b.title,
    b.series_name,
    l.name AS library_name,
    b.total_pages,
    b.file_size,
    b.file_path
FROM books b
LEFT JOIN libraries l ON l.id = b.library_id
WHERE COALESCE(b.is_deleted, 0) = 0
  AND (COALESCE(b.total_pages, 0) <= 0 OR COALESCE(b.file_size, 0) <= 0)
ORDER BY l.name, b.title""",
    },
    {
        "id": "recent_audiobook_progress_diagnosis",
        "name": "최근 오디오북 재생 기록 사용자·권한 진단",
        "description": "최근 재생 기록이 사용자와 오디오북 보관함 권한에 정상 연결되는지 확인합니다.",
        "db_types": ["audiobook"],
        "sql": """SELECT
    p.id AS progress_id,
    p.user_id,
    u.username,
    u.has_audiobook_access,
    a.id AS audiobook_id,
    a.title,
    l.name AS library_name,
    p.current_track_id,
    p.current_time,
    p.total_progress_pct,
    p.is_completed,
    p.last_listened_at,
    CASE
        WHEN u.id IS NULL THEN 'USER_MISSING'
        WHEN COALESCE(u.has_audiobook_access, 0) != 1 THEN 'AUDIOBOOK_ACCESS_DENIED'
        WHEN perm.id IS NULL THEN 'PERMISSION_MISSING'
        WHEN COALESCE(perm.has_access, 0) != 1 THEN 'ACCESS_DENIED'
        ELSE 'OK'
    END AS diagnosis
FROM audiobook_progress p
JOIN audiobooks a ON a.id = p.audiobook_id
LEFT JOIN libraries l ON l.id = a.library_id
LEFT JOIN users u ON u.id = p.user_id
LEFT JOIN user_category_permissions perm
    ON perm.user_id = p.user_id
   AND perm.library_id = a.library_id
ORDER BY p.last_listened_at DESC""",
    },
    {
        "id": "orphan_audiobook_progress_users",
        "name": "존재하지 않는 사용자 오디오북 재생 기록",
        "description": "users에 없는 user_id를 참조하는 오디오북 재생 기록을 사용자별로 집계합니다.",
        "db_types": ["audiobook"],
        "sql": """SELECT
    p.user_id,
    COUNT(*) AS progress_count,
    MAX(p.last_listened_at) AS latest_listened_at
FROM audiobook_progress p
LEFT JOIN users u ON u.id = p.user_id
WHERE u.id IS NULL
GROUP BY p.user_id
ORDER BY progress_count DESC, p.user_id""",
    },
    {
        "id": "audiobook_library_counts",
        "name": "오디오북 보관함별 항목 수와 트랙 수",
        "description": "보관함별 활성·삭제 오디오북 수와 실제 트랙 수를 확인합니다.",
        "db_types": ["audiobook"],
        "sql": """SELECT
    l.id AS library_id,
    l.name AS library_name,
    l.physical_path,
    l.scan_status,
    l.last_scanned_at,
    COUNT(DISTINCT CASE WHEN COALESCE(a.is_deleted, 0) = 0 THEN a.id END) AS active_audiobooks,
    COUNT(DISTINCT CASE WHEN COALESCE(a.is_deleted, 0) != 0 THEN a.id END) AS deleted_audiobooks,
    COUNT(DISTINCT t.id) AS track_count
FROM libraries l
LEFT JOIN audiobooks a ON a.library_id = l.id
LEFT JOIN audiobook_tracks t ON t.audiobook_id = a.id
GROUP BY l.id, l.name, l.physical_path, l.scan_status, l.last_scanned_at
ORDER BY l.id""",
    },
]


def _strip_literals_and_comments(sql):
    result = []
    index = 0
    length = len(sql)
    quote = None
    while index < length:
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < length else ""
        if quote is not None:
            result.append(" ")
            if quote == "[" and char == "]":
                quote = None
            elif char == quote:
                if next_char == quote and quote in {"'", '"', "`"}:
                    result.append(" ")
                    index += 1
                else:
                    quote = None
            index += 1
            continue
        if char in {"'", '"', "`", "["}:
            quote = char
            result.append(" ")
            index += 1
            continue
        if char == "-" and next_char == "-":
            result.extend((" ", " "))
            index += 2
            while index < length and sql[index] not in "\r\n":
                result.append(" ")
                index += 1
            continue
        if char == "/" and next_char == "*":
            result.extend((" ", " "))
            index += 2
            while index < length:
                if sql[index] == "*" and index + 1 < length and sql[index + 1] == "/":
                    result.extend((" ", " "))
                    index += 2
                    break
                result.append(" ")
                index += 1
            continue
        result.append(char)
        index += 1
    return "".join(result)


def validate_read_only_sql(sql):
    query = str(sql or "").strip()
    if not query:
        raise ValueError("실행할 SQL을 입력해 주세요.")
    if len(query) > 100000:
        raise ValueError("SQL 문장이 너무 깁니다.")

    sanitized = _strip_literals_and_comments(query).strip()
    while sanitized.endswith(";"):
        sanitized = sanitized[:-1].rstrip()
    if ";" in sanitized:
        raise ValueError("한 번에 하나의 SQL 문장만 실행할 수 있습니다.")

    upper = sanitized.upper()
    if not (
        re.match(r"^SELECT\b", upper)
        or re.match(r"^WITH\b", upper)
        or re.match(r"^EXPLAIN(?:\s+QUERY\s+PLAN)?\s+(SELECT|WITH)\b", upper)
    ):
        raise ValueError("SELECT, WITH, EXPLAIN QUERY PLAN만 실행할 수 있습니다.")

    tokens = set(re.findall(r"\b[A-Z_]+\b", upper))
    denied = sorted(tokens.intersection(FORBIDDEN_KEYWORDS))
    if denied:
        raise ValueError(f"읽기 전용 SQL에서 사용할 수 없는 키워드입니다: {denied[0]}")
    return query


class ReadOnlySqlTool:
    DEFAULT_MAX_ROWS = 200
    MAX_ROWS = 500
    DEFAULT_TIMEOUT_SECONDS = 3.0
    MAX_TIMEOUT_SECONDS = 10.0

    def __init__(self, settings):
        self.settings = dict(settings or {})
        if not any(
            key in self.settings
            for key in (
                "db_engine",
                "general_db_path",
                "adult_db_path",
                "audiobook_db_path",
                "mariadb_host",
            )
        ):
            legacy_paths = dict(self.settings)
            self.settings = {
                "db_engine": "sqlite",
                "general_db_path": legacy_paths.get("general", ""),
                "adult_db_path": legacy_paths.get("adult", ""),
                "audiobook_db_path": legacy_paths.get("audiobook", ""),
                "adult_enabled": bool(legacy_paths.get("adult")),
                "audiobook_enabled": bool(legacy_paths.get("audiobook")),
            }
        self.database_adapter = BookOasisDatabaseAdapter(self.settings)

    @staticmethod
    def presets():
        presets = []
        for item in SQL_PRESETS:
            preset = dict(item)
            preset.setdefault("db_types", ["general", "adult"])
            presets.append(preset)
        return presets

    def _target(self, db_type):
        db_key = str(db_type or "general").strip().lower()
        if db_key == "adult" and not _as_bool(self.settings.get("adult_enabled")):
            raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_key}")
        if db_key == "audiobook" and not _as_bool(
            self.settings.get("audiobook_enabled", True)
        ):
            raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_key}")
        if db_key not in {"general", "adult", "audiobook"}:
            raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_key}")
        path = self.settings.get(f"{db_key}_db_path")
        target = self.database_adapter.target(db_key, db_key, path)
        return db_key, target

    @staticmethod
    def _display_value(value):
        if isinstance(value, bytes):
            return f"<BLOB {len(value)} bytes>"
        if isinstance(value, str) and len(value) > 10000:
            return value[:10000] + "… <truncated>"
        return value

    @staticmethod
    def _authorizer(state):
        def authorize(action, arg1, arg2, database_name, trigger_name):
            del database_name, trigger_name
            table = str(arg1 or "").lower()
            column = str(arg2 or "").lower()
            if action == sqlite3.SQLITE_READ and (table, column) in SENSITIVE_COLUMNS:
                state["sensitive"] = f"{table}.{column}"
                return sqlite3.SQLITE_DENY
            if action in DENIED_AUTHORIZER_ACTIONS:
                return sqlite3.SQLITE_DENY
            if action == getattr(sqlite3, "SQLITE_FUNCTION", -1):
                function_name = str(arg2 or arg1 or "").lower()
                if function_name in {"load_extension", "readfile", "writefile"}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        return authorize

    def execute(self, db_type, sql, max_rows=None, timeout_seconds=None):
        query = validate_read_only_sql(sql)
        db_key, target = self._target(db_type)
        try:
            row_limit = int(max_rows or self.DEFAULT_MAX_ROWS)
        except (TypeError, ValueError):
            row_limit = self.DEFAULT_MAX_ROWS
        row_limit = max(1, min(row_limit, self.MAX_ROWS))
        try:
            timeout = float(timeout_seconds or self.DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = self.DEFAULT_TIMEOUT_SECONDS
        timeout = max(0.001, min(timeout, self.MAX_TIMEOUT_SECONDS))

        if target.engine == "mariadb":
            return self._execute_mariadb(
                db_key, target, query, row_limit, timeout
            )
        return self._execute_sqlite(db_key, target, query, row_limit, timeout)

    def _execute_sqlite(self, db_key, target, query, row_limit, timeout):
        path = Path(target.path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"DB 파일을 찾을 수 없습니다: {path}")
        started = time.monotonic()
        deadline = started + timeout
        state = {"sensitive": ""}
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=min(timeout, 10.0))
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 3000")
            connection.set_authorizer(self._authorizer(state))
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1000,
            )
            cursor = connection.execute(query)
            if cursor.description is None:
                raise ValueError("결과 행을 반환하는 조회 SQL만 실행할 수 있습니다.")
            columns = [str(item[0]) for item in cursor.description]
            fetched = cursor.fetchmany(row_limit + 1)
            truncated = len(fetched) > row_limit
            rows = [
                [self._display_value(value) for value in row]
                for row in fetched[:row_limit]
            ]
        except sqlite3.DatabaseError as error:
            if state["sensitive"]:
                raise ValueError(
                    f"민감 정보 컬럼은 조회할 수 없습니다: {state['sensitive']}"
                ) from error
            if "interrupted" in str(error).lower() or time.monotonic() >= deadline:
                raise TimeoutError(
                    f"SQL 실행 제한 시간 {timeout:g}초를 초과했습니다."
                ) from error
            raise ValueError(f"SQL 실행 오류: {error}") from error
        finally:
            connection.close()

        return {
            "db_type": db_key,
            "engine": "sqlite",
            "database": "",
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": row_limit,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    @staticmethod
    def _guard_mariadb_sensitive(query):
        sanitized = _strip_literals_and_comments(query).lower()
        if re.search(r"\b(password_hash|load_file|sleep|benchmark)\b", sanitized):
            raise ValueError("민감 컬럼 또는 위험한 MariaDB 함수를 조회할 수 없습니다.")
        if re.search(r"\b(information_schema|performance_schema|mysql|sys)\s*\.", sanitized):
            raise ValueError("선택한 BookOasis DB 밖의 스키마는 조회할 수 없습니다.")
        if re.search(r"\b(from|join)\s+`?settings`?\b", sanitized):
            raise ValueError("민감 설정을 포함하는 settings 테이블은 조회할 수 없습니다.")
        if re.search(r"\b(from|join)\s+`?users`?\b", sanitized):
            select_part = sanitized.split("from", 1)[0]
            select_without_count = re.sub(
                r"\bcount\s*\(\s*\*\s*\)",
                "",
                select_part,
            )
            if "*" in select_without_count:
                raise ValueError("users 테이블은 필요한 비민감 컬럼만 명시해 주세요.")

    def _execute_mariadb(self, db_key, target, query, row_limit, timeout):
        self._guard_mariadb_sensitive(query)
        query = re.sub(
            r"^\s*EXPLAIN\s+QUERY\s+PLAN\s+",
            "EXPLAIN ",
            query,
            count=1,
            flags=re.IGNORECASE,
        )
        started = time.monotonic()
        try:
            with closing(self.database_adapter.connect(target)) as connection:
                connection.execute(
                    "SET SESSION max_statement_time = ?", (float(timeout),)
                ).close()
                cursor = connection.execute(query)
                try:
                    if cursor.description is None:
                        raise ValueError(
                            "결과 행을 반환하는 조회 SQL만 실행할 수 있습니다."
                        )
                    columns = [str(item[0]) for item in cursor.description]
                    fetched = cursor.fetchmany(row_limit + 1)
                finally:
                    cursor.close()
        except BookOasisDatabaseError as error:
            message = str(error)
            if (
                "max_statement_time" in message.lower()
                or "query execution was interrupted" in message.lower()
            ):
                raise TimeoutError(
                    f"SQL 실행 제한 시간 {timeout:g}초를 초과했습니다."
                ) from error
            raise
        except Exception as error:
            message = str(error)
            if "max_statement_time" in message.lower() or "query execution was interrupted" in message.lower():
                raise TimeoutError(
                    f"SQL 실행 제한 시간 {timeout:g}초를 초과했습니다."
                ) from error
            raise ValueError(f"SQL 실행 오류: {message}") from error
        truncated = len(fetched) > row_limit
        rows = [
            [self._display_value(row.get(column)) for column in columns]
            for row in fetched[:row_limit]
        ]
        return {
            "db_type": db_key,
            "engine": "mariadb",
            "database": target.database,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "max_rows": row_limit,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }
