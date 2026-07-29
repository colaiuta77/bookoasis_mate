# FlaskFarm 설정과 진단 엔진, 검사 이력 저장을 연결합니다.
import copy
import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .bookoasis_client import BookOasisClient
from .bookoasis_logs import list_log_files, read_lazy_progress, read_log_tail
from .bookoasis_package_import import BookOasisPackageImportEngine
from .category_migration import (
    CategoryMigrationEngine,
    MigrationStopped,
    parse_library_ids,
    parse_paths,
)
from .cover_inspector import cleanup_orphan_files, inspect_cover_file
from .kavita_migration import KavitaMigrationEngine, parse_name_list
from .mate_engine import BookOasisMateEngine, _as_bool, _as_int


GAP_CACHE_SECONDS = 300
GAP_CACHE_LIMIT = 8


def _format_number_ranges(values):
    numbers = sorted({int(value) for value in (values or [])})
    if not numbers:
        return ""
    ranges = []
    start = numbers[0]
    end = numbers[0]
    for value in numbers[1:] + [None]:
        if value is not None and value == end + 1:
            end = value
            continue
        ranges.append(str(start) if start == end else f"{start}-{end}")
        if value is not None:
            start = value
            end = value
    return ", ".join(ranges)


def _safe_csv_text(value):
    text = str(value or "").replace("\x00", "")
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + text
    return text


