# 고아 표지 정리와 카테고리 이관을 FlaskFarm 웹 프로세스 밖에서 실행합니다.
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

try:
    from .category_migration import CategoryMigrationEngine, MigrationStopped
    from .cover_inspector import cleanup_orphan_files
    from .mate_engine import BookOasisMateEngine
except ImportError:
    from category_migration import CategoryMigrationEngine, MigrationStopped
    from cover_inspector import cleanup_orphan_files
    from mate_engine import BookOasisMateEngine


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


class StatusWriter:
    def __init__(self, status_path, job_type):
        self.status_path = Path(status_path)
        self.job_type = job_type
        self.started = time.monotonic()
        self.last_save_at = 0.0
        self.last_stage = ""
        self.status = _read_json(self.status_path, {}) or {}
        self.status.update(
            {
                "is_working": "run",
                "job_type": job_type,
                "worker_pid": os.getpid(),
                "started_epoch": time.time(),
                "error": "",
            }
        )
        self._save(force=True)

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

    def _save(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_save_at < 0.5:
            return
        self.status["elapsed_seconds"] = round(now - self.started, 1)
        _write_json(self.status_path, self.status)
        self.last_save_at = now

    def category_progress(self, progress):
        stage = str(progress.get("stage") or "")
        current = int(progress.get("current") or 0)
        total = int(progress.get("total") or 0)
        message = str(progress.get("message") or "카테고리 이관 중입니다.")
        self.status.update(
            {
                "stage": stage,
                "current": current,
                "total": total,
                "progress_percent": (
                    min(100, round(current * 100 / total)) if total else 0
                ),
                "message": message,
            }
        )
        stage_changed = stage != self.last_stage
        if (
            stage_changed
            or current in {0, 1, total}
            or (current and current % 100 == 0)
        ):
            self._append_log(progress.get("log") or message)
        self.last_stage = stage
        self._save(force=stage_changed or current == total)

    def orphan_reference_progress(self, read_count, reference_count):
        self.status.update(
            {
                "stage": "references",
                "message": (
                    f"DB 표지 참조를 읽고 있습니다. "
                    f"{read_count:,}건 확인 · {reference_count:,}개 참조"
                ),
            }
        )
        self._save()

    def orphan_progress(self, progress):
        for key in (
            "scanned_count",
            "target_count",
            "target_size",
            "deleted_count",
            "deleted_size",
            "error_count",
            "items",
            "truncated",
            "stopped",
        ):
            if key in progress:
                self.status[key] = progress[key]
        self.status.update(
            {
                "stage": "files",
                "message": "고아 표지 파일을 확인하고 있습니다.",
            }
        )
        self._save()

    def complete_category(self, result, operation):
        if operation == "export":
            message = "카테고리 내보내기를 완료했습니다."
        elif result.get("mode") == "merge":
            message = "기존 카테고리 병합을 완료했습니다."
        else:
            message = "신규 카테고리 가져오기를 완료했습니다."
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
        self._save(force=True)

    def complete_orphan(self, result, dry_run):
        self.orphan_progress(result)
        stopped = bool(result.get("stopped"))
        if stopped:
            message = "사용자 요청으로 작업을 중지했습니다."
        elif dry_run:
            message = "Dry Run을 완료했습니다."
        else:
            message = "고아 표지파일 정리를 완료했습니다."
        self.status.update(
            {
                "is_working": "stop" if stopped else "wait",
                "message": message,
                "error": "",
            }
        )
        self._save(force=True)

    def stopped(self, message):
        self.status.update(
            {
                "is_working": "stop",
                "message": message,
                "error": "",
            }
        )
        self._append_log(message)
        self._save(force=True)

    def failed(self, message, error):
        self.status.update(
            {
                "is_working": "error",
                "message": message,
                "error": str(error),
            }
        )
        self._append_log(f"{message} {error}")
        self._save(force=True)


def _run_orphan_cleanup(config, writer, stop_file):
    settings = dict(config.get("settings") or {})
    library_ids = list(config.get("library_ids") or [])
    dry_run = bool(config.get("dry_run", True))
    engine = BookOasisMateEngine(settings)
    references = engine.all_cover_references(
        include_inactive_adult=True,
        library_ids=library_ids,
        batch_size=2000,
        on_progress=writer.orphan_reference_progress,
        should_stop=stop_file.exists,
    )
    result = cleanup_orphan_files(
        settings.get("cover_root_path"),
        references,
        library_ids,
        dry_run=dry_run,
        result_limit=500,
        should_stop=stop_file.exists,
        on_progress=writer.orphan_progress,
    )
    writer.complete_orphan(result, dry_run)


def _run_category_migration(config, writer, stop_file):
    operation = str(config.get("operation") or "").strip().lower()
    if operation not in {"export", "import"}:
        raise ValueError("카테고리 이관 작업 유형이 올바르지 않습니다.")
    engine = CategoryMigrationEngine(
        config.get("work_dir"),
        config.get("cover_root_path"),
        should_stop=stop_file.exists,
        on_progress=writer.category_progress,
    )
    if operation == "export":
        db_type = str(config.get("export_db_type") or "general")
        target_db_path = (
            config.get("target_adult_db")
            if db_type == "adult"
            else config.get("target_general_db")
        )
        result = engine.export_categories(
            target_db_path,
            db_type,
            config.get("export_library_ids"),
        )
    else:
        inspection = engine.inspect_package(config.get("import_package"))
        requested_type = str(config.get("import_db_type") or "auto")
        db_type = inspection["db_type"] if requested_type == "auto" else requested_type
        target_db_path = (
            config.get("target_adult_db")
            if db_type == "adult"
            else config.get("target_general_db")
        )
        result = engine.import_category(
            target_db_path,
            config.get("import_package"),
            config.get("import_target_paths"),
            db_type=db_type,
            name=config.get("import_name") or None,
            merge_to=(
                config.get("merge_library_id")
                if config.get("import_mode") == "merge"
                else None
            ),
            backup=bool(config.get("backup_before_import", True)),
            inspection=inspection,
        )
    writer.complete_category(result, operation)


def run_worker(config_path, status_path, stop_path):
    config = _read_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("작업 설정을 읽을 수 없습니다.")
    if hasattr(os, "nice"):
        try:
            os.nice(5)
        except OSError:
            pass
    job_type = str(config.get("job_type") or "").strip()
    if job_type not in {"orphan_cleanup", "category_migration"}:
        raise ValueError("지원하지 않는 유지보수 작업입니다.")
    writer = StatusWriter(status_path, job_type)
    stop_file = Path(stop_path)
    try:
        if job_type == "orphan_cleanup":
            _run_orphan_cleanup(config, writer, stop_file)
        else:
            _run_category_migration(config, writer, stop_file)
        return 0
    except MigrationStopped as error:
        writer.stopped("사용자 요청으로 카테고리 이관을 중지했습니다.")
        return 2
    except Exception as error:
        message = (
            "고아 표지파일 정리에 실패했습니다."
            if job_type == "orphan_cleanup"
            else "카테고리 이관에 실패했습니다."
        )
        writer.failed(message, error)
        traceback.print_exc()
        return 1


def main(argv=None):
    arguments = list(argv or sys.argv[1:])
    if len(arguments) != 3:
        print(
            "usage: maintenance_worker.py CONFIG STATUS STOP",
            file=sys.stderr,
        )
        return 64
    return run_worker(*arguments)


if __name__ == "__main__":
    raise SystemExit(main())
