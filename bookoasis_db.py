# BookOasis의 SQLite와 MariaDB를 같은 읽기 전용 인터페이스로 연결합니다.
import os
import re
import sqlite3
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path


VALID_DB_TYPES = {"general", "adult", "audiobook"}
VALID_ENGINES = {"auto", "sqlite", "mariadb"}


class BookOasisDatabaseError(RuntimeError):
    """DB 엔진 설정 또는 연결 실패를 사용자용 메시지로 전달합니다."""


class CompatRow(dict):
    """sqlite3.Row처럼 이름과 정수 인덱스를 함께 지원합니다."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class CursorProxy:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def _row(self, value):
        if value is None:
            return None
        if isinstance(value, dict):
            return CompatRow({key: normalize_value(item) for key, item in value.items()})
        if hasattr(value, "keys"):
            return CompatRow(
                {key: normalize_value(value[key]) for key in value.keys()}
            )
        names = [column[0] for column in (self.description or [])]
        return CompatRow(
            zip(names, (normalize_value(item) for item in value))
        )

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [self._row(row) for row in rows]

    def __iter__(self):
        for row in self._cursor:
            yield self._row(row)

    def close(self):
        self._cursor.close()


class ConnectionProxy:
    def __init__(self, connection, target):
        self._connection = connection
        self.target = target
        self.engine = target.engine
        self._tables_cache = None
        self._columns_cache = {}

    def execute(self, sql, params=()):
        if self.engine == "sqlite":
            return CursorProxy(
                self._connection.execute(sql, tuple(params or ()))
            )
        cursor = self._connection.cursor()
        try:
            cursor.execute(convert_placeholders(sql), tuple(params or ()))
        except Exception as error:
            cursor.close()
            raise BookOasisDatabaseError(
                f"MariaDB 조회 실패: {error}"
            ) from error
        return CursorProxy(cursor)

    def executemany(self, sql, parameters):
        values = [tuple(item or ()) for item in parameters]
        if self.engine == "sqlite":
            return CursorProxy(self._connection.executemany(sql, values))
        cursor = self._connection.cursor()
        try:
            cursor.executemany(convert_placeholders(sql), values)
        except Exception as error:
            cursor.close()
            raise BookOasisDatabaseError(
                f"MariaDB 쿼리 실행 실패: {error}"
            ) from error
        return CursorProxy(cursor)

    def begin(self, immediate=False):
        if self.engine == "sqlite":
            statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
            self._connection.execute(statement)
        else:
            self._connection.begin()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def tables(self):
        if self._tables_cache is not None:
            return set(self._tables_cache)
        if self.engine == "sqlite":
            rows = self.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = ?",
                (self.target.database,),
            ).fetchall()
        self._tables_cache = {str(row["name"]) for row in rows}
        return set(self._tables_cache)

    def columns(self, table):
        safe_table = validate_identifier(table)
        if safe_table in self._columns_cache:
            return set(self._columns_cache[safe_table])
        if self.engine == "sqlite":
            rows = self.execute(f'PRAGMA table_info("{safe_table}")').fetchall()
        else:
            rows = self.execute(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                (self.target.database, safe_table),
            ).fetchall()
        self._columns_cache[safe_table] = {str(row["name"]) for row in rows}
        return set(self._columns_cache[safe_table])

    def database_size(self):
        if self.engine == "sqlite":
            return Path(self.target.path).stat().st_size
        row = self.execute(
            "SELECT COALESCE(SUM(data_length + index_length), 0) AS size_bytes "
            "FROM information_schema.tables WHERE table_schema = ?",
            (self.target.database,),
        ).fetchone()
        return int(row["size_bytes"] or 0)

    def engine_details(self):
        if self.engine == "sqlite":
            return {
                "journal_mode": str(self.execute("PRAGMA journal_mode").fetchone()[0]),
                "user_version": int(self.execute("PRAGMA user_version").fetchone()[0] or 0),
                "server_version": sqlite3.sqlite_version,
                "collation": "",
            }
        row = self.execute(
            "SELECT VERSION() AS server_version, @@collation_database AS collation"
        ).fetchone()
        return {
            "journal_mode": "",
            "user_version": 0,
            "server_version": str(row["server_version"] or ""),
            "collation": str(row["collation"] or ""),
        }


class DatabaseTarget:
    def __init__(self, db_type, label, engine, path="", database=""):
        self.db_type = db_type
        self.label = label
        self.engine = engine
        self.path = str(path or "").strip()
        self.database = str(database or "").strip()

    @property
    def identity(self):
        return self.path if self.engine == "sqlite" else self.database


class BookOasisDatabaseAdapter:
    def __init__(self, settings, pymysql_module=None):
        self.settings = dict(settings or {})
        self.selected_engine = normalize_engine(self.settings.get("db_engine"))
        self.engine = resolve_engine(self.settings)
        self._pymysql_module = pymysql_module

    def database_name(self, db_type):
        db_type = normalize_db_type(db_type)
        prefix = str(
            self.settings.get("mariadb_database_prefix") or "media_"
        ).strip()
        return f"{prefix}{db_type}"

    def target(self, db_type, label, path=""):
        db_type = normalize_db_type(db_type)
        return DatabaseTarget(
            db_type=db_type,
            label=label,
            engine=self.engine,
            path=path if self.engine == "sqlite" else "",
            database=self.database_name(db_type) if self.engine == "mariadb" else "",
        )

    def connect(self, target, readonly=True):
        if target.engine != self.engine:
            raise BookOasisDatabaseError("DB 대상과 선택된 엔진이 일치하지 않습니다.")
        if self.engine == "sqlite":
            return self._connect_sqlite(target, readonly=readonly)
        return self._connect_mariadb(target, readonly=readonly)

    @staticmethod
    def _connect_sqlite(target, readonly=True):
        if not target.path:
            raise BookOasisDatabaseError(f"{target.label} DB 경로가 비어 있습니다.")
        path = Path(target.path).expanduser()
        if not path.is_file():
            raise BookOasisDatabaseError(f"DB 파일을 찾을 수 없습니다: {target.path}")
        try:
            if readonly:
                uri = f"{path.resolve().as_uri()}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=30)
            else:
                connection = sqlite3.connect(str(path.resolve()), timeout=30)
            connection.row_factory = sqlite3.Row
            if readonly:
                connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 30000")
            return ConnectionProxy(connection, target)
        except sqlite3.Error as error:
            raise BookOasisDatabaseError(f"SQLite 연결 실패: {error}") from error

    def _connect_mariadb(self, target, readonly=True):
        module = self._pymysql_module
        if module is None:
            try:
                import pymysql as module
            except ImportError as error:
                raise BookOasisDatabaseError(
                    "MariaDB 연결 모듈 PyMySQL이 없습니다. FlaskFarm 환경에 "
                    "PyMySQL 1.1 이상을 설치해 주세요."
                ) from error

        host = str(self.settings.get("mariadb_host") or "").strip()
        user = str(self.settings.get("mariadb_user") or "").strip()
        if not host or not user:
            raise BookOasisDatabaseError(
                "MariaDB 호스트와 사용자를 설정해 주세요."
            )
        try:
            connection = module.connect(
                host=host,
                port=bounded_int(self.settings.get("mariadb_port"), 3306, 1, 65535),
                user=user,
                password=str(self.settings.get("mariadb_password") or ""),
                database=target.database,
                charset="utf8mb4",
                autocommit=bool(readonly),
                cursorclass=module.cursors.DictCursor,
                connect_timeout=bounded_int(
                    self.settings.get("mariadb_connect_timeout"), 10, 1, 60
                ),
                read_timeout=bounded_int(
                    self.settings.get("mariadb_read_timeout"), 30, 1, 600
                ),
                write_timeout=bounded_int(
                    self.settings.get("mariadb_write_timeout"), 30, 1, 600
                ),
            )
            return ConnectionProxy(connection, target)
        except Exception as error:
            raise BookOasisDatabaseError(
                f"MariaDB {target.database} 연결 실패: {error}"
            ) from error

    def public_info(self):
        return {
            "selected_engine": self.selected_engine,
            "resolved_engine": self.engine,
            "host": str(self.settings.get("mariadb_host") or "").strip()
            if self.engine == "mariadb"
            else "",
            "port": bounded_int(self.settings.get("mariadb_port"), 3306, 1, 65535)
            if self.engine == "mariadb"
            else 0,
            "database_prefix": str(
                self.settings.get("mariadb_database_prefix") or "media_"
            ).strip()
            if self.engine == "mariadb"
            else "",
        }


def bounded_int(value, default, minimum, maximum):
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def normalize_value(value):
    """MariaDB 결과를 기존 SQLite·JSON 처리 코드가 다룰 수 있게 정규화합니다."""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def normalize_engine(value):
    value = str(value or "sqlite").strip().lower()
    if value == "mysql":
        value = "mariadb"
    return value if value in VALID_ENGINES else "sqlite"


def resolve_engine(settings, environ=None):
    selected = normalize_engine((settings or {}).get("db_engine"))
    if selected != "auto":
        return selected
    environ = os.environ if environ is None else environ
    for key in ("DB_ENGINE", "DBMS"):
        value = str(environ.get(key) or "").strip().lower()
        if value == "mysql":
            value = "mariadb"
        if value in {"sqlite", "mariadb"}:
            return value
    if str((settings or {}).get("mariadb_host") or "").strip() and str(
        (settings or {}).get("mariadb_user") or ""
    ).strip():
        return "mariadb"
    return "sqlite"


def normalize_db_type(value):
    value = str(value or "general").strip().lower()
    if value not in VALID_DB_TYPES:
        raise ValueError(f"지원하지 않는 DB 유형입니다: {value}")
    return value


def validate_identifier(value):
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError("DB 식별자가 올바르지 않습니다.")
    return value


def convert_placeholders(sql):
    """문자열과 주석 밖의 qmark placeholder만 PyMySQL 형식으로 바꿉니다."""
    output = []
    index = 0
    quote = ""
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            output.append(char)
            if char == quote:
                if next_char == quote:
                    output.append(next_char)
                    index += 1
                else:
                    quote = ""
            elif char == "\\" and next_char:
                output.append(next_char)
                index += 1
        elif char in {"'", '"', "`"}:
            quote = char
            output.append(char)
        elif char == "-" and next_char == "-":
            end = sql.find("\n", index)
            if end < 0:
                output.append(sql[index:])
                break
            output.append(sql[index:end + 1])
            index = end
        elif char == "/" and next_char == "*":
            end = sql.find("*/", index + 2)
            if end < 0:
                output.append(sql[index:])
                break
            output.append(sql[index:end + 2])
            index = end + 1
        elif char == "?":
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)
