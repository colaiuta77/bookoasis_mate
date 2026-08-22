# FF 컨테이너의 MariaDB 필수 패키지 상태와 설치 작업을 관리합니다.
import copy
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from importlib import metadata


class DependencyInstallerError(RuntimeError):
    pass


class DependencyInstaller:
    PIP_SPEC = "PyMySQL>=1.1,<2"
    MAX_OUTPUT = 12000
    INSTALL_TIMEOUT = 900

    def __init__(self, logger=None):
        self.logger = logger
        self._lock = threading.Lock()
        self._job = None

    @staticmethod
    def _version_tuple(value):
        numbers = []
        for part in str(value or "").split("."):
            digits = "".join(character for character in part if character.isdigit())
            if not digits:
                break
            numbers.append(int(digits))
        return tuple(numbers)

    @staticmethod
    def _is_root():
        get_euid = getattr(os, "geteuid", None)
        return bool(get_euid and get_euid() == 0)

    @staticmethod
    def _package_manager():
        if shutil.which("apt-get"):
            return "apt-get"
        if shutil.which("apk"):
            return "apk"
        return ""

    @staticmethod
    def _command_version(command):
        if not command:
            return ""
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = (result.stdout or result.stderr or "").strip()
            return output.splitlines()[0][:240] if output else ""
        except Exception:
            return ""

    def _python_package_status(self):
        try:
            version = metadata.version("PyMySQL")
        except Exception:
            version = ""
        installed = bool(version)
        version_tuple = self._version_tuple(version)
        ready = installed and (1, 1) <= version_tuple < (2,)
        return {
            "key": "pymysql",
            "name": "PyMySQL",
            "spec": self.PIP_SPEC,
            "installed": installed,
            "ready": ready,
            "version": version,
            "message": "" if ready else ("1.1 이상 2.0 미만 버전이 필요합니다." if installed else "설치되지 않았습니다."),
        }

    def _client_package_status(self):
        client = shutil.which("mariadb") or shutil.which("mysql") or ""
        dump = shutil.which("mariadb-dump") or shutil.which("mysqldump") or ""
        installed = bool(client and dump)
        versions = [self._command_version(path) for path in (client, dump) if path]
        return {
            "key": "mariadb-client",
            "name": "mariadb-client",
            "spec": "mariadb-client",
            "installed": installed,
            "ready": installed,
            "version": " / ".join(value for value in versions if value),
            "client_path": client,
            "dump_path": dump,
            "message": "" if installed else "mariadb와 mariadb-dump 명령이 모두 필요합니다.",
        }

    def status(self):
        manager = self._package_manager()
        with self._lock:
            job = copy.deepcopy(self._job)
        return {
            "packages": {
                "pymysql": self._python_package_status(),
                "mariadb-client": self._client_package_status(),
            },
            "platform": {
                "python": sys.executable,
                "package_manager": manager,
                "is_root": self._is_root(),
                "system_install_supported": bool(manager),
            },
            "job": job,
        }

    def install_commands(self, key):
        key = str(key or "").strip().lower()
        if key == "pymysql":
            return [[
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                self.PIP_SPEC,
            ]]
        if key == "mariadb-client":
            manager = self._package_manager()
            if not manager:
                raise DependencyInstallerError(
                    "지원하는 패키지 관리자(apt-get 또는 apk)를 찾을 수 없습니다."
                )
            if not self._is_root():
                raise DependencyInstallerError(
                    "mariadb-client 설치에는 root 권한이 필요합니다. FF 컨테이너 실행 사용자를 확인해 주세요."
                )
            executable = shutil.which(manager) or manager
            if manager == "apt-get":
                return [
                    [executable, "update"],
                    [
                        executable,
                        "install",
                        "-y",
                        "--no-install-recommends",
                        "mariadb-client",
                    ],
                ]
            return [[executable, "add", "--no-cache", "mariadb-client"]]
        if key == "all":
            commands = self.install_commands("pymysql")
            commands.extend(self.install_commands("mariadb-client"))
            return commands
        if key == "needed":
            package_status = {
                "pymysql": self._python_package_status(),
                "mariadb-client": self._client_package_status(),
            }
            needed = [
                package_key
                for package_key, item in package_status.items()
                if not item.get("ready")
            ]
            if not needed:
                raise DependencyInstallerError("MariaDB 필수 구성요소가 이미 모두 사용 가능합니다.")
            commands = []
            for package_key in needed:
                commands.extend(self.install_commands(package_key))
            return commands
        raise DependencyInstallerError("지원하지 않는 패키지 설치 요청입니다.")

    def start(self, key):
        commands = self.install_commands(key)
        with self._lock:
            if self._job and self._job.get("status") in {"ready", "running"}:
                raise DependencyInstallerError("다른 패키지 설치 작업이 진행 중입니다.")
            self._job = {
                "id": uuid.uuid4().hex,
                "key": str(key or "").strip().lower(),
                "status": "ready",
                "status_label": "대기",
                "message": "설치 작업을 준비하고 있습니다.",
                "current": 0,
                "total": len(commands),
                "percent": 0,
                "output": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
            }
            job_id = self._job["id"]
            snapshot = copy.deepcopy(self._job)
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, commands),
            daemon=True,
            name="bookoasis-mate-dependency-installer",
        )
        thread.start()
        return snapshot

    def _append_output(self, value):
        text = str(value or "").strip()
        if not text:
            return
        with self._lock:
            if not self._job:
                return
            existing = self._job.get("output") or ""
            self._job["output"] = (existing + ("\n" if existing else "") + text)[
                -self.MAX_OUTPUT :
            ]

    def _run_job(self, job_id, commands):
        try:
            with self._lock:
                if not self._job or self._job.get("id") != job_id:
                    return
                self._job["status"] = "running"
                self._job["status_label"] = "진행 중"
                self._job["message"] = "필수 패키지를 설치하고 있습니다."
            total = max(1, len(commands))
            for index, command in enumerate(commands, start=1):
                with self._lock:
                    self._job["current"] = index
                    self._job["percent"] = int(((index - 1) / total) * 100)
                    self._job["message"] = f"설치 단계 {index}/{total}을 실행하고 있습니다."
                if self.logger:
                    self.logger.info(
                        "[BookOasisMate] 필수 패키지 설치 단계 시작 %s/%s command=%s",
                        index,
                        total,
                        " ".join(command),
                    )
                environment = os.environ.copy()
                environment.setdefault("DEBIAN_FRONTEND", "noninteractive")
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self.INSTALL_TIMEOUT,
                    env=environment,
                )
                self._append_output(result.stdout)
                self._append_output(result.stderr)
                if result.returncode != 0:
                    raise DependencyInstallerError(
                        f"설치 명령이 종료 코드 {result.returncode}로 실패했습니다."
                    )
                with self._lock:
                    self._job["percent"] = int((index / total) * 100)
            with self._lock:
                self._job["status"] = "completed"
                self._job["status_label"] = "완료"
                self._job["message"] = "필수 패키지 설치를 완료했습니다."
                self._job["percent"] = 100
                self._job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as error:
            if self.logger:
                self.logger.exception("[BookOasisMate] 필수 패키지 설치 실패")
            self._append_output(str(error))
            with self._lock:
                if self._job and self._job.get("id") == job_id:
                    self._job["status"] = "failed"
                    self._job["status_label"] = "실패"
                    self._job["message"] = str(error)
                    self._job["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
