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

DIAGNOSTIC_TABLES = {
    "information_schema": {"processlist", "tables", "columns", "statistics", "schemata", "innodb_trx", "innodb_locks", "innodb_lock_waits"},
    "performance_schema": {"threads", "events_statements_current", "events_statements_history", "events_statements_summary_by_digest", "table_io_waits_summary_by_table"},
}

DIAGNOSTIC_PRESETS = [
    ("processlist", "현재 실행 중인 쿼리", "SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO FROM information_schema.PROCESSLIST WHERE COMMAND <> 'Sleep' ORDER BY TIME DESC"),
    ("long_queries", "5초 이상 실행 중인 쿼리", "SELECT ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO FROM information_schema.PROCESSLIST WHERE COMMAND <> 'Sleep' AND TIME >= 5 ORDER BY TIME DESC"),
    ("transactions", "InnoDB 트랜잭션", "SELECT * FROM information_schema.INNODB_TRX ORDER BY trx_started"),
    ("table_sizes", "테이블 크기", "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE, TABLE_ROWS, ROUND(DATA_LENGTH / 1024 / 1024, 2) AS data_mb, ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() ORDER BY DATA_LENGTH + INDEX_LENGTH DESC"),
    ("indexes", "인덱스 현황", "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"),
]

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
    {
        "id": "video_library_counts",
        "name": "비디오북 보관함별 작품·에피소드 수",
        "description": "보관함별 활성·삭제 작품 수와 에피소드 수를 확인합니다.",
        "db_types": ["video"],
        "sql": """SELECT
    l.id AS library_id,
    l.name AS library_name,
    l.physical_path,
    l.scan_status,
    l.last_scanned_at,
    COUNT(DISTINCT CASE WHEN COALESCE(v.is_deleted, 0) = 0 THEN v.id END) AS active_videos,
    COUNT(DISTINCT CASE WHEN COALESCE(v.is_deleted, 0) != 0 THEN v.id END) AS deleted_videos,
    COUNT(DISTINCT e.id) AS episode_count
FROM libraries l
LEFT JOIN videos v ON v.library_id = l.id
LEFT JOIN video_episodes e ON e.video_id = v.id
GROUP BY l.id, l.name, l.physical_path, l.scan_status, l.last_scanned_at
ORDER BY l.id""",
    },
    {
        "id": "recent_video_progress",
        "name": "최근 비디오북 시청 기록",
        "description": "비디오 DB에 저장된 최근 작품·에피소드 진행률을 확인합니다. 사용자명은 general DB에서 별도로 확인해야 합니다.",
        "db_types": ["video"],
        "sql": """SELECT
    p.user_id,
    v.id AS video_id,
    v.title,
    l.name AS library_name,
    p.current_episode_id,
    e.title AS episode_title,
    p.current_time,
    p.total_progress_pct,
    p.is_completed,
    p.last_watched_at
FROM video_progress p
JOIN videos v ON v.id = p.video_id
LEFT JOIN libraries l ON l.id = v.library_id
LEFT JOIN video_episodes e ON e.id = p.current_episode_id
ORDER BY p.last_watched_at DESC""",
    },
]


