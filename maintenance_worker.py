# 장시간 걸리는 BookOasis 유지보수 작업을 FlaskFarm 웹 프로세스 밖에서 실행합니다.
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path

try:
    from .bookoasis_client import BookOasisClient
    from .bookoasis_db import BookOasisDatabaseAdapter
    from .category_migration import CategoryMigrationEngine, MigrationStopped
    from .cover_inspector import cleanup_orphan_files, inspect_cover_file
    from .library_statistics import LibraryStatisticsCancelled, LibraryStatisticsEngine
    from .mate_engine import BookOasisMateEngine
except ImportError:
    from bookoasis_client import BookOasisClient
    from bookoasis_db import BookOasisDatabaseAdapter
    from category_migration import CategoryMigrationEngine, MigrationStopped
    from cover_inspector import cleanup_orphan_files, inspect_cover_file
    from library_statistics import LibraryStatisticsCancelled, LibraryStatisticsEngine
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


class JobAlreadyRunning(RuntimeError):
    pass


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except (PermissionError, OSError):
        return True


class WorkerLock:
    def __init__(self, path, stale_seconds=7200):
        self.path = Path(path)
        self.owner_path = self.path / "owner.json"
        self.stale_seconds = max(60, int(stale_seconds or 7200))

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError:
            owner = _read_json(self.owner_path, {}) or {}
            age = max(0, time.time() - float(owner.get("started_epoch") or 0))
            if _pid_alive(owner.get("pid")) or age < min(30, self.stale_seconds):
                raise JobAlreadyRunning("같은 집계 작업이 이미 실행 중입니다.")
            try:
                self.owner_path.unlink()
            except FileNotFoundError:
                pass
            try:
                self.path.rmdir()
            except OSError as error:
                raise JobAlreadyRunning("같은 집계 작업 잠금을 확인할 수 없습니다.") from error
            self.path.mkdir()
        _write_json(
            self.owner_path,
            {"pid": os.getpid(), "started_epoch": time.time()},
        )
        return self

    def __exit__(self, unused_type, unused_value, unused_traceback):
        try:
            self.owner_path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.path.rmdir()
        except OSError:
            pass


class StatusWriter:
    def __init__(self, status_path, job_type):
        self.status_path = Path(status_path)
        self.job_type = job_type
        self.started = time.monotonic()
        self.last_save_at = 0.0
        self.last_stage = ""
        self._lock = threading.RLock()
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
        with self._lock:
            now = time.monotonic()
            if not force and now - self.last_save_at < 0.5:
                return
            self.status["elapsed_seconds"] = round(now - self.started, 1)
            _write_json(self.status_path, self.status)
            self.last_save_at = now

    def analysis_progress(self, stage, current, total, message):
        current = int(current or 0)
        total = max(1, int(total or 100))
        self.status.update(
            {
                "stage": str(stage or "analyze"),
                "current": current,
                "total": total,
                "progress_percent": min(100, round(current * 100 / total)),
                "message": str(message or "집계 중입니다."),
            }
        )
        self._save(force=current in {0, total})

    def complete_analysis(self, result, source, message):
        self.status.update(
            {
                "is_working": "done",
                "stage": "complete",
                "current": 100,
                "total": 100,
                "progress_percent": 100,
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "message": message,
                "result": result,
                "source": source,
                "error": "",
            }
        )
        self._save(force=True)

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

    def batch_rescan_progress(self, book_id, title, success, message):
        current = int(self.status.get("current") or 0) + 1
        total = int(self.status.get("total") or 0)
        success_count = int(self.status.get("success_count") or 0)
        failed_count = int(self.status.get("failed_count") or 0)
        if success:
            success_count += 1
        else:
            failed_count += 1
        items = self.status.setdefault("items", [])
        items.append(
            {
                "book_id": int(book_id),
                "title": str(title or f"도서 {book_id}"),
                "status": "success" if success else "error",
                "message": str(message or ""),
            }
        )
        if len(items) > 200:
            del items[:-200]
            self.status["truncated"] = True
        self.status.update(
            {
                "stage": "book_scan",
                "current": current,
                "success_count": success_count,
                "failed_count": failed_count,
                "progress_percent": min(100, round(current * 100 / total)) if total else 0,
                "message": f"도서 {current:,}/{total:,}권을 처리했습니다.",
                "last_book_id": int(book_id),
            }
        )
        if not success:
            self._append_log(f"도서 ID {book_id} 재스캔 실패. {message}")
        elif current in {1, total} or current % 100 == 0:
            self._append_log(self.status["message"])
        self._save(force=current == total)

    def complete_batch_rescan(self, stopped=False):
        current = int(self.status.get("current") or 0)
        total = int(self.status.get("total") or 0)
        if stopped:
            message = f"사용자 요청으로 일괄 재스캔을 중지했습니다. ({current:,}/{total:,})"
        else:
            message = f"검색 결과 일괄 재스캔을 완료했습니다. ({current:,}/{total:,})"
        self.status.update(
            {
                "is_working": "stop" if stopped else "wait",
                "stopped": bool(stopped),
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "progress_percent": min(100, round(current * 100 / total)) if total else 100,
                "message": message,
                "error": "",
            }
        )
        self._append_log(message)
        self._save(force=True)

    def cover_inspection_progress(self, current, total, issue_counts):
        current = int(current or 0)
        total = int(total or 0)
        self.status.update(
            {
                "stage": "cover_files",
                "current": current,
                "total": total,
                "progress_percent": min(100, round(current * 100 / total)) if total else 0,
                "issue_counts": dict(issue_counts or {}),
                "message": f"표지 파일 {current:,}/{total:,}개를 검사했습니다.",
            }
        )
        if current in {0, 1, total} or (current and current % 1000 == 0):
            self._append_log(self.status["message"])
        self._save(force=current == total)

    def complete_cover_inspection(self, result, stopped=False):
        current = int(result.get("inspected_count") or 0)
        total = int(result.get("source_total") or 0)
        message = (
            f"사용자 요청으로 표지 정밀 검사를 중지했습니다. ({current:,}/{total:,})"
            if stopped
            else f"표지 정밀 검사를 완료했습니다. ({current:,}/{total:,})"
        )
        self.status.update(
            {
                "is_working": "stop" if stopped else "wait",
                "stopped": bool(stopped),
                "current": current,
                "total": total,
                "progress_percent": min(100, round(current * 100 / total)) if total else 100,
                "issue_counts": dict(result.get("issue_counts") or {}),
                "result_ready": True,
                "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "message": message,
                "error": "",
            }
        )
        self._append_log(message)
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
        database_settings=config,
    )
    if operation == "export":
        db_type = str(config.get("export_db_type") or "general")
        target_db_path = config.get(f"target_{db_type}_db")
        result = engine.export_categories(
            target_db_path,
            db_type,
            config.get("export_library_ids"),
        )
    else:
        inspection = engine.inspect_package(config.get("import_package"))
        requested_type = str(config.get("import_db_type") or "auto")
        db_type = inspection["db_type"] if requested_type == "auto" else requested_type
        target_db_path = config.get(f"target_{db_type}_db")
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


