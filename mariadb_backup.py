# MariaDB 논리 백업과 복원을 외부 클라이언트로 안전하게 실행합니다.
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class MariaDBToolError(RuntimeError):
    pass


class MariaDBClientTools:
    def __init__(self, settings, work_dir=None, should_stop=None, on_progress=None):
        self.settings = dict(settings or {})
        self.work_dir = Path(work_dir).expanduser().resolve() if work_dir else None
        self.should_stop = should_stop or (lambda: False)
        self.on_progress = on_progress

    @staticmethod
    def _find_binary(configured, candidates, label):
        value = str(configured or "").strip()
        if value:
            path = shutil.which(value) or (value if Path(value).is_file() else "")
            if path:
                return str(path)
        for candidate in candidates:
            path = shutil.which(candidate)
            if path:
                return str(path)
        names = ", ".join(candidates)
        raise MariaDBToolError(
            f"{label} 실행 파일을 찾을 수 없습니다. FlaskFarm 환경에 "
            f"MariaDB client를 설치해 주세요. 탐색 이름: {names}"
        )

    @property
    def dump_binary(self):
        return self._find_binary(
            self.settings.get("mariadb_dump_binary"),
            ("mariadb-dump", "mysqldump"),
            "MariaDB 백업",
        )

    @property
    def client_binary(self):
        return self._find_binary(
            self.settings.get("mariadb_client_binary"),
            ("mariadb", "mysql"),
            "MariaDB 복원",
        )

    @contextmanager
    def _defaults_file(self):
        directory = str(self.work_dir) if self.work_dir else None
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".bookoasis_mate_mariadb_",
            suffix=".cnf",
            dir=directory,
            delete=False,
        )
        path = Path(handle.name)
        try:
            handle.write("[client]\n")
            handle.write(f"host={str(self.settings.get('mariadb_host') or '').strip()}\n")
            handle.write(f"port={int(self.settings.get('mariadb_port') or 3306)}\n")
            handle.write(f"user={str(self.settings.get('mariadb_user') or '').strip()}\n")
            password = str(self.settings.get("mariadb_password") or "")
            escaped_password = (
                password.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "")
                .replace("\n", "")
            )
            handle.write(f'password="{escaped_password}"\n')
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            if os.name != "nt":
                os.chmod(path, 0o600)
            yield path
        finally:
            try:
                handle.close()
            except Exception:
                pass
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _run(self, command, stdin=None, stdout=None, label="MariaDB 작업"):
        with tempfile.TemporaryFile() as error_output:
            process = subprocess.Popen(
                command,
                stdin=stdin,
                stdout=stdout,
                stderr=error_output,
            )
            while process.poll() is None:
                if self.should_stop():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise MariaDBToolError(f"사용자 요청으로 {label}을 중지했습니다.")
                time.sleep(0.2)
            error_output.seek(0)
            stderr = error_output.read().decode("utf-8", errors="replace")
            if process.returncode != 0:
                message = stderr.strip().splitlines()[-1] if stderr.strip() else "알 수 없는 오류"
                raise MariaDBToolError(f"{label}에 실패했습니다: {message}")

    def dump(self, database, target_path):
        database = str(database or "").strip()
        if not database:
            raise ValueError("백업할 MariaDB 이름이 비어 있습니다.")
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with self._defaults_file() as defaults:
            command = [
                self.dump_binary,
                f"--defaults-extra-file={defaults}",
                "--single-transaction",
                "--quick",
                "--hex-blob",
                "--triggers",
                "--skip-comments",
                "--default-character-set=utf8mb4",
                database,
            ]
            try:
                with partial.open("wb") as output:
                    self._run(command, stdout=output, label=f"{database} 논리 백업")
                os.replace(str(partial), str(target))
            finally:
                if partial.exists():
                    partial.unlink()
        return target

    def restore(self, database, source_path):
        database = str(database or "").strip()
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError(f"MariaDB 복원 파일을 찾을 수 없습니다: {source}")
        with self._defaults_file() as defaults:
            command = [
                self.client_binary,
                f"--defaults-extra-file={defaults}",
                "--default-character-set=utf8mb4",
                database,
            ]
            with source.open("rb") as input_file:
                self._run(
                    command,
                    stdin=input_file,
                    label=f"{database} 논리 복원",
                )


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def clear_mariadb_database(connection):
    rows = connection.execute("SHOW FULL TABLES").fetchall()
    table_names = []
    view_names = []
    for row in rows:
        values = list(row.values())
        if not values:
            continue
        name = str(values[0])
        kind = str(values[1] if len(values) > 1 else "BASE TABLE").upper()
        if "VIEW" in kind:
            view_names.append(name)
        else:
            table_names.append(name)
    connection.execute("SET FOREIGN_KEY_CHECKS = 0")
    try:
        for name in view_names:
            safe = name.replace("`", "``")
            connection.execute(f"DROP VIEW IF EXISTS `{safe}`")
        for name in table_names:
            safe = name.replace("`", "``")
            connection.execute(f"DROP TABLE IF EXISTS `{safe}`")
    finally:
        connection.execute("SET FOREIGN_KEY_CHECKS = 1")