def _strip_literals_and_comments(sql, identifiers=False):
    result = []
    index = 0
    length = len(sql)
    quote = None
    while index < length:
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < length else ""
        if quote is not None:
            result.append(char if identifiers and quote == "`" and char != "`" else " ")
            if char == "\\" and quote in {"'", '"'}:
                result.append(" ")
                index += 2
                continue
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
        if char == "#":
            while index < length and sql[index] not in "\r\n":
                result.append(" ")
                index += 1
            continue
        if char == "/" and next_char == "*":
            if sql[index + 2:index + 3] == "!" or sql[index + 2:index + 4].upper() == "M!":
                raise ValueError("실행 가능한 SQL 주석은 허용하지 않습니다.")
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
                "video_db_path",
                "mariadb_host",
            )
        ):
            legacy_paths = dict(self.settings)
            self.settings = {
                "db_engine": "sqlite",
                "general_db_path": legacy_paths.get("general", ""),
                "adult_db_path": legacy_paths.get("adult", ""),
                "audiobook_db_path": legacy_paths.get("audiobook", ""),
                "video_db_path": legacy_paths.get("video", ""),
                "adult_enabled": bool(legacy_paths.get("adult")),
                "audiobook_enabled": bool(legacy_paths.get("audiobook")),
                "video_enabled": bool(legacy_paths.get("video")),
            }
        self.database_adapter = BookOasisDatabaseAdapter(self.settings)

    @staticmethod
    def presets(mode="safe"):
        presets = []
        for item in SQL_PRESETS:
            preset = dict(item)
            preset.setdefault("db_types", ["general", "adult"])
            presets.append(preset)
        if mode == "diagnostic":
            presets.extend({"id": "diagnostic_" + key, "name": name, "sql": query, "mode": "diagnostic", "db_types": ["general", "adult", "audiobook", "video"], "description": "MariaDB 서버의 권한과 버전에 따라 표시 범위와 지원 테이블이 다릅니다."} for key, name, query in DIAGNOSTIC_PRESETS)
            for preset in presets:
                if preset["id"] == "diagnostic_transactions":
                    preset["description"] = "InnoDB 트랜잭션 조회에는 Mate의 MariaDB 접속 계정에 PROCESS 권한이 필요합니다. 관리자 진단 모드 선택만으로 권한이 부여되지는 않습니다."
        return presets

    def _target(self, db_type):
        db_key = str(db_type or "general").strip().lower()
        if db_key == "adult" and not _as_bool(self.settings.get("adult_enabled")):
            raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_key}")
        if db_key == "audiobook" and not _as_bool(
            self.settings.get("audiobook_enabled", True)
        ):
            raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_key}")
        if db_key == "video" and not _as_bool(
            self.settings.get("video_enabled", True)
        ):
            raise ValueError(f"활성화되지 않은 DB 유형입니다: {db_key}")
        if db_key not in {"general", "adult", "audiobook", "video"}:
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

    def execute(self, db_type, sql, max_rows=None, timeout_seconds=None, mode="safe"):
        query = validate_read_only_sql(sql)
        if mode not in {"safe", "diagnostic"}:
            raise ValueError("지원하지 않는 SQL 모드입니다.")
        db_key, target = self._target(db_type)
        diagnostic = mode == "diagnostic"
        if diagnostic and target.engine != "mariadb":
            raise ValueError("관리자 진단 모드는 MariaDB에서만 사용할 수 있습니다.")
        try:
            row_limit = int(max_rows or self.DEFAULT_MAX_ROWS)
        except (TypeError, ValueError):
            row_limit = self.DEFAULT_MAX_ROWS
        row_limit = max(1, min(row_limit, 5000 if diagnostic else self.MAX_ROWS))
        try:
            timeout = float(timeout_seconds or self.DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = self.DEFAULT_TIMEOUT_SECONDS
        timeout = max(0.001, min(timeout, 30.0 if diagnostic else self.MAX_TIMEOUT_SECONDS))

        if target.engine == "mariadb":
            return self._execute_mariadb(
                db_key, target, query, row_limit, timeout, mode=mode
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
    def _guard_mariadb_sensitive(query, mode="safe", database=""):
        if '"' in query or "\\" in query:
            raise ValueError("MariaDB SQL 모드에 따른 해석 차이를 피하려면 문자열은 작은따옴표, 식별자는 백틱을 사용하고 역슬래시 이스케이프는 제거해 주세요.")
        sanitized = _strip_literals_and_comments(query, identifiers=True).lower()
        if re.search(r"\binto\s+(outfile|dumpfile)\b", sanitized):
            raise ValueError("읽기 전용 SQL에서는 MariaDB 파일 출력(OUTFILE/DUMPFILE)을 사용할 수 없습니다.")
        if re.search(r"\b(password_hash|load_file|sleep|benchmark|get_lock|release_lock)\b", sanitized):
            raise ValueError("민감 컬럼 또는 위험한 MariaDB 함수를 조회할 수 없습니다.")
        if re.search(r"\b(for\s+update|lock\s+in\s+share\s+mode|into)\b", sanitized):
            raise ValueError("잠금 또는 변수·파일 출력을 사용하는 SQL은 허용하지 않습니다.")
        for schema, table in re.findall(r"\b(information_schema|performance_schema|mysql|sys)\s*\.\s*(\w+)", sanitized):
            if mode != "diagnostic":
                raise ValueError("일반 조회에서는 시스템 스키마를 조회할 수 없습니다. 관리자 진단 모드를 사용해 주세요.")
            if table not in DIAGNOSTIC_TABLES.get(schema, set()):
                raise ValueError(f"관리자 진단 모드에서 허용되지 않은 테이블입니다. {schema}.{table}")
        for schema in re.findall(r"(?:\bfrom\b|\bjoin\b)\s*(?:\(\s*)*(\w+)\s*\.", sanitized):
            if schema not in DIAGNOSTIC_TABLES and schema != database.lower():
                raise ValueError("선택한 BookOasis DB 밖의 스키마는 조회할 수 없습니다.")
        for clause in re.findall(r"\bfrom\b(.*?)(?=\bwhere\b|\bgroup\b|\border\b|\blimit\b|\bunion\b|$)", sanitized, re.DOTALL):
            if re.search(r",\s*(?:\(\s*)*\w+\s*\.", clause):
                raise ValueError("스키마를 지정한 쉼표 조인 대신 명시적 JOIN을 사용해 주세요.")
        if re.search(r"\bsettings\b", sanitized):
            raise ValueError("민감 설정을 포함하는 settings 테이블은 조회할 수 없습니다.")
        if re.search(r"\busers\b", sanitized):
            select_part = sanitized
            select_without_count = re.sub(
                r"\bcount\s*\(\s*\*\s*\)",
                "",
                select_part,
            )
            if "*" in select_without_count:
                raise ValueError("users 테이블은 필요한 비민감 컬럼만 명시해 주세요.")

    def _execute_mariadb(self, db_key, target, query, row_limit, timeout, mode="safe"):
        self._guard_mariadb_sensitive(query, mode=mode, database=target.database)
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
                connection.execute("START TRANSACTION READ ONLY").close()
                try:
                    cursor = connection.execute_stream(query)
                    try:
                        if cursor.description is None:
                            raise ValueError("결과 행을 반환하는 조회 SQL만 실행할 수 있습니다.")
                        columns = [str(item[0]) for item in cursor.description]
                        fetched = cursor.fetchmany(row_limit + 1)
                    finally:
                        cursor.close()
                finally:
                    connection.rollback()
                    connection.execute("SET SESSION max_statement_time = 0").close()
        except BookOasisDatabaseError as error:
            message = str(error)
            driver_args = getattr(error.__cause__, "args", ())
            if len(driver_args) >= 2 and driver_args[0] == 1227 and "PROCESS" in str(driver_args[1]).upper():
                raise ValueError(
                    "MariaDB 접속 계정의 PROCESS 권한이 없어 이 진단 쿼리를 실행할 수 없습니다. "
                    "DB 관리자에게 해당 계정의 권한을 확인해 달라고 요청하세요. Mate는 DB 권한을 자동 부여하지 않습니다."
                ) from error
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
