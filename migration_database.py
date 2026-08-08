# 이관 작업에서 SQLite와 MariaDB 쓰기 연결·검증·백업을 통합합니다.
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    from .bookoasis_db import BookOasisDatabaseAdapter
    from .mariadb_backup import MariaDBClientTools
except ImportError:
    from bookoasis_db import BookOasisDatabaseAdapter
    from mariadb_backup import MariaDBClientTools


class MigrationDatabaseContext:
    def __init__(self, settings, work_dir=None, should_stop=None, on_progress=None):
        self.settings = dict(settings or {})
        for db_type in ("general", "adult", "audiobook"):
            path_key = f"{db_type}_db_path"
            legacy_key = f"target_{db_type}_db"
            if not self.settings.get(path_key) and self.settings.get(legacy_key):
                self.settings[path_key] = self.settings[legacy_key]
        self.adapter = BookOasisDatabaseAdapter(self.settings)
        self.work_dir = Path(work_dir).expanduser().resolve() if work_dir else None
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress

    @property
    def engine(self):
        return self.adapter.engine

    def target(self, db_type):
        labels = {"general": "일반", "adult": "성인", "audiobook": "오디오북"}
        return self.adapter.target(
            db_type,
            labels.get(db_type, db_type),
            self.settings.get(f"{db_type}_db_path"),
        )

    def connect(self, db_type, readonly=True):
        return self.adapter.connect(self.target(db_type), readonly=readonly)

    def libraries(self, db_type):
        connection = self.connect(db_type, readonly=True)
        try:
            if "libraries" not in connection.tables():
                return []
            rows = connection.execute(
                "SELECT id, name FROM libraries ORDER BY LOWER(name), id"
            ).fetchall()
            return [{"id": int(row["id"]), "name": row["name"]} for row in rows]
        finally:
            connection.close()

    def integrity_check(self, db_type):
        connection = self.connect(db_type, readonly=True)
        try:
            if self.engine == "sqlite":
                values = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
                if values != ["ok"]:
                    raise RuntimeError(
                        "대상 DB quick_check에 실패했습니다: " + ", ".join(values[:10])
                    )
                return {"engine": "sqlite", "result": "ok"}
            failures = []
            for table in sorted(connection.tables()):
                safe = table.replace("`", "``")
                for row in connection.execute(f"CHECK TABLE `{safe}` QUICK"):
                    message_type = str(row.get("Msg_type") or row.get("msg_type") or "")
                    message = str(row.get("Msg_text") or row.get("msg_text") or "")
                    if message_type.lower() == "error" or message.lower() != "ok":
                        failures.append(f"{table}: {message_type} {message}".strip())
            if failures:
                raise RuntimeError(
                    "MariaDB CHECK TABLE QUICK에 실패했습니다: "
                    + ", ".join(failures[:10])
                )
            return {"engine": "mariadb", "result": "ok"}
        finally:
            connection.close()

    def _sqlite_backup(self, db_type, target):
        source_path = Path(self.target(db_type).path)
        source = sqlite3.connect(
            f"{source_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()

    def backup(self, db_type, reason="before_import"):
        if self.work_dir is None:
            raise ValueError("DB 백업을 저장할 작업 디렉터리가 없습니다.")
        backup_dir = self.work_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if self.engine == "sqlite":
            source_name = Path(self.target(db_type).path).stem
            target = backup_dir / f"{source_name}_{db_type}_{reason}_{timestamp}.db"
            self._sqlite_backup(db_type, target)
            return target
        database = self.target(db_type).database
        target = backup_dir / f"{database}_{reason}_{timestamp}.sql"
        MariaDBClientTools(
            self.settings,
            work_dir=self.work_dir,
            should_stop=self.should_stop,
            on_progress=self.on_progress,
        ).dump(database, target)
        return target