def _open_book_database(settings, db_type):
    adapter = BookOasisDatabaseAdapter(settings)
    target = adapter.target(
        db_type,
        db_type,
        settings.get(f"{db_type}_db_path"),
    )
    return adapter.connect(target)


def _scan_book_with_retry(client, book_id, db_type, stop_file, max_attempts=3):
    last_result = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = client.scan_book(book_id, db_type)
        except Exception as error:
            result = {
                "success": False,
                "message": f"BookOasis 관리자 API 요청 중 예외가 발생했습니다: {error}",
                "retryable": True,
            }
        last_result = dict(result or {})
        if last_result.get("success"):
            if attempt > 1:
                message = last_result.get("message") or "재스캔 완료"
                last_result["message"] = f"{message} ({attempt}회 시도)"
            return last_result
        if not last_result.get("retryable") or attempt >= max_attempts:
            break
        if stop_file.exists():
            break
        time.sleep(0.5 * attempt)

    last_result = last_result or {
        "success": False,
        "message": "BookOasis 관리자 API 응답이 없습니다.",
        "retryable": True,
    }
    if last_result.get("retryable") and max_attempts > 1:
        message = last_result.get("message") or "재스캔 실패"
        last_result["message"] = f"{message} ({max_attempts}회 시도)"
    return last_result