def build_series_gap_csv(items):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "DB 종류",
            "보관함 ID",
            "보관함",
            "시리즈",
            "보유 권차",
            "예상 누락",
            "전체 도서 수",
            "권차 인식 수",
            "미인식 수",
            "모호한 수",
            "신뢰도",
        ]
    )
    for item in items or []:
        writer.writerow(
            [
                "성인 DB" if item.get("db_type") == "adult" else "일반 DB",
                item.get("library_id") or "",
                _safe_csv_text(item.get("library_name")),
                _safe_csv_text(item.get("series_name")),
                _format_number_ranges(item.get("present")),
                _format_number_ranges(item.get("missing")),
                int(item.get("book_count") or 0),
                int(item.get("parsed_count") or 0),
                int(item.get("unparsed_count") or 0),
                int(item.get("ambiguous_count") or 0),
                "신뢰도 높음" if item.get("confidence") == "high" else "확인 필요",
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def derive_bookoasis_paths(root_path):
    root = str(root_path or "").strip().replace("\\", "/")
    if not root:
        return {}
    root = "/" if root == "/" else root.rstrip("/")

    def child(relative_path):
        return f"/{relative_path}" if root == "/" else f"{root}/{relative_path}"

    return {
        "general_db_path": child("db/media_general.db"),
        "adult_db_path": child("db/media_adult.db"),
        "bookoasis_log_dir": child("logs"),
        "cover_root_path": child("covers"),
    }


def infer_bookoasis_root(values):
    root = str((values or {}).get("bookoasis_root_path") or "").strip()
    if root:
        return root.replace("\\", "/").rstrip("/") or "/"
    general_db_path = str((values or {}).get("general_db_path") or "").strip().replace("\\", "/")
    suffix = "/db/media_general.db"
    if general_db_path.endswith(suffix):
        return general_db_path[: -len(suffix)] or "/"
    return ""


class BookOasisMateService:
    def __init__(self, plugin):
        self.P = plugin
        self._lock = threading.RLock()
        self._cached_report = None
        self._cached_at = 0.0
        self._settings_fingerprint = None
        self._cover_issue_cache_key = None
        self._cover_issue_cache = None
        self._gap_cache = {}
        self._gap_analysis_lock = threading.Lock()
        self._admin_client = None
        self._admin_client_fingerprint = None
        self._orphan_cleanup_stop = threading.Event()
        self._orphan_cleanup_thread = None
        self._orphan_cleanup_status = self._empty_orphan_cleanup_status()
        self._migration_stop = threading.Event()
        self._migration_thread = None
        self._migration_started_monotonic = None
        self._migration_status = self._empty_migration_status()
        self._database_migration_stop = threading.Event()
        self._database_migration_process = None
        self._database_migration_status_path = None
        self._database_migration_stop_path = None
        self._database_migration_started_monotonic = None
        self._database_migration_status = self._empty_migration_status()

    @staticmethod
    def _empty_orphan_cleanup_status():
        return {
            "is_working": "wait",
            "dry_run": True,
            "db_type": "general",
            "library_id": None,
            "library_name": "전체 보관함",
            "scanned_count": 0,
            "target_count": 0,
            "target_size": 0,
            "deleted_count": 0,
            "deleted_size": 0,
            "error_count": 0,
            "items": [],
            "truncated": False,
            "stopped": False,
            "message": "대기중",
        }

    @staticmethod
    def _empty_migration_status():
        return {
            "is_working": "wait",
            "operation": "",
            "stage": "",
            "current": 0,
            "total": 0,
            "progress_percent": 0,
            "started_at": "",
            "elapsed_seconds": 0,
            "message": "대기중",
            "logs": [],
            "result": None,
            "error": "",
        }

    def _debug(self, event, **fields):
        details = []
        for key, value in fields.items():
            safe_value = str(value).replace("\r", " ").replace("\n", " ")[:120]
            details.append(f"{key}={safe_value}")
        suffix = f" {' '.join(details)}" if details else ""
        self.P.logger.debug(f"[BookOasisMate] {event}{suffix}")

    @staticmethod
    def _duration_ms(started):
        return round((time.monotonic() - started) * 1000)

    def settings(self):
        model = self.P.ModelSetting
        values = {
            "bookoasis_root_path": model.get("bookoasis_root_path"),
            "general_db_path": model.get("general_db_path"),
            "adult_enabled": model.get_bool("adult_enabled"),
            "adult_db_path": model.get("adult_db_path"),
            "bookoasis_url": model.get("bookoasis_url"),
            "bookoasis_username": model.get("bookoasis_username"),
            "bookoasis_password": model.get("bookoasis_password"),
            "webhook_token": model.get("webhook_token"),
            "bookoasis_log_dir": model.get("bookoasis_log_dir"),
            "cover_root_path": model.get("cover_root_path"),
            "cover_min_width": _as_int(model.get("cover_min_width"), 200, 1, 10000),
            "cover_min_height": _as_int(model.get("cover_min_height"), 280, 1, 10000),
            "cover_min_file_size_kb": _as_int(model.get("cover_min_file_size_kb"), 5, 0, 102400),
            "cover_min_aspect_percent": _as_int(model.get("cover_min_aspect_percent"), 35, 0, 100),
            "check_missing_isbn": model.get_bool("check_missing_isbn"),
            "stale_days": _as_int(model.get("stale_days"), 14, 1, 3650),
            "page_size": _as_int(model.get("page_size"), 50, 10, 200),
            "cache_seconds": _as_int(model.get("cache_seconds"), 30, 0, 3600),
            "history_limit": _as_int(model.get("history_limit"), 100, 10, 1000),
            "api_timeout": _as_int(model.get("api_timeout"), 30, 1, 30),
        }
        root = infer_bookoasis_root(values)
        values["bookoasis_root_path"] = root
        if root:
            values.update(derive_bookoasis_paths(root))
        return values

    @staticmethod
    def settings_from_mapping(values):
        settings = {
            "bookoasis_root_path": str(values.get("bookoasis_root_path") or "").strip(),
            "general_db_path": values.get("general_db_path"),
            "adult_enabled": _as_bool(values.get("adult_enabled"), False),
            "adult_db_path": values.get("adult_db_path"),
            "bookoasis_url": values.get("bookoasis_url"),
            "bookoasis_username": values.get("bookoasis_username"),
            "bookoasis_password": values.get("bookoasis_password"),
            "webhook_token": values.get("webhook_token"),
            "bookoasis_log_dir": values.get("bookoasis_log_dir"),
            "cover_root_path": values.get("cover_root_path"),
            "cover_min_width": _as_int(values.get("cover_min_width"), 200, 1, 10000),
            "cover_min_height": _as_int(values.get("cover_min_height"), 280, 1, 10000),
            "cover_min_file_size_kb": _as_int(values.get("cover_min_file_size_kb"), 5, 0, 102400),
            "cover_min_aspect_percent": _as_int(values.get("cover_min_aspect_percent"), 35, 0, 100),
            "check_missing_isbn": _as_bool(values.get("check_missing_isbn"), False),
            "stale_days": _as_int(values.get("stale_days"), 14, 1, 3650),
            "page_size": _as_int(values.get("page_size"), 50, 10, 200),
            "cache_seconds": _as_int(values.get("cache_seconds"), 30, 0, 3600),
            "history_limit": _as_int(values.get("history_limit"), 100, 10, 1000),
            "api_timeout": _as_int(values.get("api_timeout"), 30, 1, 30),
        }
        root = infer_bookoasis_root(settings)
        settings["bookoasis_root_path"] = root
        if root:
            settings.update(derive_bookoasis_paths(root))
        return settings

    def engine(self, settings=None):
        return BookOasisMateEngine(settings or self.settings())

    def admin_client(self, settings=None):
        settings = settings or self.settings()
        fingerprint = (
            str(settings.get("bookoasis_url") or ""),
            str(settings.get("bookoasis_username") or ""),
            str(settings.get("bookoasis_password") or ""),
            int(settings.get("api_timeout") or 30),
        )
        with self._lock:
            if self._admin_client is None or self._admin_client_fingerprint != fingerprint:
                self._admin_client = BookOasisClient(
                    settings.get("bookoasis_url"),
                    settings.get("api_timeout", 30),
                    username=settings.get("bookoasis_username"),
                    password=settings.get("bookoasis_password"),
                )
                self._admin_client_fingerprint = fingerprint
            return self._admin_client

    def invalidate(self):
        with self._lock:
            self._cached_report = None
            self._cached_at = 0.0
            self._settings_fingerprint = None
            self._cover_issue_cache_key = None
            self._cover_issue_cache = None
            self._gap_cache = {}
        self._debug("상태 요약 캐시 초기화")

    def report(self, force=False):
        started = time.monotonic()
        settings = self.settings()
        fingerprint = tuple(sorted((key, str(value)) for key, value in settings.items()))
        ttl = settings["cache_seconds"]
        with self._lock:
            fresh = self._cached_report is not None and time.monotonic() - self._cached_at <= ttl
            if not force and fresh and fingerprint == self._settings_fingerprint:
                self._debug("상태 요약 캐시 사용", age_seconds=round(time.monotonic() - self._cached_at, 1))
                return self._cached_report

            self._debug("상태 요약 생성 시작", force=str(bool(force)).lower())
            report = self.engine(settings).build_report()
            self._cached_report = report
            self._cached_at = time.monotonic()
            self._settings_fingerprint = fingerprint
            self._debug(
                "상태 요약 생성 완료",
                status=report.get("status"),
                total_books=report.get("totals", {}).get("total_books", 0),
                problem_books=report.get("totals", {}).get("problem_books", 0),
                duration_ms=self._duration_ms(started),
            )
            return report

    def run_and_record(self, trigger="manual"):
        started = time.monotonic()
        self._debug("전체 검사 시작", trigger=trigger)
        report = self.report(force=True)
        duration_ms = round((time.monotonic() - started) * 1000)
        history_model = getattr(self.P, "history_model", None)
        if history_model is not None:
            history_model.create_from_report(report, trigger, duration_ms)
        else:
            self.P.logger.warning("BookOasis Mate 검사 이력 모델이 비활성화되어 결과를 저장하지 않습니다.")
        self._debug(
            "전체 검사 완료",
            trigger=trigger,
            status=report.get("status"),
            history_saved=str(history_model is not None).lower(),
            duration_ms=duration_ms,
        )
        return report

    def issues(self, **kwargs):
        started = time.monotonic()
        settings = self.settings()
        page_size = kwargs.pop("page_size", settings["page_size"])
        data = self.engine(settings).list_issues(page_size=page_size, **kwargs)
        self._debug(
            "문제 도서 조회 완료",
            db_type=kwargs.get("db_type", "general"),
            library_id=kwargs.get("library_id") or "all",
            issue_type=kwargs.get("issue_type", "all"),
            search=str(bool(kwargs.get("search"))).lower(),
            page=data.get("page", 1),
            total=data.get("total", 0),
            items=len(data.get("items", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def scanner(self, db_type="general", limit=100, include_live=False):
        started = time.monotonic()
        settings = self.settings()
        data = self.engine(settings).scanner_status(db_type=db_type, limit=limit)
        if not _as_bool(include_live, False):
            self._debug(
                "스캔 상태 조회 완료",
                db_type=db_type,
                libraries=len(data.get("libraries", [])),
                tasks=len(data.get("tasks", [])),
                live_queue="false",
                duration_ms=self._duration_ms(started),
            )
            return data
        response = self.admin_client(settings).queue_status()
        running = None
        pending = []
        live_error = ""
        if response.get("success") and isinstance(response.get("queue"), dict):
            queue = response["queue"]
            running = self._queue_task(queue.get("running"))
            pending = [self._queue_task(item) for item in queue.get("pending", []) if isinstance(item, dict)]
        else:
            live_error = str(response.get("message") or response.get("error") or "실시간 큐 상태를 불러오지 못했습니다.")

        active_key = running.get("key") if running else None
        for task in data.get("tasks", []):
            task["display_status"] = task.get("status") or "-"
            task["display_stage"] = task.get("stage") or "-"
            task["is_active"] = False
            if active_key and task.get("task_key") == active_key and task.get("status") in {"running", "exit_pending"}:
                task["display_status"] = "running"
                task["display_stage"] = running.get("stage") or task["display_stage"]
                task["is_active"] = True

        lazy_active = bool(running and running.get("type") == "lazy_scan")
        if not running:
            lazy_active = any(
                task.get("task_type") == "lazy_scan" and task.get("status") in {"running", "exit_pending"}
                for task in data.get("tasks", [])
            )
        data["live_queue"] = {
            "success": not bool(live_error),
            "running": running,
            "pending": pending,
            "pending_count": len(pending),
            "error": live_error,
        }
        lazy_progress = read_lazy_progress(settings.get("bookoasis_log_dir")) if lazy_active else None
        if lazy_progress and running and running.get("started_at"):
            try:
                started_timestamp = datetime.strptime(running["started_at"], "%Y-%m-%d %H:%M:%S").timestamp()
                if lazy_progress.get("modified_timestamp", 0) < started_timestamp:
                    lazy_progress = None
            except (TypeError, ValueError):
                pass
        data["lazy_progress"] = lazy_progress
        self._debug(
            "스캔 상태 조회 완료",
            db_type=db_type,
            libraries=len(data.get("libraries", [])),
            tasks=len(data.get("tasks", [])),
            live_queue=str(not bool(live_error)).lower(),
            active_type=running.get("type") if running else "none",
            duration_ms=self._duration_ms(started),
        )
        return data

    @staticmethod
    def _queue_task(task):
        if not isinstance(task, dict):
            return None
        return {
            "type": str(task.get("type") or ""),
            "key": str(task.get("key") or ""),
            "enqueued_at": task.get("enqueued_at"),
            "started_at": task.get("started_at"),
            "stage": str(task.get("stage") or ""),
            "library_name": str(task.get("library_name") or ""),
        }

    def log_catalog(self):
        started = time.monotonic()
        try:
            data = list_log_files(self.settings().get("bookoasis_log_dir"))
        except (ValueError, OSError) as error:
            data = {"success": False, "files": [], "message": str(error)}
        self._debug(
            "BookOasis 로그 목록 조회 완료",
            files=len(data.get("files", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def log_tail(self, filename, cursor_identity="", cursor_offset=None, line_limit=500):
        started = time.monotonic()
        try:
            data = read_log_tail(
                self.settings().get("bookoasis_log_dir"),
                str(filename or ""),
                cursor_identity=cursor_identity,
                cursor_offset=cursor_offset,
                max_lines=_as_int(line_limit, 500, 10, 2000),
            )
        except (ValueError, OSError) as error:
            data = {"success": False, "file": str(filename or ""), "text": "", "message": str(error)}
        self._debug(
            "BookOasis 로그 tail 조회 완료",
            file=data.get("file"),
            bytes=len(data.get("text", "").encode("utf-8")),
            reset=str(data.get("reset", False)).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def request_rescan(self, db_type="general", library_id=None, all_libraries=False, force=False):
        started = time.monotonic()
        settings = self.settings()
        libraries = self.engine(settings).scanner_status(db_type=db_type, limit=10)["libraries"]
        if all_libraries:
            selected = libraries
        else:
            try:
                selected_id = int(library_id)
            except (TypeError, ValueError):
                raise ValueError("보관함 ID가 올바르지 않습니다.")
            selected = [item for item in libraries if int(item["id"]) == selected_id]
        if not selected:
            raise ValueError("재스캔할 보관함을 찾을 수 없습니다.")
        client = BookOasisClient(settings.get("bookoasis_url"), settings.get("api_timeout", 30))
        results = [client.request_scan(settings.get("webhook_token"), item["id"], db_type, _as_bool(force)) for item in selected]
        data = {
            "success": all(result["success"] for result in results),
            "requested": len(results),
            "queued": sum(1 for result in results if result["success"] and not result.get("already_queued")),
            "already_queued": sum(1 for result in results if result.get("already_queued")),
            "results": results,
        }
        self._debug(
            "재스캔 요청 완료",
            db_type=db_type,
            scope="all" if all_libraries else "single",
            requested=data["requested"],
            queued=data["queued"],
            already_queued=data["already_queued"],
            success=str(data["success"]).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def scan_book(self, book_id, db_type="general"):
        started = time.monotonic()
        data = self.admin_client().scan_book(book_id, db_type)
        if data.get("success"):
            self.invalidate()
        self._debug(
            "개별 도서 재스캔 완료",
            db_type=db_type,
            book_id=book_id,
            success=str(bool(data.get("success"))).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def metadata_plugins(self):
        started = time.monotonic()
        data = self.admin_client().metadata_plugins()
        if data.get("success"):
            data = {
                "success": True,
                "plugins": [
                    {"id": str(item.get("id") or ""), "name": str(item.get("name") or item.get("id") or "")}
                    for item in data.get("plugins", [])
                    if item.get("id")
                ],
            }
        self._debug(
            "메타데이터 플러그인 조회 완료",
            success=str(bool(data.get("success"))).lower(),
            plugins=len(data.get("plugins", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def search_metadata(self, query, source=None, db_type="general"):
        started = time.monotonic()
        data = self.admin_client().search_metadata(query, source, db_type)
        self._debug(
            "메타데이터 검색 완료",
            db_type=db_type,
            source=str(source or "default")[:80],
            success=str(bool(data.get("success"))).lower(),
            results=len(data.get("results", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def apply_metadata(self, book_id, item_data, source=None, db_type="general"):
        started = time.monotonic()
        data = self.admin_client().apply_metadata(book_id, item_data, source, db_type)
        if data.get("success"):
            self.invalidate()
        self._debug(
            "메타데이터 적용 완료",
            db_type=db_type,
            book_id=book_id,
            source=str(source or "default")[:80],
            success=str(bool(data.get("success"))).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def gaps(self, **kwargs):
        started = time.monotonic()
        settings = self.settings()
        page = _as_int(kwargs.pop("page", 1), 1, 1, 100000)
        page_size = _as_int(
            kwargs.pop("page_size", settings["page_size"]),
            settings["page_size"],
            10,
            200,
        )
        db_type = kwargs.get("db_type", "general")
        library_id = kwargs.get("library_id")
        search = kwargs.get("search", "")
        analysis, cache_hit = self._series_gap_analysis(
            settings,
            db_type=db_type,
            library_id=library_id,
            search=search,
        )
        offset = (page - 1) * page_size
        data = {
            "items": analysis["items"][offset:offset + page_size],
            "total": analysis["total"],
            "page": page,
            "page_size": page_size,
            "pages": (analysis["total"] + page_size - 1) // page_size,
            "analyzed_books": analysis["analyzed_books"],
            "duration_ms": analysis["duration_ms"],
            "cache_hit": cache_hit,
        }
        self._debug(
            "시리즈 누락 분석 완료",
            db_type=db_type,
            library_id=library_id or "all",
            search=str(bool(search)).lower(),
            analyzed_books=data.get("analyzed_books", 0),
            candidates=data.get("total", 0),
            cache_hit=str(cache_hit).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def _series_gap_analysis(self, settings, db_type="general", library_id=None, search=""):
        normalized_db_type = str(db_type or "general").strip()
        normalized_library_id = str(library_id or "").strip()
        normalized_search = str(search or "").strip()
        target_path = (
            settings.get("adult_db_path")
            if normalized_db_type == "adult"
            else settings.get("general_db_path")
        )
        cache_key = (
            normalized_db_type,
            normalized_library_id,
            normalized_search,
            str(target_path or ""),
        )

        def cached_value():
            with self._lock:
                cached = self._gap_cache.get(cache_key)
                if cached and time.monotonic() - cached["created_at"] <= GAP_CACHE_SECONDS:
                    return cached["data"]
            return None

        cached = cached_value()
        if cached is not None:
            return cached, True

        with self._gap_analysis_lock:
            cached = cached_value()
            if cached is not None:
                return cached, True
            data = self.engine(settings).analyze_series_gaps(
                db_type=normalized_db_type,
                library_id=normalized_library_id,
                search=normalized_search,
            )
            with self._lock:
                now = time.monotonic()
                self._gap_cache = {
                    key: value
                    for key, value in self._gap_cache.items()
                    if now - value["created_at"] <= GAP_CACHE_SECONDS
                }
                if len(self._gap_cache) >= GAP_CACHE_LIMIT:
                    oldest_key = min(
                        self._gap_cache,
                        key=lambda key: self._gap_cache[key]["created_at"],
                    )
                    self._gap_cache.pop(oldest_key, None)
                self._gap_cache[cache_key] = {"created_at": now, "data": data}
            return data, False

    def gaps_csv(self, db_type="general", library_id=None, search=""):
        started = time.monotonic()
        settings = self.settings()
        analysis, cache_hit = self._series_gap_analysis(
            settings,
            db_type=db_type,
            library_id=library_id,
            search=search,
        )
        content = build_series_gap_csv(analysis["items"])
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"bookoasis_series_gaps_{db_type}_{timestamp}.csv"
        self._debug(
            "시리즈 누락 CSV 생성 완료",
            db_type=db_type,
            library_id=library_id or "all",
            search=str(bool(search)).lower(),
            candidates=analysis.get("total", 0),
            cache_hit=str(cache_hit).lower(),
            bytes=len(content),
            duration_ms=self._duration_ms(started),
        )
        return {
            "content": content,
            "filename": filename,
            "total": analysis["total"],
            "cache_hit": cache_hit,
        }

    def covers(self, db_type="general", library_id=None, mode="missing", search="", page=1, page_size=None, force=False):
        started = time.monotonic()
        settings = self.settings()
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size or min(settings["page_size"], 25), 25, 1, 200)
        mode = {"http": "missing", "file": "resolution"}.get(mode, mode)
        mode = mode if mode in {"missing", "resolution", "file_size", "aspect"} else "missing"
        target_issue = {
            "missing": "missing_reference",
            "resolution": "low_resolution",
            "file_size": "small_file",
            "aspect": "abnormal_aspect_ratio",
        }[mode]
        file_mode = mode != "missing"
        search = str(search or "").strip()
        force = _as_bool(force, False)
        cache_key = (
            db_type,
            str(library_id or ""),
            mode,
            search,
            str(settings.get("general_db_path") or ""),
            str(settings.get("adult_db_path") or ""),
            str(settings.get("cover_root_path") or ""),
            settings["cover_min_width"],
            settings["cover_min_height"],
            settings["cover_min_file_size_kb"],
            settings["cover_min_aspect_percent"],
        )
        with self._lock:
            cached = self._cover_issue_cache if self._cover_issue_cache_key == cache_key else None

        cache_hit = not force and cached is not None
        if not cache_hit:
            root = settings.get("cover_root_path")
            if file_mode and (not root or not Path(root).is_dir()):
                raise FileNotFoundError("설정된 표지 디렉터리를 찾을 수 없습니다.")
            engine = self.engine(settings)
            issues = []
            source_total = 0
            inspected_count = 0
            source_page = 1
            executor = ThreadPoolExecutor(max_workers=4) if file_mode else None
            try:
                while True:
                    batch = engine.cover_items(
                        db_type=db_type,
                        library_id=library_id,
                        search=search,
                        page=source_page,
                        page_size=1000,
                    )
                    if source_page == 1:
                        source_total = batch["total"]
                    items = batch["items"]
                    if file_mode:
                        inspections = list(executor.map(
                            lambda item: inspect_cover_file(
                                root,
                                item.get("cover_path"),
                                min_width=settings["cover_min_width"],
                                min_height=settings["cover_min_height"],
                                min_file_size=settings["cover_min_file_size_kb"] * 1024,
                                min_aspect_ratio=settings["cover_min_aspect_percent"] / 100,
                            ),
                            items,
                        ))
                    else:
                        inspections = [
                            {"status": "missing_reference"} if not item.get("cover_path") else {"status": "ok"}
                            for item in items
                        ]
                    inspected_count += len(items)
                    for item, inspection in zip(items, inspections):
                        issue_codes = inspection.get("issues") or [inspection.get("status")]
                        if target_issue not in issue_codes:
                            continue
                        inspection["status"] = target_issue
                        inspection["issues"] = [target_issue]
                        item["inspection"] = inspection
                        issues.append(item)
                    if source_page >= batch["pages"] or not items:
                        break
                    source_page += 1
            finally:
                if executor is not None:
                    executor.shutdown(wait=True)

            cached = {
                "items": issues,
                "source_total": source_total,
                "inspected_count": inspected_count,
                "duration_ms": self._duration_ms(started),
            }
            with self._lock:
                self._cover_issue_cache_key = cache_key
                self._cover_issue_cache = cached

        issue_count = len(cached["items"])
        pages = (issue_count + page_size - 1) // page_size
        page = min(page, max(1, pages))
        offset = (page - 1) * page_size
        data = {
            "items": cached["items"][offset:offset + page_size],
            "total": issue_count,
            "source_total": cached["source_total"],
            "inspected_count": cached["inspected_count"],
            "issue_count": issue_count,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "mode": mode,
            "duration_ms": cached["duration_ms"],
            "cache_hit": cache_hit,
        }
        self._debug(
            "표지 검사 완료",
            db_type=db_type,
            library_id=library_id or "all",
            mode=mode,
            search=str(bool(search)).lower(),
            page=data.get("page", 1),
            source_total=data["source_total"],
            inspected=data["inspected_count"],
            issues=issue_count,
            cache_hit=str(cache_hit).lower(),
            duration_ms=data["duration_ms"],
        )
        return data

    def orphan_cleanup_status(self):
        with self._lock:
            return copy.deepcopy(self._orphan_cleanup_status)

    def start_orphan_cleanup(self, db_type="general", library_id=None, dry_run=True, confirm_delete=False):
        settings = self.settings()
        root = Path(settings.get("cover_root_path") or "").expanduser()
        if not root.is_dir():
            raise FileNotFoundError("설정된 표지 디렉터리를 찾을 수 없습니다.")
        dry_run = _as_bool(dry_run, True)
        if not dry_run and not _as_bool(confirm_delete, False):
            raise ValueError("실제 삭제 확인값이 누락되었습니다. Dry Run으로 먼저 확인해 주세요.")

        engine = self.engine(settings)
        libraries = engine.scanner_status(db_type=db_type, limit=500)["libraries"]
        selected = libraries
        selected_library_id = None
        selected_name = "전체 보관함"
        if str(library_id or "").strip():
            try:
                selected_library_id = int(library_id)
            except (TypeError, ValueError):
                raise ValueError("보관함 ID가 올바르지 않습니다.")
            selected = [item for item in libraries if int(item["id"]) == selected_library_id]
            if selected:
                selected_name = selected[0].get("name") or f"보관함 {selected_library_id}"
        if not selected:
            raise ValueError("정리할 보관함을 찾을 수 없습니다.")

        with self._lock:
            if self._orphan_cleanup_status.get("is_working") == "run":
                return {
                    "started": False,
                    "message": "고아 표지파일 정리가 이미 실행 중입니다.",
                    "status": copy.deepcopy(self._orphan_cleanup_status),
                }
            self._orphan_cleanup_stop.clear()
            self._orphan_cleanup_status = self._empty_orphan_cleanup_status()
            self._orphan_cleanup_status.update({
                "is_working": "run",
                "dry_run": dry_run,
                "db_type": db_type,
                "library_id": selected_library_id,
                "library_name": selected_name,
                "message": "표지 참조와 파일을 분석하는 중입니다.",
            })

        library_ids = [int(item["id"]) for item in selected]
        worker = threading.Thread(
            target=self._run_orphan_cleanup,
            args=(settings, db_type, selected_library_id, selected_name, library_ids, dry_run),
            name="ff-library-doctor-orphan-covers",
        )
        worker.daemon = True
        self._orphan_cleanup_thread = worker
        worker.start()
        self._debug(
            "고아 표지파일 정리 시작",
            db_type=db_type,
            library_id=selected_library_id or "all",
            libraries=len(library_ids),
            dry_run=str(dry_run).lower(),
        )
        return {
            "started": True,
            "message": "Dry Run을 시작했습니다." if dry_run else "고아 표지파일 정리를 시작했습니다.",
            "status": self.orphan_cleanup_status(),
        }

    def _run_orphan_cleanup(self, settings, db_type, library_id, library_name, library_ids, dry_run):
        started = time.monotonic()

        def publish(progress):
            with self._lock:
                current = self._orphan_cleanup_status
                for key in (
                    "scanned_count", "target_count", "target_size", "deleted_count",
                    "deleted_size", "error_count", "items", "truncated", "stopped",
                ):
                    current[key] = copy.deepcopy(progress.get(key, current.get(key)))
                current["message"] = "중지 요청을 처리하는 중입니다." if self._orphan_cleanup_stop.is_set() else "정리 작업을 실행 중입니다."

        try:
            engine = self.engine(settings)
            references = engine.all_cover_references(include_inactive_adult=True)
            result = cleanup_orphan_files(
                settings.get("cover_root_path"),
                references,
                library_ids,
                dry_run=dry_run,
                result_limit=500,
                should_stop=self._orphan_cleanup_stop.is_set,
                on_progress=publish,
            )
            publish(result)
            with self._lock:
                self._orphan_cleanup_status.update({
                    "is_working": "stop" if result["stopped"] else "wait",
                    "db_type": db_type,
                    "library_id": library_id,
                    "library_name": library_name,
                    "message": (
                        "사용자 요청으로 작업을 중지했습니다."
                        if result["stopped"]
                        else ("Dry Run을 완료했습니다." if dry_run else "고아 표지파일 정리를 완료했습니다.")
                    ),
                    "duration_ms": self._duration_ms(started),
                })
            self._debug(
                "고아 표지파일 정리 완료",
                db_type=db_type,
                library_id=library_id or "all",
                dry_run=str(dry_run).lower(),
                scanned=result["scanned_count"],
                targets=result["target_count"],
                deleted=result["deleted_count"],
                errors=result["error_count"],
                stopped=str(result["stopped"]).lower(),
                duration_ms=self._duration_ms(started),
            )
        except Exception as error:
            self.P.logger.error(f"BookOasis Mate 고아 표지파일 정리 실패: {error}")
            with self._lock:
                self._orphan_cleanup_status.update({
                    "is_working": "error",
                    "message": "고아 표지파일 정리에 실패했습니다. 플러그인 로그를 확인해 주세요.",
                    "duration_ms": self._duration_ms(started),
                })

    def stop_orphan_cleanup(self):
        with self._lock:
            if self._orphan_cleanup_status.get("is_working") != "run":
                return {
                    "requested": False,
                    "message": "실행 중인 고아 표지파일 정리 작업이 없습니다.",
                    "status": copy.deepcopy(self._orphan_cleanup_status),
                }
            self._orphan_cleanup_stop.set()
            self._orphan_cleanup_status["message"] = "중지 요청을 처리하는 중입니다."
        self._debug("고아 표지파일 정리 중지 요청")
        return {
            "requested": True,
            "message": "중지를 요청했습니다.",
            "status": self.orphan_cleanup_status(),
        }

    def migration_config(self):
        model = self.P.ModelSetting
        return {
            "work_dir": str(model.get("migration_work_dir") or "").strip(),
            "operation": str(model.get("migration_operation") or "export").strip(),
            "export_db_type": str(
                model.get("migration_export_db_type") or "general"
            ).strip(),
            "export_library_ids": parse_library_ids(
                model.get("migration_export_library_ids")
            ),
            "import_package": str(
                model.get("migration_import_package") or ""
            ).strip(),
            "import_db_type": str(
                model.get("migration_import_db_type") or "auto"
            ).strip(),
            "import_mode": str(
                model.get("migration_import_mode") or "new"
            ).strip(),
            "merge_library_id": str(
                model.get("migration_merge_library_id") or ""
            ).strip(),
            "import_name": str(model.get("migration_import_name") or "").strip(),
            "import_target_paths": parse_paths(
                model.get("migration_import_target_paths")
            ),
            "backup_before_import": model.get_bool(
                "migration_backup_before_import"
            ),
        }

    def _migration_engine(self, config=None, on_progress=None):
        config = config or self.migration_config()
        settings = self.settings()
        return CategoryMigrationEngine(
            config.get("work_dir"),
            settings.get("cover_root_path"),
            should_stop=self._migration_stop.is_set,
            on_progress=on_progress,
        )

    def migration_libraries(self, db_type="general"):
        target = self.engine().get_target(db_type)
        data = CategoryMigrationEngine.libraries(target.path)
        self._debug("이관 카테고리 목록 조회", db_type=db_type, count=len(data))
        return data

    def migration_packages(self, work_dir=None):
        config = self.migration_config()
        if str(work_dir or "").strip():
            config["work_dir"] = str(work_dir).strip()
        packages = self._migration_engine(config).list_packages()
        self._debug("이관 패키지 목록 조회", count=len(packages))
        return packages

    def inspect_migration_package(self, package_path=None, work_dir=None):
        config = self.migration_config()
        if str(work_dir or "").strip():
            config["work_dir"] = str(work_dir).strip()
        selected = str(package_path or config.get("import_package") or "").strip()
        data = self._migration_engine(config).inspect_package(selected)
        self._debug(
            "이관 패키지 검사 완료",
            db_type=data.get("db_type"),
            books=data.get("books_count", 0),
            covers=data.get("covers_count", 0),
            roots=data.get("root_paths_count", 0),
        )
        return data

    def migration_status(self):
        with self._lock:
            status = copy.deepcopy(self._migration_status)
            if (
                status.get("is_working") == "run"
                and self._migration_started_monotonic is not None
            ):
                status["elapsed_seconds"] = round(
                    time.monotonic() - self._migration_started_monotonic,
                    1,
                )
            return status

    def _append_migration_log(self, message):
        text = str(message or "").strip()
        if not text:
            return
        logs = self._migration_status["logs"]
        if logs and logs[-1]["message"] == text:
            logs[-1]["time"] = datetime.now().strftime("%H:%M:%S")
            return
        logs.append(
            {
                "time": datetime.now().strftime("%H:%M:%S"),
                "message": text,
            }
        )
        if len(logs) > 300:
            del logs[:-300]

    def _publish_migration(self, progress):
        with self._lock:
            current = int(progress.get("current") or 0)
            total = int(progress.get("total") or 0)
            stage = str(progress.get("stage") or "")
            previous_stage = self._migration_status.get("stage")
            self._migration_status.update(
                {
                    "stage": stage,
                    "current": current,
                    "total": total,
                    "progress_percent": (
                        min(100, round(current * 100 / total))
                        if total > 0
                        else 0
                    ),
                    "message": (
                        "중지 요청을 처리하는 중입니다."
                        if self._migration_stop.is_set()
                        else str(progress.get("message") or "작업을 실행 중입니다.")
                    ),
                }
            )
            if (
                stage != previous_stage
                or current in {0, 1, total}
                or (current > 0 and current % 100 == 0)
            ):
                self._append_migration_log(
                    progress.get("log") or progress.get("message")
                )

    def start_migration(self):
        config = self.migration_config()
        operation = config.get("operation")
        if operation not in {"export", "import"}:
            raise ValueError("이관 작업 유형이 올바르지 않습니다.")
        if not config.get("work_dir"):
            raise ValueError("이관 작업 디렉터리를 설정해 주세요.")
        if operation == "export" and not config.get("export_library_ids"):
            raise ValueError("내보낼 카테고리를 선택해 주세요.")
        if operation == "import":
            if not config.get("import_package"):
                raise ValueError("가져올 패키지를 선택해 주세요.")
            if not config.get("import_target_paths"):
                raise ValueError("가져올 대상 물리 경로를 입력해 주세요.")
            if config.get("import_mode") not in {"new", "merge"}:
                raise ValueError("가져오기 방식이 올바르지 않습니다.")
            if (
                config.get("import_mode") == "merge"
                and not config.get("merge_library_id")
            ):
                raise ValueError("병합할 기존 카테고리를 선택해 주세요.")

        with self._lock:
            if self._migration_status.get("is_working") == "run":
                return {
                    "started": False,
                    "message": "카테고리 이관 작업이 이미 실행 중입니다.",
                    "status": self.migration_status(),
                }
            self._migration_stop.clear()
            self._migration_started_monotonic = time.monotonic()
            self._migration_status = self._empty_migration_status()
            self._migration_status.update(
                {
                    "is_working": "run",
                    "operation": operation,
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message": (
                        "카테고리 내보내기를 준비하고 있습니다."
                        if operation == "export"
                        else "카테고리 가져오기를 준비하고 있습니다."
                    ),
                }
            )
            self._append_migration_log(self._migration_status["message"])

        worker = threading.Thread(
            target=self._run_migration,
            args=(config,),
            name="bookoasis-mate-category-migration",
        )
        worker.daemon = True
        self._migration_thread = worker
        worker.start()
        self._debug("카테고리 이관 시작", operation=operation)
        return {
            "started": True,
            "message": "카테고리 이관 작업을 시작했습니다.",
            "status": self.migration_status(),
        }

    def _run_migration(self, config):
        started = time.monotonic()
        operation = config["operation"]
        try:
            engine = self._migration_engine(
                config,
                on_progress=self._publish_migration,
            )
            settings = self.settings()
            if operation == "export":
                db_type = config["export_db_type"]
                target = self.engine(settings).get_target(db_type)
                result = engine.export_categories(
                    target.path,
                    db_type,
                    config["export_library_ids"],
                )
            else:
                inspection = engine.inspect_package(config["import_package"])
                requested_type = config["import_db_type"]
                db_type = (
                    inspection["db_type"]
                    if requested_type == "auto"
                    else requested_type
                )
                target = self.engine(settings).get_target(db_type)
                result = engine.import_category(
                    target.path,
                    config["import_package"],
                    config["import_target_paths"],
                    db_type=db_type,
                    name=config["import_name"] or None,
                    merge_to=(
                        config["merge_library_id"]
                        if config["import_mode"] == "merge"
                        else None
                    ),
                    backup=config["backup_before_import"],
                    inspection=inspection,
                )
            with self._lock:
                self._migration_status.update(
                    {
                        "is_working": "wait",
                        "progress_percent": 100,
                        "message": (
                            "카테고리 내보내기를 완료했습니다."
                            if operation == "export"
                            else (
                                "기존 카테고리 병합을 완료했습니다."
                                if result.get("mode") == "merge"
                                else "신규 카테고리 가져오기를 완료했습니다."
                            )
                        ),
                        "result": result,
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    }
                )
                self._append_migration_log(self._migration_status["message"])
            self.invalidate()
            self._debug(
                "카테고리 이관 완료",
                operation=operation,
                duration_ms=self._duration_ms(started),
            )
        except MigrationStopped:
            with self._lock:
                self._migration_status.update(
                    {
                        "is_working": "stop",
                        "message": "사용자 요청으로 카테고리 이관을 중지했습니다.",
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    }
                )
                self._append_migration_log(self._migration_status["message"])
            self._debug(
                "카테고리 이관 중지",
                operation=operation,
                duration_ms=self._duration_ms(started),
            )
        except Exception as error:
            self.P.logger.error(f"BookOasis Mate 카테고리 이관 실패: {error}")
            with self._lock:
                self._migration_status.update(
                    {
                        "is_working": "error",
                        "message": "카테고리 이관에 실패했습니다.",
                        "error": str(error),
                        "elapsed_seconds": round(time.monotonic() - started, 1),
                    }
                )
                self._append_migration_log(
                    f"{self._migration_status['message']} {error}"
                )

    def stop_migration(self):
        with self._lock:
            if self._migration_status.get("is_working") != "run":
                return {
                    "requested": False,
                    "message": "실행 중인 카테고리 이관 작업이 없습니다.",
                    "status": self.migration_status(),
                }
            self._migration_stop.set()
            self._migration_status["message"] = "중지 요청을 처리하는 중입니다."
            self._append_migration_log("사용자가 작업 중지를 요청했습니다.")
        self._debug("카테고리 이관 중지 요청")
        return {
            "requested": True,
            "message": "카테고리 이관 중지를 요청했습니다.",
            "status": self.migration_status(),
        }

    def database_migration_config(self, values=None):
        model = self.P.ModelSetting
        raw = {
            "database_migration_source": model.get("database_migration_source"),
            "kavita_db_path": model.get("kavita_db_path"),
            "kavita_cover_path": model.get("kavita_cover_path"),
            "kavita_target_db_type": model.get("kavita_target_db_type"),
            "kavita_selected_libraries": model.get("kavita_selected_libraries"),
            "kavita_path_mappings": model.get("kavita_path_mappings"),
            "kavita_user_mappings": model.get("kavita_user_mappings"),
            "kavita_import_covers": model.get("kavita_import_covers"),
            "kavita_import_progress": model.get("kavita_import_progress"),
            "kavita_lock_metadata": model.get("kavita_lock_metadata"),
            "kavita_backup_before_import": model.get("kavita_backup_before_import"),
            "kavita_dry_run": model.get("kavita_dry_run"),
            "bookoasis_db_package_path": model.get(
                "bookoasis_db_package_path"
            ),
            "bookoasis_cover_package_path": model.get(
                "bookoasis_cover_package_path"
            ),
            "bookoasis_package_action": model.get(
                "bookoasis_package_action"
            ),
            "bookoasis_export_name": model.get("bookoasis_export_name"),
            "bookoasis_package_path_mappings": model.get(
                "bookoasis_package_path_mappings"
            ),
            "bookoasis_backup_before_import": model.get(
                "bookoasis_backup_before_import"
            ),
            "bookoasis_package_dry_run": model.get(
                "bookoasis_package_dry_run"
            ),
            "migration_work_dir": model.get("migration_work_dir"),
        }
        if values:
            raw.update(dict(values))
        source_type = str(
            raw.get("database_migration_source") or "kavita"
        ).strip().lower()
        target_db_type = str(
            raw.get("kavita_target_db_type") or "general"
        ).strip().lower()
        package_action = str(
            raw.get("bookoasis_package_action") or "import"
        ).strip().lower()
        return {
            "source_type": source_type,
            "kavita_db_path": str(raw.get("kavita_db_path") or "").strip(),
            "kavita_cover_path": str(raw.get("kavita_cover_path") or "").strip(),
            "target_db_type": target_db_type,
            "selected_libraries": parse_name_list(
                raw.get("kavita_selected_libraries")
            ),
            "path_mappings": str(raw.get("kavita_path_mappings") or "").strip(),
            "user_mappings": str(raw.get("kavita_user_mappings") or "").strip(),
            "import_covers": _as_bool(raw.get("kavita_import_covers"), True),
            "import_progress": _as_bool(raw.get("kavita_import_progress"), False),
            "lock_metadata": _as_bool(raw.get("kavita_lock_metadata"), True),
            "backup": _as_bool(raw.get("kavita_backup_before_import"), True),
            "dry_run": _as_bool(raw.get("kavita_dry_run"), True),
            "bookoasis_db_package_path": str(
                raw.get("bookoasis_db_package_path") or ""
            ).strip(),
            "bookoasis_cover_package_path": str(
                raw.get("bookoasis_cover_package_path") or ""
            ).strip(),
            "bookoasis_package_action": package_action,
            "bookoasis_export_name": str(
                raw.get("bookoasis_export_name") or "dist_books"
            ).strip(),
            "bookoasis_path_mappings": str(
                raw.get("bookoasis_package_path_mappings") or ""
            ).strip(),
            "bookoasis_backup": _as_bool(
                raw.get("bookoasis_backup_before_import"), True
            ),
            "bookoasis_dry_run": _as_bool(
                raw.get("bookoasis_package_dry_run"), True
            ),
            "bookoasis_confirm_stopped": _as_bool(
                raw.get("bookoasis_confirm_stopped"), False
            ),
            "work_dir": str(raw.get("migration_work_dir") or "").strip(),
        }

    def _database_migration_engine(self, config, on_progress=None):
        settings = self.settings()
        common = {
            "should_stop": self._database_migration_stop.is_set,
            "on_progress": on_progress,
        }
        if config["source_type"] == "kavita":
            target = self.engine(settings).get_target(config["target_db_type"])
            return KavitaMigrationEngine(
                config["kavita_db_path"],
                config["kavita_cover_path"],
                target.path,
                settings.get("cover_root_path"),
                config["work_dir"],
                **common,
            )
        if config["source_type"] == "bookoasis":
            return BookOasisPackageImportEngine(
                config["work_dir"],
                settings.get("general_db_path"),
                settings.get("adult_db_path"),
                settings.get("cover_root_path"),
                health_check=lambda: BookOasisClient(
                    settings.get("bookoasis_url"),
                    settings.get("api_timeout", 30),
                ).health(),
                **common,
            )
        raise ValueError("지원하지 않는 이관 원본 유형입니다.")

    def inspect_database_migration(self, values=None):
        config = self.database_migration_config(values)
        engine = self._database_migration_engine(config)
        if config["source_type"] == "kavita":
            data = engine.inspect(
                path_mappings=config["path_mappings"],
                selected_libraries=None,
            )
            data["source_type"] = "kavita"
        else:
            data = engine.inspect(
                config["bookoasis_db_package_path"],
                config["bookoasis_cover_package_path"],
                path_mappings=config["bookoasis_path_mappings"],
            )
        self._debug(
            "통DB 이관 미리보기 완료",
            source_type=config["source_type"],
            books=data.get("books_count", 0),
            matched=data.get("matched_books_count", data.get("books_count", 0)),
        )
        return data

    def database_migration_packages(self, values=None):
        config = self.database_migration_config(values)
        config["source_type"] = "bookoasis"
        packages = self._database_migration_engine(config).list_packages()
        self._debug(
            "통DB 공유 패키지 목록 조회",
            databases=len(packages.get("databases", [])),
            covers=len(packages.get("covers", [])),
        )
        return packages

    def inspect_bookoasis_database_package(self, values=None):
        config = self.database_migration_config(values)
        config["source_type"] = "bookoasis"
        data = self._database_migration_engine(config).inspect_database_package(
            config["bookoasis_db_package_path"],
            path_mappings=config["bookoasis_path_mappings"],
        )
        self._debug(
            "통DB 공유 DB 패키지 검사 완료",
            books=data.get("books_count", 0),
            libraries=data.get("libraries_count", 0),
            source_parent=data.get("suggested_mapping_source", ""),
        )
        return data

    @staticmethod
    def _database_migration_worker_paths(work_dir):
        root = Path(str(work_dir or "")).expanduser().resolve()
        return {
            "config": root / ".bookoasis_mate_database_migration_job.json",
            "status": root / ".bookoasis_mate_database_migration_status.json",
            "stop": root / ".bookoasis_mate_database_migration_stop",
            "log": root / ".bookoasis_mate_database_migration_worker.log",
        }

    @staticmethod
    def _write_database_migration_json(path, data):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(target))

    @staticmethod
    def _read_database_migration_json(path):
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _external_database_migration_status(self):
        status_path = self._database_migration_status_path
        if status_path is None:
            work_dir = str(
                self.settings().get("migration_work_dir") or ""
            ).strip()
            if not work_dir:
                return None
            status_path = self._database_migration_worker_paths(work_dir)[
                "status"
            ]
        status = self._read_database_migration_json(status_path)
        if (
            not status
            or status.get("operation") not in {"kavita", "bookoasis"}
        ):
            return None
        started_epoch = float(status.get("started_epoch") or 0)
        if status.get("is_working") == "run" and started_epoch:
            status["elapsed_seconds"] = round(
                max(0, time.time() - started_epoch),
                1,
            )
        process = self._database_migration_process
        if (
            status.get("is_working") == "run"
            and process is not None
            and process.poll() is not None
        ):
            status.update(
                {
                    "is_working": "error",
                    "message": "통DB 이관 작업 프로세스가 종료되었습니다.",
                    "error": (
                        "작업 프로세스가 완료 상태를 기록하지 못했습니다. "
                        f"종료 코드: {process.returncode}"
                    ),
                }
            )
            self._write_database_migration_json(status_path, status)
        return status

    def database_migration_status(self):
        with self._lock:
            external = self._external_database_migration_status()
            if external:
                completed_now = (
                    self._database_migration_status.get("is_working") == "run"
                    and external.get("is_working") == "wait"
                )
                self._database_migration_status = copy.deepcopy(external)
                if completed_now:
                    self._cached_report = None
                    self._cached_at = 0.0
                    self._settings_fingerprint = None
                    self._cover_issue_cache_key = None
                    self._cover_issue_cache = None
            status = copy.deepcopy(self._database_migration_status)
            if (
                status.get("is_working") == "run"
                and self._database_migration_started_monotonic is not None
            ):
                status["elapsed_seconds"] = round(
                    time.monotonic() - self._database_migration_started_monotonic,
                    1,
                )
            return status

    def _start_database_migration_process(self, config):
        settings = self.settings()
        paths = self._database_migration_worker_paths(config["work_dir"])
        paths["config"].parent.mkdir(parents=True, exist_ok=True)
        if paths["stop"].exists():
            paths["stop"].unlink()
        worker_config = {
            **config,
            "target_general_db": settings.get("general_db_path"),
            "target_adult_db": settings.get("adult_db_path"),
            "target_cover_root": settings.get("cover_root_path"),
            "bookoasis_url": settings.get("bookoasis_url"),
            "api_timeout": settings.get("api_timeout", 30),
        }
        self._database_migration_status_path = paths["status"]
        self._database_migration_stop_path = paths["stop"]
        self._write_database_migration_json(
            paths["config"],
            worker_config,
        )
        self._write_database_migration_json(
            paths["status"],
            self._database_migration_status,
        )
        worker_script = Path(__file__).with_name(
            "database_migration_worker.py"
        )
        command = [
            sys.executable,
            str(worker_script),
            str(paths["config"]),
            str(paths["status"]),
            str(paths["stop"]),
        ]
        with paths["log"].open("ab") as output:
            process = subprocess.Popen(
                command,
                cwd=str(worker_script.parent),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._database_migration_process = process
        self._debug(
            "통DB 이관 별도 프로세스 시작",
            pid=process.pid,
            status_path=paths["status"],
        )
        return process

    def _append_database_migration_log(self, message):
        text = str(message or "").strip()
        if not text:
            return
        logs = self._database_migration_status["logs"]
        if logs and logs[-1]["message"] == text:
            logs[-1]["time"] = datetime.now().strftime("%H:%M:%S")
            return
        logs.append(
            {"time": datetime.now().strftime("%H:%M:%S"), "message": text}
        )
        if len(logs) > 300:
            del logs[:-300]

    def start_database_migration(self, values=None):
        config = self.database_migration_config(values)
        if config["source_type"] not in {"kavita", "bookoasis"}:
            raise ValueError("이관 원본 유형이 올바르지 않습니다.")
        if config["source_type"] == "kavita" and not config["kavita_db_path"]:
            raise ValueError("Kavita DB 파일을 설정해 주세요.")
        if config["source_type"] == "bookoasis":
            if config["bookoasis_package_action"] not in {"export", "import"}:
                raise ValueError("BookOasis 패키지 작업 유형이 올바르지 않습니다.")
            if config["bookoasis_package_action"] == "import":
                if not config["bookoasis_db_package_path"]:
                    raise ValueError("BookOasis DB 패키지를 설정해 주세요.")
                if not config["bookoasis_cover_package_path"]:
                    raise ValueError("BookOasis 표지 패키지를 설정해 주세요.")
            elif not config["bookoasis_export_name"]:
                raise ValueError("BookOasis 내보내기 이름을 입력해 주세요.")
        if not config["work_dir"]:
            raise ValueError("이관 작업 디렉터리를 설정해 주세요.")
        if config["target_db_type"] not in {"general", "adult"}:
            raise ValueError("대상 DB 유형이 올바르지 않습니다.")
        current_status = self.database_migration_status()
        if current_status.get("is_working") == "run":
            return {
                "started": False,
                "message": "통DB 이관 작업이 이미 실행 중입니다.",
                "status": current_status,
            }
        with self._lock:
            self._database_migration_stop.clear()
            self._database_migration_started_monotonic = time.monotonic()
            self._database_migration_status = self._empty_migration_status()
            dry_run = (
                config["dry_run"]
                if config["source_type"] == "kavita"
                else (
                    config["bookoasis_dry_run"]
                    if config["bookoasis_package_action"] == "import"
                    else False
                )
            )
            if config["source_type"] == "kavita":
                source_label = "Kavita"
            elif config["bookoasis_package_action"] == "export":
                source_label = "BookOasis 패키지 내보내기"
            else:
                source_label = "BookOasis"
            self._database_migration_status.update(
                {
                    "is_working": "run",
                    "operation": config["source_type"],
                    "package_action": config.get(
                        "bookoasis_package_action",
                        "",
                    ),
                    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message": (
                        "BookOasis 패키지 내보내기를 준비하고 있습니다."
                        if (
                            config["source_type"] == "bookoasis"
                            and config["bookoasis_package_action"] == "export"
                        )
                        else (
                            f"{source_label} Dry Run을 준비하고 있습니다."
                            if dry_run
                            else f"{source_label} 실제 이관을 준비하고 있습니다."
                        )
                    ),
                }
            )
            self._append_database_migration_log(
                self._database_migration_status["message"]
            )
        try:
            self._start_database_migration_process(config)
        except Exception as error:
            with self._lock:
                self._database_migration_status.update(
                    {
                        "is_working": "error",
                        "message": "통DB 이관 작업 프로세스를 시작하지 못했습니다.",
                        "error": str(error),
                    }
                )
                self._append_database_migration_log(
                    self._database_migration_status["message"]
                )
            raise
        return {
            "started": True,
            "message": "통DB 이관 작업을 시작했습니다.",
            "status": self.database_migration_status(),
        }

    def stop_database_migration(self):
        with self._lock:
            if self._database_migration_status.get("is_working") != "run":
                return {
                    "requested": False,
                    "message": "실행 중인 통DB 이관 작업이 없습니다.",
                    "status": self.database_migration_status(),
                }
            self._database_migration_stop.set()
            if self._database_migration_status.get("operation") in {
                "kavita",
                "bookoasis",
            }:
                stop_path = self._database_migration_stop_path
                if stop_path is None:
                    work_dir = str(
                        self.settings().get("migration_work_dir") or ""
                    ).strip()
                    if work_dir:
                        worker_paths = self._database_migration_worker_paths(
                            work_dir
                        )
                        stop_path = worker_paths["stop"]
                        self._database_migration_stop_path = stop_path
                        self._database_migration_status_path = worker_paths[
                            "status"
                        ]
                if stop_path is not None:
                    stop_path.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    stop_path.touch()
            self._database_migration_status["message"] = (
                "중지 요청을 처리하는 중입니다."
            )
            self._append_database_migration_log(
                "사용자가 통DB 이관 중지를 요청했습니다."
            )
            if self._database_migration_status_path is not None:
                self._write_database_migration_json(
                    self._database_migration_status_path,
                    self._database_migration_status,
                )
        return {
            "requested": True,
            "message": "통DB 이관 중지를 요청했습니다.",
            "status": self.database_migration_status(),
        }

    def quick_check(self, db_type="general", settings=None):
        started = time.monotonic()
        data = self.engine(settings).quick_check(db_type)
        self._debug(
            "DB 무결성 검사 완료",
            db_type=db_type,
            success=str(data.get("success", False)).lower(),
            result_count=len(data.get("result", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def database_details(self, db_type="general", settings=None):
        started = time.monotonic()
        data = self.engine(settings).database_details(db_type)
        self._debug(
            "DB 상세 정보 조회 완료",
            db_type=db_type,
            success=str(data.get("success", False)).lower(),
            libraries=len(data.get("libraries", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def connection_test(self, settings=None):
        started = time.monotonic()
        settings = settings or self.settings()
        engine = self.engine(settings)
        databases = [engine.inspect_target(target) for target in engine.targets()]
        api = BookOasisClient(settings.get("bookoasis_url"), settings.get("api_timeout", 30)).health()
        admin_api = self.admin_client(settings).login_admin()
        cover_root = str(settings.get("cover_root_path") or "").strip()
        cover_root_status = {"configured": bool(cover_root), "readable": bool(cover_root and Path(cover_root).is_dir())}
        log_dir = str(settings.get("bookoasis_log_dir") or "").strip()
        log_root_status = {"configured": bool(log_dir), "readable": False, "files": [], "error": ""}
        if log_dir:
            try:
                log_data = list_log_files(log_dir)
                log_root_status.update({"readable": True, "files": log_data.get("files", [])})
            except (ValueError, OSError) as error:
                log_root_status["error"] = str(error)
        data = {
            "success": all(database["connected"] for database in databases) and api["success"] and admin_api["success"],
            "databases": databases,
            "api": api,
            "admin_api": admin_api,
            "cover_root": cover_root_status,
            "log_root": log_root_status,
        }
        self._debug(
            "연결 검사 완료",
            success=str(data["success"]).lower(),
            databases=len(databases),
            connected=sum(1 for database in databases if database.get("connected")),
            api_success=str(api.get("success", False)).lower(),
            admin_api_success=str(admin_api.get("success", False)).lower(),
            cover_root_readable=str(cover_root_status["readable"]).lower(),
            log_root_readable=str(log_root_status["readable"]).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def history(self):
        started = time.monotonic()
        history_model = getattr(self.P, "history_model", None)
        if history_model is None:
            self._debug("검사 이력 조회 완료", available="false", items=0, duration_ms=self._duration_ms(started))
            return []
        data = history_model.recent(self.settings()["history_limit"])
        self._debug("검사 이력 조회 완료", available="true", items=len(data), duration_ms=self._duration_ms(started))
        return data

    def clear_history(self):
        started = time.monotonic()
        history_model = getattr(self.P, "history_model", None)
        if history_model is None:
            self._debug("검사 이력 삭제 완료", available="false", deleted=0, duration_ms=self._duration_ms(started))
            return 0
        count = history_model.delete_all()
        self._debug("검사 이력 삭제 완료", available="true", deleted=count, duration_ms=self._duration_ms(started))
        return count
