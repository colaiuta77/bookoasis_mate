# Kavita와 BookOasis 통DB 이관을 FlaskFarm 웹 프로세스와 분리해 실행합니다.
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

try:
    from .bookoasis_client import BookOasisClient
    from .bookoasis_package_import import (
        BookOasisPackageImportEngine,
        BookOasisPackageImportStopped,
    )
    from .kavita_migration import (
        KavitaMigrationEngine,
        KavitaMigrationStopped,
    )
except ImportError:
    from bookoasis_client import BookOasisClient
    from bookoasis_package_import import (
        BookOasisPackageImportEngine,
        BookOasisPackageImportStopped,
    )
    from kavita_migration import (
        KavitaMigrationEngine,
        KavitaMigrationStopped,
    )


def _read_json(path, default=None):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(target))


class MigrationStatusWriter:
    def __init__(self, status_path, operation, package_action=""):
        self.status_path = Path(status_path)
        self.started = time.monotonic()
        self.operation = operation
        self.package_action = package_action
        self.status = _read_json(self.status_path, {}) or {}
        self.status.update(
            {
                "is_working": "run",
                "operation": operation,
                "package_action": package_action,
                "worker_pid": os.getpid(),
                "started_epoch": time.time(),
                "error": "",
                "result": None,
            }
        )
        self.status.setdefault("logs", [])
        self._save()

    def _append_log(self, message):
        text = str(message or "").strip()
        if not text:
            return
        logs = self.status.setdefault("logs", [])
        now = datetime.now().strftime("%H:%M:%S")
        if logs and logs[-1].get("message") == text:
            logs[-1]["time"] = now
        else:
            logs.append({"time": now, "message": text})
        if len(logs) > 300:
            del logs[:-300]

    def _save(self):
        self.status["elapsed_seconds"] = round(
            time.monotonic() - self.started,
            1,
        )
        _write_json(self.status_path, self.status)

    def progress(self, progress):
        current = int(progress.get("current") or 0)
        total = int(progress.get("total") or 0)
        message = str(progress.get("message") or "통DB 이관 중입니다.")
        self.status.update(
            {
                "stage": str(progress.get("stage") or ""),
                "current": current,
                "total": total,
                "progress_percent": (
                    min(100, round(current * 100 / total)) if total else 0
                ),
                "message": message,
            }
        )
        if current in {0, 1, total} or (current and current % 100 == 0):
            self._append_log(progress.get("log") or message)
        self._save()

    def complete(self, result):
        if self.operation == "bookoasis" and self.package_action == "export":
            message = "BookOasis 패키지 내보내기를 완료했습니다."
        else:
            message = (
                "통DB 이관 Dry Run을 완료했습니다."
                if result.get("dry_run")
                else "통DB 이관을 완료했습니다."
            )
        self.status.update(
            {
                "is_working": "wait",
                "progress_percent": 100,
                "message": message,
                "result": result,
                "error": "",
            }
        )
        self._append_log(message)
        self._save()

    def stopped(self, error):
        self.status.update(
            {
                "is_working": "stop",
                "message": "통DB 이관을 중지했습니다.",
                "error": str(error),
            }
        )
        self._append_log(self.status["message"])
        self._save()

    def failed(self, error):
        self.status.update(
            {
                "is_working": "error",
                "message": "통DB 이관에 실패했습니다.",
                "error": str(error),
            }
        )
        self._append_log(f"{self.status['message']} {error}")
        self._save()


def run_worker(config_path, status_path, stop_path):
    config = _read_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("통DB 이관 작업 설정을 읽을 수 없습니다.")
    if hasattr(os, "nice"):
        try:
            os.nice(5)
        except OSError:
            pass
    stop_file = Path(stop_path)
    source_type = str(config.get("source_type") or "bookoasis").strip().lower()
    if source_type not in {"kavita", "bookoasis"}:
        raise ValueError("이관 원본 유형이 올바르지 않습니다.")
    package_action = ""
    if source_type == "bookoasis":
        package_action = str(
            config.get("bookoasis_package_action") or "import"
        ).strip().lower()
        if package_action not in {"export", "import"}:
            raise ValueError("BookOasis 패키지 작업 유형이 올바르지 않습니다.")
    writer = MigrationStatusWriter(
        status_path,
        source_type,
        package_action,
    )
    try:
        if source_type == "kavita":
            target_db_type = str(
                config.get("target_db_type") or "general"
            ).strip().lower()
            target_db_path = (
                config.get("target_adult_db")
                if target_db_type == "adult"
                else config.get("target_general_db")
            )
            engine = KavitaMigrationEngine(
                config.get("kavita_db_path"),
                config.get("kavita_cover_path"),
                target_db_path,
                config.get("target_cover_root"),
                config["work_dir"],
                should_stop=stop_file.exists,
                on_progress=writer.progress,
            )
            result = engine.migrate(
                path_mappings=config.get("path_mappings", ""),
                user_mappings=config.get("user_mappings", ""),
                selected_libraries=config.get("selected_libraries", []),
                import_covers=bool(config.get("import_covers", True)),
                import_progress=bool(config.get("import_progress", True)),
                lock_metadata=bool(config.get("lock_metadata", True)),
                backup=bool(config.get("backup", True)),
                dry_run=bool(config.get("dry_run", True)),
            )
            result["source_type"] = "kavita"
        else:
            engine = BookOasisPackageImportEngine(
                config["work_dir"],
                config.get("target_general_db"),
                config.get("target_adult_db"),
                config.get("target_cover_root"),
                health_check=lambda: BookOasisClient(
                    config.get("bookoasis_url"),
                    config.get("api_timeout", 30),
                ).health(),
                should_stop=stop_file.exists,
                on_progress=writer.progress,
            )
            if package_action == "export":
                result = engine.export_package(
                    config.get("bookoasis_export_name", "dist_books")
                )
            else:
                result = engine.migrate(
                    config["bookoasis_db_package_path"],
                    config["bookoasis_cover_package_path"],
                    path_mappings=config.get("bookoasis_path_mappings", ""),
                    backup=bool(config.get("bookoasis_backup", True)),
                    dry_run=bool(config.get("bookoasis_dry_run", True)),
                    confirm_stopped=bool(
                        config.get("bookoasis_confirm_stopped", False)
                    ),
                )
                result.setdefault("package_action", "import")
        writer.complete(result)
        return 0
    except (BookOasisPackageImportStopped, KavitaMigrationStopped) as error:
        writer.stopped(error)
        return 2
    except Exception as error:
        writer.failed(error)
        traceback.print_exc()
        return 1


def main(argv=None):
    arguments = list(argv or sys.argv[1:])
    if len(arguments) != 3:
        print(
            "usage: database_migration_worker.py CONFIG STATUS STOP",
            file=sys.stderr,
        )
        return 64
    return run_worker(*arguments)


if __name__ == "__main__":
    raise SystemExit(main())