def _run_batch_book_rescan(config, writer, stop_file):
    db_type = str(config.get("db_type") or "general")
    issue_filter = dict(config.get("issue_filter") or {})
    book_ids = [
        int(value)
        for value in config.get("book_ids") or []
        if str(value or "").isdigit() and int(value) > 0
    ]
    writer.status.update(
        {
            "db_type": db_type,
            "source": str(config.get("source") or ""),
            "source_label": str(config.get("source_label") or "검색 결과"),
            "total": len(book_ids),
            "message": (
                "문제 도서 재스캔 대상을 집계하고 있습니다."
                if issue_filter
                else "BookOasis 관리자 API에 연결하고 있습니다."
            ),
        }
    )
    writer._save(force=True)
    db_settings = config.get("db_settings") or {
        "db_engine": "sqlite",
        f"{db_type}_db_path": config.get("db_path"),
    }
    engine = BookOasisMateEngine(db_settings)
    if issue_filter:
        total = engine.count_issues(
            db_type=db_type,
            library_id=issue_filter.get("library_id"),
            issue_type=issue_filter.get("issue_type", "all"),
            search=issue_filter.get("search", ""),
        )
        writer.status.update(
            {
                "stage": "target_count",
                "total": total,
                "message": f"문제 도서 재스캔 대상 {total:,}권을 확인했습니다.",
            }
        )
        writer._append_log(writer.status["message"])
        writer._save(force=True)
        if total == 0:
            writer.complete_batch_rescan(stopped=False)
            return

    client = BookOasisClient(
        config.get("bookoasis_url"),
        config.get("api_timeout", 30),
        username=config.get("bookoasis_username"),
        password=config.get("bookoasis_password"),
    )
    login = client.login_admin()
    if not login.get("success"):
        raise RuntimeError(login.get("message") or "BookOasis 관리자 로그인에 실패했습니다.")

    consecutive_transport_failures = 0
    def process_book(book_id, title):
        nonlocal consecutive_transport_failures
        result = _scan_book_with_retry(
            client,
            book_id,
            db_type,
            stop_file,
            max_attempts=3,
        )
        success = bool(result.get("success"))
        message = result.get("message") or result.get("error") or (
            "재스캔 완료" if success else "재스캔 실패"
        )
        writer.batch_rescan_progress(book_id, title, success, message)
        if success or not result.get("retryable"):
            consecutive_transport_failures = 0
        else:
            consecutive_transport_failures += 1
            if consecutive_transport_failures >= 10:
                raise RuntimeError(
                    "BookOasis API 응답이 10권 연속 실패하여 일괄 재스캔을 중단했습니다. "
                    "BookOasis 상태와 네트워크를 확인한 뒤 다시 실행해 주세요."
                )

    if issue_filter:
        for batch in engine.iter_issue_book_batches(
            db_type=db_type,
            library_id=issue_filter.get("library_id"),
            issue_type=issue_filter.get("issue_type", "all"),
            search=issue_filter.get("search", ""),
            after_id=int(issue_filter.get("after_id") or 0),
            batch_size=int(issue_filter.get("batch_size") or 500),
        ):
            for item in batch:
                if stop_file.exists():
                    writer.complete_batch_rescan(stopped=True)
                    return
                process_book(int(item["id"]), str(item.get("title") or ""))
    else:
        with closing(_open_book_database(db_settings, db_type)) as connection:
            for book_id in book_ids:
                if stop_file.exists():
                    writer.complete_batch_rescan(stopped=True)
                    return
                row = connection.execute(
                    "SELECT title FROM books WHERE id = ?",
                    (book_id,),
                ).fetchone()
                title = str(row["title"] or "") if row else ""
                process_book(book_id, title)
    writer.complete_batch_rescan(stopped=False)


def _run_cover_inspection(config, writer, stop_file):
    settings = dict(config.get("settings") or {})
    root = settings.get("cover_root_path")
    if not root or not Path(root).is_dir():
        raise FileNotFoundError("설정된 표지 디렉터리를 찾을 수 없습니다.")
    db_type = str(config.get("db_type") or "general")
    library_id = config.get("library_id")
    search = str(config.get("search") or "").strip()
    raw_result_path = str(config.get("result_path") or "").strip()
    if not raw_result_path:
        raise ValueError("표지 정밀 검사 결과 경로가 없습니다.")
    result_path = Path(raw_result_path)

    target_issues = {"low_resolution", "small_file", "abnormal_aspect_ratio"}
    issue_counts = {key: 0 for key in sorted(target_issues)}
    issue_items = []
    inspected_count = 0
    source_total = 0
    source_page = 1
    stopped = False
    engine = BookOasisMateEngine(settings)
    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=4) as executor:
        while True:
            if stop_file.exists():
                stopped = True
                break
            batch = engine.cover_items(
                db_type=db_type,
                library_id=library_id,
                search=search,
                page=source_page,
                page_size=1000,
            )
            if source_page == 1:
                source_total = int(batch.get("total") or 0)
                writer.cover_inspection_progress(0, source_total, issue_counts)
            items = list(batch.get("items") or [])
            if not items:
                break
            inspections = list(executor.map(
                lambda item: inspect_cover_file(
                    root,
                    item.get("cover_path"),
                    min_width=int(settings.get("cover_min_width") or 200),
                    min_height=int(settings.get("cover_min_height") or 280),
                    min_file_size=int(settings.get("cover_min_file_size_kb") or 0) * 1024,
                    min_aspect_ratio=int(settings.get("cover_min_aspect_percent") or 0) / 100,
                ),
                items,
            ))
            for item, inspection in zip(items, inspections):
                issues = [
                    code for code in inspection.get("issues") or []
                    if code in target_issues
                ]
                if not issues:
                    continue
                for code in issues:
                    issue_counts[code] += 1
                stored = dict(item)
                stored["inspection"] = dict(inspection)
                issue_items.append(stored)
            inspected_count += len(items)
            writer.cover_inspection_progress(inspected_count, source_total, issue_counts)
            if stop_file.exists():
                stopped = True
                break
            if source_page >= int(batch.get("pages") or 0):
                break
            source_page += 1

    result = {
        "fingerprint": str(config.get("fingerprint") or ""),
        "db_type": db_type,
        "library_id": str(library_id or ""),
        "search": search,
        "source_total": source_total,
        "inspected_count": inspected_count,
        "issue_counts": issue_counts,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "complete": not stopped,
        "items": issue_items,
    }
    _write_json(result_path, result)
    writer.complete_cover_inspection(result, stopped=stopped)


def _run_summary_report(config, writer):
    engine = BookOasisMateEngine(dict(config.get("settings") or {}))
    source = engine.source_fingerprint()
    result = engine.build_report(
        source=source,
        target_cache=None,
        force=bool(config.get("force")),
    )
    writer.complete_analysis(result, source, "상태 요약 갱신을 완료했습니다.")


def _run_library_statistics(config, writer, stop_file):
    engine = LibraryStatisticsEngine(dict(config.get("settings") or {}))
    db_type = str(config.get("db_type") or "general")
    library_id = str(config.get("library_id") or "").strip() or None
    source = engine.source_fingerprint(db_type=db_type, library_id=library_id)
    result = engine.analyze(
        db_type=db_type,
        library_id=library_id,
        on_progress=writer.analysis_progress,
        should_stop=stop_file.exists,
        source=source,
    )
    writer.complete_analysis(result, source, "라이브러리 통계 분석을 완료했습니다.")


def run_worker(config_path, status_path, stop_path):
    config = _read_json(config_path)
    if not isinstance(config, dict):
        raise ValueError("작업 설정을 읽을 수 없습니다.")
    if config.get("delete_config_after_read"):
        try:
            Path(config_path).unlink()
        except FileNotFoundError:
            pass
    if hasattr(os, "nice"):
        try:
            os.nice(5)
        except OSError:
            pass
    job_type = str(config.get("job_type") or "").strip()
    if job_type not in {
        "orphan_cleanup",
        "category_migration",
        "batch_book_rescan",
        "cover_inspection",
        "summary_report",
        "library_statistics",
    }:
        raise ValueError("지원하지 않는 유지보수 작업입니다.")
    stop_file = Path(stop_path)
    lock_path = config.get("lock_path") or f"{status_path}.lock"
    watchdog_seconds = max(0, int(config.get("watchdog_seconds") or 0))
    writer = None
    watchdog = None
    try:
        with WorkerLock(lock_path, stale_seconds=max(120, watchdog_seconds * 2)):
            writer = StatusWriter(status_path, job_type)
            if watchdog_seconds:
                def abort_timed_out_worker():
                    writer.failed(
                        "작업 대기 제한 시간을 초과했습니다.",
                        f"watchdog timeout: {watchdog_seconds}s",
                    )
                    os._exit(124)

                watchdog = threading.Timer(watchdog_seconds, abort_timed_out_worker)
                watchdog.daemon = True
                watchdog.start()
            if job_type == "orphan_cleanup":
                _run_orphan_cleanup(config, writer, stop_file)
            elif job_type == "category_migration":
                _run_category_migration(config, writer, stop_file)
            elif job_type == "batch_book_rescan":
                _run_batch_book_rescan(config, writer, stop_file)
            elif job_type == "cover_inspection":
                _run_cover_inspection(config, writer, stop_file)
            elif job_type == "summary_report":
                _run_summary_report(config, writer)
            elif job_type == "library_statistics":
                _run_library_statistics(config, writer, stop_file)
        return 0
    except JobAlreadyRunning:
        return 3
    except LibraryStatisticsCancelled as error:
        if writer is not None:
            writer.stopped(str(error))
        return 2
    except MigrationStopped as error:
        if writer is not None:
            writer.stopped("사용자 요청으로 카테고리 이관을 중지했습니다.")
        return 2
    except Exception as error:
        messages = {
            "orphan_cleanup": "고아 표지파일 정리에 실패했습니다.",
            "category_migration": "카테고리 이관에 실패했습니다.",
            "batch_book_rescan": "검색 결과 일괄 재스캔에 실패했습니다.",
            "cover_inspection": "표지 정밀 검사에 실패했습니다.",
            "summary_report": "상태 요약 갱신에 실패했습니다.",
            "library_statistics": "라이브러리 통계 분석에 실패했습니다.",
        }
        message = messages[job_type]
        if writer is not None:
            writer.failed(message, error)
        traceback.print_exc()
        return 1
    finally:
        if watchdog is not None:
            watchdog.cancel()


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
