# FlaskFarm 설정과 진단 엔진, 검사 이력 저장을 연결합니다.
import copy
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .bookoasis_client import BookOasisClient
from .bookoasis_db import BookOasisDatabaseAdapter
from .bookoasis_logs import list_log_files, read_lazy_progress, read_log_tail
from .bookoasis_package_import import BookOasisPackageImportEngine
from .category_migration import (
    CategoryMigrationEngine,
    parse_library_ids,
    parse_paths,
)
from .kavita_migration import KavitaMigrationEngine, parse_name_list
from .library_statistics import (
    LibraryStatisticsCancelled,
    LibraryStatisticsEngine,
)
from .mate_engine import BookOasisMateEngine, _as_bool, _as_int
from .font_manager import CustomFontManager


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
        "audiobook_db_path": child("db/media_audiobook.db"),
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
        self._report_build_lock = threading.Lock()
        self._report_thread = None
        self._report_started_monotonic = None
        self._report_status = self._empty_report_status()
        self._cached_report = None
        self._cached_at = 0.0
        self._settings_fingerprint = None
        self._cover_issue_cache_key = None
        self._cover_issue_cache = None
        self._gap_cache = {}
        self._gap_analysis_lock = threading.Lock()
        self._admin_client = None
        self._admin_client_fingerprint = None
        self._orphan_cleanup_process = None
        self._orphan_cleanup_status_path = None
        self._orphan_cleanup_stop_path = None
        self._orphan_cleanup_status = self._empty_orphan_cleanup_status()
        self._batch_rescan_process = None
        self._batch_rescan_status_path = None
        self._batch_rescan_stop_path = None
        self._batch_rescan_status = self._empty_batch_rescan_status()
        self._batch_rescan_invalidated_finished_at = ""
        self._cover_inspection_process = None
        self._cover_inspection_status_path = None
        self._cover_inspection_stop_path = None
        self._cover_inspection_status = self._empty_cover_inspection_status()
        self._library_statistics_thread = None
        self._library_statistics_stop = threading.Event()
        self._library_statistics_started_monotonic = None
        self._library_statistics_status = self._empty_library_statistics_status()
        self._migration_stop = threading.Event()
        self._migration_process = None
        self._migration_status_path = None
        self._migration_stop_path = None
        self._migration_started_monotonic = None
        self._migration_status = self._empty_migration_status()
        self._database_migration_stop = threading.Event()
        self._database_migration_process = None
        self._database_migration_status_path = None
        self._database_migration_stop_path = None
        self._database_migration_started_monotonic = None
        self._database_migration_status = self._empty_migration_status()

    @staticmethod
    def _empty_report_status():
        return {
            "is_working": "wait",
            "job_type": "summary_report",
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "message": "상태 요약을 준비하고 있습니다.",
            "result": None,
            "error": "",
            "cached": False,
        }

    @staticmethod
    def _empty_orphan_cleanup_status():
        return {
            "is_working": "wait",
            "job_type": "orphan_cleanup",
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
            "job_type": "category_migration",
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

    @staticmethod
    def _empty_batch_rescan_status():
        return {
            "is_working": "wait",
            "job_type": "batch_book_rescan",
            "source": "",
            "source_label": "",
            "db_type": "general",
            "current": 0,
            "total": 0,
            "success_count": 0,
            "failed_count": 0,
            "progress_percent": 0,
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "message": "대기중",
            "items": [],
            "truncated": False,
            "stopped": False,
            "error": "",
        }

    @staticmethod
    def _empty_cover_inspection_status():
        return {
            "is_working": "wait",
            "job_type": "cover_inspection",
            "db_type": "general",
            "library_id": "",
            "search": "",
            "mode": "resolution",
            "fingerprint": "",
            "current": 0,
            "total": 0,
            "progress_percent": 0,
            "issue_counts": {
                "low_resolution": 0,
                "small_file": 0,
                "abnormal_aspect_ratio": 0,
            },
            "result_ready": False,
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "message": "대기중",
            "logs": [],
            "stopped": False,
            "error": "",
        }

    @staticmethod
    def _empty_library_statistics_status():
        return {
            "is_working": "wait",
            "job_type": "library_statistics",
            "db_type": "general",
            "library_id": "",
            "library_name": "전체 보관함",
            "stage": "",
            "current": 0,
            "total": 100,
            "progress_percent": 0,
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "message": "분석 조건을 선택하고 분석 버튼을 눌러 주세요.",
            "result": None,
            "error": "",
            "stopped": False,
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
            "db_engine": model.get("db_engine") or "sqlite",
            "bookoasis_root_path": model.get("bookoasis_root_path"),
            "general_db_path": model.get("general_db_path"),
            "adult_enabled": model.get_bool("adult_enabled"),
            "adult_db_path": model.get("adult_db_path"),
            "audiobook_db_path": model.get("audiobook_db_path"),
            "mariadb_host": model.get("mariadb_host"),
            "mariadb_port": _as_int(model.get("mariadb_port"), 3306, 1, 65535),
            "mariadb_user": model.get("mariadb_user"),
            "mariadb_password": model.get("mariadb_password"),
            "mariadb_database_prefix": model.get("mariadb_database_prefix") or "media_",
            "mariadb_connect_timeout": _as_int(
                model.get("mariadb_connect_timeout"), 10, 1, 60
            ),
            "mariadb_read_timeout": _as_int(
                model.get("mariadb_read_timeout"), 30, 1, 600
            ),
            "mariadb_write_timeout": _as_int(
                model.get("mariadb_write_timeout"), 30, 1, 600
            ),
            "bookoasis_url": model.get("bookoasis_url"),
            "bookoasis_username": model.get("bookoasis_username"),
            "bookoasis_password": model.get("bookoasis_password"),
            "webhook_token": model.get("webhook_token"),
            "bookoasis_log_dir": model.get("bookoasis_log_dir"),
            "cover_root_path": model.get("cover_root_path"),
            "cover_root_custom": model.get_bool("cover_root_custom"),
            "custom_font_dir": model.get("custom_font_dir"),
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
            derived = derive_bookoasis_paths(root)
            if values["cover_root_custom"]:
                derived.pop("cover_root_path", None)
            values.update(derived)
        return values

    @staticmethod
    def settings_from_mapping(values):
        settings = {
            "db_engine": str(values.get("db_engine") or "sqlite").strip().lower(),
            "bookoasis_root_path": str(values.get("bookoasis_root_path") or "").strip(),
            "general_db_path": values.get("general_db_path"),
            "adult_enabled": _as_bool(values.get("adult_enabled"), False),
            "adult_db_path": values.get("adult_db_path"),
            "audiobook_db_path": values.get("audiobook_db_path"),
            "mariadb_host": values.get("mariadb_host"),
            "mariadb_port": _as_int(values.get("mariadb_port"), 3306, 1, 65535),
            "mariadb_user": values.get("mariadb_user"),
            "mariadb_password": values.get("mariadb_password"),
            "mariadb_database_prefix": values.get("mariadb_database_prefix") or "media_",
            "mariadb_connect_timeout": _as_int(
                values.get("mariadb_connect_timeout"), 10, 1, 60
            ),
            "mariadb_read_timeout": _as_int(
                values.get("mariadb_read_timeout"), 30, 1, 600
            ),
            "mariadb_write_timeout": _as_int(
                values.get("mariadb_write_timeout"), 30, 1, 600
            ),
            "bookoasis_url": values.get("bookoasis_url"),
            "bookoasis_username": values.get("bookoasis_username"),
            "bookoasis_password": values.get("bookoasis_password"),
            "webhook_token": values.get("webhook_token"),
            "bookoasis_log_dir": values.get("bookoasis_log_dir"),
            "cover_root_path": values.get("cover_root_path"),
            "cover_root_custom": _as_bool(
                values.get("cover_root_custom"),
                False,
            ),
            "custom_font_dir": values.get("custom_font_dir"),
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
            derived = derive_bookoasis_paths(root)
            if settings["cover_root_custom"]:
                derived.pop("cover_root_path", None)
            settings.update(derived)
        return settings

    def engine(self, settings=None):
        return BookOasisMateEngine(settings or self.settings())

    def database_engine_info(self, settings=None):
        return self.engine(settings).database_adapter.public_info()

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

    @staticmethod
    def _admin_credentials_configured(settings):
        return bool(
            str(settings.get("bookoasis_username") or "").strip()
            and str(settings.get("bookoasis_password") or "")
        )

    @staticmethod
    def _unsupported_admin_api(response):
        return (
            isinstance(response, dict)
            and response.get("http_status") in {404, 405}
        )

    def invalidate(self):
        with self._lock:
            self._cached_report = None
            self._cached_at = 0.0
            self._settings_fingerprint = None
            self._cover_issue_cache_key = None
            self._cover_issue_cache = None
            self._gap_cache = {}
            if self._report_status.get("is_working") != "run":
                self._report_status = self._empty_report_status()
            if self._library_statistics_status.get("is_working") != "run":
                self._library_statistics_status = self._empty_library_statistics_status()
        self._debug("상태 요약 캐시 초기화")

    def library_statistics_catalog(self, db_type="general"):
        started = time.monotonic()
        data = LibraryStatisticsEngine(self.settings()).catalog(db_type)
        self._debug(
            "라이브러리 통계 보관함 조회 완료",
            db_type=db_type,
            libraries=len(data.get("libraries", [])),
            duration_ms=self._duration_ms(started),
        )
        return data

    def library_statistics_status(self):
        with self._lock:
            status = copy.deepcopy(self._library_statistics_status)
            if (
                status.get("is_working") == "run"
                and self._library_statistics_started_monotonic is not None
            ):
                status["elapsed_seconds"] = round(
                    time.monotonic() - self._library_statistics_started_monotonic,
                    1,
                )
            return status

    def _library_statistics_progress(self, stage, current, total, message):
        with self._lock:
            status = self._library_statistics_status
            if status.get("is_working") != "run":
                return
            status["stage"] = str(stage or "")
            status["current"] = int(current or 0)
            status["total"] = max(1, int(total or 100))
            status["progress_percent"] = max(
                0,
                min(100, round(status["current"] / status["total"] * 100)),
            )
            status["message"] = str(message or "")

    def _run_library_statistics(self, settings, db_type, library_id):
        try:
            result = LibraryStatisticsEngine(settings).analyze(
                db_type=db_type,
                library_id=library_id,
                on_progress=self._library_statistics_progress,
                should_stop=self._library_statistics_stop.is_set,
            )
            with self._lock:
                status = self._library_statistics_status
                status.update(
                    {
                        "is_working": "done",
                        "stage": "complete",
                        "current": 100,
                        "total": 100,
                        "progress_percent": 100,
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(
                            time.monotonic() - self._library_statistics_started_monotonic,
                            1,
                        ),
                        "message": "라이브러리 통계 분석을 완료했습니다.",
                        "result": result,
                        "error": "",
                    }
                )
            self._debug(
                "라이브러리 통계 분석 완료",
                db_type=db_type,
                library_id=library_id or "all",
                total_items=result.get("summary", {}).get("total_items", 0),
                duration_ms=result.get("duration_ms", 0),
            )
        except LibraryStatisticsCancelled as error:
            with self._lock:
                self._library_statistics_status.update(
                    {
                        "is_working": "stopped",
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(
                            time.monotonic() - self._library_statistics_started_monotonic,
                            1,
                        ),
                        "message": str(error),
                        "stopped": True,
                    }
                )
            self._debug(
                "라이브러리 통계 분석 중지",
                db_type=db_type,
                library_id=library_id or "all",
            )
        except Exception as error:
            self.P.logger.error(f"BookOasis Mate 라이브러리 통계 오류: {error}")
            with self._lock:
                self._library_statistics_status.update(
                    {
                        "is_working": "fail",
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(
                            time.monotonic() - self._library_statistics_started_monotonic,
                            1,
                        ),
                        "message": "라이브러리 통계 분석에 실패했습니다.",
                        "error": str(error),
                    }
                )
        finally:
            with self._lock:
                self._library_statistics_thread = None

    def start_library_statistics(self, db_type="general", library_id=None):
        db_type = str(db_type or "general").strip().lower()
        selected_library_id = str(library_id or "").strip()
        with self._lock:
            if self._library_statistics_status.get("is_working") == "run":
                return {
                    "started": False,
                    "message": "라이브러리 통계 분석이 이미 실행 중입니다.",
                    "status": copy.deepcopy(self._library_statistics_status),
                }
        catalog = self.library_statistics_catalog(db_type)
        library_name = "전체 보관함"
        if selected_library_id:
            library = next(
                (
                    item
                    for item in catalog.get("libraries", [])
                    if str(item.get("id")) == selected_library_id
                ),
                None,
            )
            if library is None:
                raise ValueError("선택한 보관함을 찾을 수 없습니다.")
            library_name = str(library.get("name") or f"보관함 {selected_library_id}")
        initial = self._empty_library_statistics_status()
        initial.update(
            {
                "is_working": "run",
                "db_type": db_type,
                "library_id": selected_library_id,
                "library_name": library_name,
                "stage": "prepare",
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "message": "라이브러리 통계 분석을 준비하고 있습니다.",
            }
        )
        self._library_statistics_stop.clear()
        with self._lock:
            self._library_statistics_status = initial
            self._library_statistics_started_monotonic = time.monotonic()
            thread = threading.Thread(
                target=self._run_library_statistics,
                args=(self.settings(), db_type, selected_library_id or None),
                name="bookoasis-mate-library-statistics",
                daemon=True,
            )
            self._library_statistics_thread = thread
            thread.start()
        self._debug(
            "라이브러리 통계 백그라운드 분석 시작",
            db_type=db_type,
            library_id=selected_library_id or "all",
        )
        return {
            "started": True,
            "message": "라이브러리 통계 분석을 시작했습니다.",
            "status": copy.deepcopy(initial),
        }

    def stop_library_statistics(self):
        current = self.library_statistics_status()
        if current.get("is_working") != "run":
            return {
                "requested": False,
                "message": "실행 중인 라이브러리 통계 분석이 없습니다.",
                "status": current,
            }
        self._library_statistics_stop.set()
        with self._lock:
            self._library_statistics_status["message"] = "현재 데이터 묶음 처리 후 중지합니다."
        self._debug("라이브러리 통계 분석 중지 요청")
        return {
            "requested": True,
            "message": "라이브러리 통계 분석 중지를 요청했습니다.",
            "status": self.library_statistics_status(),
        }

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

        with self._report_build_lock:
            with self._lock:
                fresh = self._cached_report is not None and time.monotonic() - self._cached_at <= ttl
                if not force and fresh and fingerprint == self._settings_fingerprint:
                    self._debug("상태 요약 캐시 사용", age_seconds=round(time.monotonic() - self._cached_at, 1))
                    return self._cached_report
            self._debug("상태 요약 생성 시작", force=str(bool(force)).lower())
            report = self.engine(settings).build_report()
            with self._lock:
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

    def report_refresh_status(self):
        with self._lock:
            status = copy.deepcopy(self._report_status)
            if (
                status.get("is_working") == "run"
                and self._report_started_monotonic is not None
            ):
                status["elapsed_seconds"] = round(
                    time.monotonic() - self._report_started_monotonic,
                    1,
                )
            return status

    def _run_report_refresh(self, force):
        try:
            report = self.report(force=force)
            with self._lock:
                self._report_status.update(
                    {
                        "is_working": "done",
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(
                            time.monotonic() - self._report_started_monotonic,
                            1,
                        ),
                        "message": "상태 요약 갱신을 완료했습니다.",
                        "result": report,
                        "error": "",
                        "cached": False,
                    }
                )
        except Exception as error:
            self.P.logger.error(f"BookOasis Mate 상태 요약 갱신 오류: {error}")
            with self._lock:
                self._report_status.update(
                    {
                        "is_working": "fail",
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(
                            time.monotonic() - self._report_started_monotonic,
                            1,
                        ),
                        "message": "상태 요약 갱신에 실패했습니다.",
                        "error": str(error),
                    }
                )
        finally:
            with self._lock:
                self._report_thread = None

    def start_report_refresh(self, force=False):
        settings = self.settings()
        fingerprint = tuple(sorted((key, str(value)) for key, value in settings.items()))
        ttl = settings["cache_seconds"]
        with self._lock:
            if self._report_status.get("is_working") == "run":
                return {
                    "started": False,
                    "status": self.report_refresh_status(),
                }
            cached_report = copy.deepcopy(self._cached_report)
            fresh = (
                cached_report is not None
                and time.monotonic() - self._cached_at <= ttl
                and fingerprint == self._settings_fingerprint
            )
            if not force and fresh:
                status = self._empty_report_status()
                status.update(
                    {
                        "is_working": "done",
                        "message": "캐시된 상태 요약을 표시합니다.",
                        "result": cached_report,
                        "cached": True,
                    }
                )
                self._report_status = status
                return {"started": False, "status": copy.deepcopy(status)}

            status = self._empty_report_status()
            status.update(
                {
                    "is_working": "run",
                    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "message": "BookOasis DB 상태를 백그라운드에서 집계하고 있습니다.",
                    "result": cached_report,
                    "cached": cached_report is not None,
                }
            )
            self._report_status = status
            self._report_started_monotonic = time.monotonic()
            thread = threading.Thread(
                target=self._run_report_refresh,
                args=(bool(force),),
                name="bookoasis-mate-summary-report",
                daemon=True,
            )
            self._report_thread = thread
            thread.start()
            return {"started": True, "status": copy.deepcopy(status)}

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
        client = self.admin_client(settings)
        libraries_response = client.library_schedules(db_type)
        data["library_source"] = "database"
        data["library_api_error"] = ""
        if (
            isinstance(libraries_response, dict)
            and libraries_response.get("success")
            and isinstance(libraries_response.get("libraries"), list)
        ):
            database_libraries = {
                str(item.get("id")): item
                for item in data.get("libraries", [])
                if isinstance(item, dict) and item.get("id") is not None
            }
            api_libraries = []
            for raw_item in libraries_response["libraries"]:
                if not isinstance(raw_item, dict):
                    continue
                item = dict(raw_item)
                database_item = database_libraries.get(str(item.get("id")), {})
                rc_url = str(item.pop("rclone_rc_url", "") or "").strip()
                item.pop("physical_path", None)
                item["rclone_rc_configured"] = bool(
                    rc_url or database_item.get("rclone_rc_configured")
                )
                item["checkpoint_folders"] = int(
                    database_item.get("checkpoint_folders") or 0
                )
                if item.get("vfs_refresh_before_scan") is None:
                    item["vfs_refresh_before_scan"] = database_item.get(
                        "vfs_refresh_before_scan"
                    )
                api_libraries.append(item)
            data["libraries"] = api_libraries
            data["library_source"] = "api"
        else:
            data["library_api_error"] = str(
                libraries_response.get("message")
                or libraries_response.get("error")
                or "보관함 상태 API를 사용할 수 없습니다."
            ) if isinstance(libraries_response, dict) else "보관함 상태 API 응답이 올바르지 않습니다."

        response = client.queue_status()
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
        kwargs = task.get("kwargs") if isinstance(task.get("kwargs"), dict) else {}
        return {
            "type": str(task.get("type") or ""),
            "key": str(task.get("key") or ""),
            "enqueued_at": task.get("enqueued_at"),
            "started_at": task.get("started_at"),
            "stage": str(task.get("stage") or ""),
            "library_name": str(task.get("library_name") or ""),
            "library_id": kwargs.get("library_id"),
            "db_type": str(kwargs.get("db_type") or ""),
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
        client = self.admin_client(settings)
        libraries = []
        if self._admin_credentials_configured(settings):
            schedules = client.library_schedules(db_type)
            if (
                isinstance(schedules, dict)
                and schedules.get("success")
                and isinstance(schedules.get("libraries"), list)
            ):
                libraries = schedules["libraries"]
        if not libraries:
            libraries = self.engine(settings).scanner_status(
                db_type=db_type,
                limit=10,
            )["libraries"]
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
        force = _as_bool(force)
        api_response = None
        if self._admin_credentials_configured(settings):
            api_response = (
                client.scan_all_libraries(db_type, force)
                if all_libraries
                else client.scan_library(selected[0]["id"], db_type, force)
            )
            if not self._unsupported_admin_api(api_response):
                success = bool(
                    isinstance(api_response, dict)
                    and api_response.get("success")
                )
                data = {
                    "success": success,
                    "requested": len(selected),
                    "queued": len(selected) if success else 0,
                    "already_queued": 0,
                    "source": "admin_api",
                    "message": (
                        api_response.get("message")
                        or api_response.get("error")
                        or "재스캔 요청을 처리했습니다."
                    ) if isinstance(api_response, dict) else "BookOasis 관리자 API 응답이 올바르지 않습니다.",
                    "results": [api_response] if isinstance(api_response, dict) else [],
                }
                self._debug(
                    "재스캔 요청 완료",
                    db_type=db_type,
                    scope="all" if all_libraries else "single",
                    source=data["source"],
                    requested=data["requested"],
                    queued=data["queued"],
                    already_queued=0,
                    success=str(data["success"]).lower(),
                    duration_ms=self._duration_ms(started),
                )
                return data

        results = [
            client.request_scan(
                settings.get("webhook_token"),
                item["id"],
                db_type,
                force,
            )
            for item in selected
        ]
        data = {
            "success": all(result["success"] for result in results),
            "requested": len(results),
            "queued": sum(1 for result in results if result["success"] and not result.get("already_queued")),
            "already_queued": sum(1 for result in results if result.get("already_queued")),
            "source": "webhook_fallback",
            "message": next(
                (
                    result.get("message")
                    for result in results
                    if result.get("message")
                ),
                "재스캔 요청을 처리했습니다.",
            ),
            "results": results,
        }
        self._debug(
            "재스캔 요청 완료",
            db_type=db_type,
            scope="all" if all_libraries else "single",
            source=data["source"],
            requested=data["requested"],
            queued=data["queued"],
            already_queued=data["already_queued"],
            success=str(data["success"]).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def cancel_library_scan(self, library_id, db_type="general"):
        started = time.monotonic()
        data = self.admin_client().cancel_library_scan(library_id, db_type)
        self._debug(
            "보관함 스캔 취소 요청 완료",
            db_type=db_type,
            library_id=library_id,
            success=str(bool(data.get("success"))).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def scan_library_covers(self, library_id, db_type="general"):
        started = time.monotonic()
        data = self.admin_client().scan_library_covers(library_id, db_type)
        self._debug(
            "보관함 표지 스캔 요청 완료",
            db_type=db_type,
            library_id=library_id,
            success=str(bool(data.get("success"))).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def clear_scan_queue(self):
        started = time.monotonic()
        data = self.admin_client().clear_queue()
        self._debug(
            "스캔 대기열 정리 완료",
            success=str(bool(data.get("success"))).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def cancel_scan_queue_task(self, task_key):
        started = time.monotonic()
        data = self.admin_client().cancel_queue_task(task_key)
        self._debug(
            "스캔 대기 작업 취소 완료",
            task_key=str(task_key or "")[:80],
            success=str(bool(data.get("success"))).lower(),
            duration_ms=self._duration_ms(started),
        )
        return data

    def scan_book(self, book_id, db_type="general"):
        batch_status = self.batch_rescan_status()
        if batch_status.get("is_working") == "run":
            return {
                "success": False,
                "message": "일괄 재스캔이 실행 중입니다. 작업이 끝난 후 개별 재스캔을 실행해 주세요.",
            }
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
        client = self.admin_client()
        searchable = client.metadata_plugins()
        managed = client.metadata_plugins_manage()
        data = searchable
        if searchable.get("success"):
            active = {
                str(item.get("id") or ""): item
                for item in searchable.get("plugins", [])
                if isinstance(item, dict) and item.get("id")
            }
            plugins = []
            if managed.get("success"):
                for item in managed.get("plugins", []):
                    if not isinstance(item, dict) or not item.get("id") or not item.get("is_searchable", True):
                        continue
                    plugin_id = str(item["id"])
                    schema = item.get("config_schema") if isinstance(item.get("config_schema"), list) else []
                    config = item.get("config") if isinstance(item.get("config"), dict) else {}
                    required_keys = [
                        str(field.get("key") or "")
                        for field in schema
                        if isinstance(field, dict) and field.get("required") and field.get("key")
                    ]
                    plugins.append({
                        "id": plugin_id,
                        "name": str(item.get("name") or plugin_id),
                        "enabled": bool(item.get("enabled")),
                        "configured": all(bool(config.get(key)) for key in required_keys),
                        "active": plugin_id in active,
                        "update_supported": bool(item.get("update_manifest")),
                    })
            else:
                plugins = [
                    {
                        "id": plugin_id,
                        "name": str(item.get("name") or plugin_id),
                        "enabled": True,
                        "configured": True,
                        "active": True,
                        "update_supported": False,
                    }
                    for plugin_id, item in active.items()
                ]
            data = {
                "success": True,
                "plugins": plugins,
                "diagnostics_supported": bool(managed.get("success")),
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
        database_info = BookOasisDatabaseAdapter(settings).public_info()
        cache_key = (
            normalized_db_type,
            normalized_library_id,
            normalized_search,
            str(database_info.get("resolved_engine") or ""),
            str(database_info.get("host") or ""),
            str(database_info.get("database_prefix") or ""),
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

    @staticmethod
    def _cover_query_context(settings, db_type, library_id, mode, search):
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
        adapter = BookOasisDatabaseAdapter(settings)
        database_info = adapter.public_info()
        cache_key = (
            db_type,
            str(library_id or ""),
            mode,
            search,
            str(database_info.get("resolved_engine") or ""),
            str(database_info.get("host") or ""),
            str(database_info.get("database_prefix") or ""),
            str(settings.get("general_db_path") or ""),
            str(settings.get("adult_db_path") or ""),
            str(settings.get("cover_root_path") or ""),
            settings["cover_min_width"],
            settings["cover_min_height"],
            settings["cover_min_file_size_kb"],
            settings["cover_min_aspect_percent"],
        )
        return mode, target_issue, file_mode, search, cache_key

    def covers(self, db_type="general", library_id=None, mode="missing", search="", page=1, page_size=None, force=False):
        started = time.monotonic()
        settings = self.settings()
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size or min(settings["page_size"], 25), 25, 1, 200)
        mode, target_issue, file_mode, search, cache_key = self._cover_query_context(
            settings,
            db_type,
            library_id,
            mode,
            search,
        )
        if file_mode:
            restored = self.cover_inspection_status(
                mode=mode,
                page=page,
                page_size=page_size,
            )
            if restored.get("data") is None:
                raise ValueError(
                    "현재 조건의 표지 정밀 검사 결과가 없습니다. 검사 버튼을 눌러 백그라운드 검사를 시작해 주세요."
                )
            return restored["data"]
        force = _as_bool(force, False)
        with self._lock:
            cached = self._cover_issue_cache if self._cover_issue_cache_key == cache_key else None

        cache_hit = not force and cached is not None
        if not cache_hit:
            engine = self.engine(settings)
            issues = []
            source_total = 0
            inspected_count = 0
            source_page = 1
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

    def _cover_inspection_paths(self, settings=None):
        settings = settings or self.settings()
        state_root = self._maintenance_state_root(settings)
        if state_root is None:
            return None
        paths = self._maintenance_worker_paths(
            state_root,
            "cover_inspection",
        )
        paths["result"] = paths["status"].with_name(
            ".bookoasis_mate_cover_inspection_result.json"
        )
        return paths

    def _cover_inspection_criteria(self, settings, db_type, library_id, search):
        target = self.engine(settings).get_target(db_type)
        selected_library_id = ""
        if str(library_id or "").strip():
            try:
                numeric_library_id = int(str(library_id).strip())
            except (TypeError, ValueError) as error:
                raise ValueError("보관함 ID가 올바르지 않습니다.") from error
            if numeric_library_id < 1:
                raise ValueError("보관함 ID가 올바르지 않습니다.")
            selected_library_id = str(numeric_library_id)
        db_path = Path(str(target.path or "")).expanduser() if target.path else None

        def file_signature(path):
            try:
                stat = path.stat()
                return [int(stat.st_size), int(stat.st_mtime_ns)]
            except OSError:
                return [0, 0]

        criteria = {
            "db_type": target.db_type,
            "library_id": selected_library_id,
            "search": str(search or "").strip(),
            "db_engine": target.engine,
            "database": target.database,
            "db_path": str(db_path.resolve()) if db_path is not None else "",
            "db_signature": file_signature(db_path) if db_path is not None else [0, 0],
            "wal_signature": file_signature(Path(f"{db_path}-wal")) if db_path is not None else [0, 0],
            "cover_root_path": str(settings.get("cover_root_path") or ""),
            "cover_min_width": int(settings["cover_min_width"]),
            "cover_min_height": int(settings["cover_min_height"]),
            "cover_min_file_size_kb": int(settings["cover_min_file_size_kb"]),
            "cover_min_aspect_percent": int(settings["cover_min_aspect_percent"]),
        }
        encoded = json.dumps(
            criteria,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return criteria, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _cover_result_page(result, mode, page, page_size):
        mode = {"file": "resolution"}.get(mode, mode)
        mode = mode if mode in {"resolution", "file_size", "aspect"} else "resolution"
        target_issue = {
            "resolution": "low_resolution",
            "file_size": "small_file",
            "aspect": "abnormal_aspect_ratio",
        }[mode]
        matches = []
        for source in result.get("items") or []:
            inspection = source.get("inspection") or {}
            if target_issue not in (inspection.get("issues") or []):
                continue
            item = copy.deepcopy(source)
            item["inspection"]["status"] = target_issue
            item["inspection"]["issues"] = [target_issue]
            matches.append(item)
        total = len(matches)
        pages = (total + page_size - 1) // page_size
        page = min(page, max(1, pages))
        offset = (page - 1) * page_size
        return {
            "items": matches[offset:offset + page_size],
            "total": total,
            "source_total": int(result.get("source_total") or 0),
            "inspected_count": int(result.get("inspected_count") or 0),
            "issue_count": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "mode": mode,
            "duration_ms": int(result.get("duration_ms") or 0),
            "cache_hit": True,
            "complete": bool(result.get("complete", True)),
        }

    def cover_inspection_status(self, mode="resolution", page=1, page_size=None):
        settings = self.settings()
        page = _as_int(page, 1, 1, 100000)
        page_size = _as_int(page_size or min(settings["page_size"], 25), 25, 1, 200)
        with self._lock:
            paths = self._cover_inspection_paths(settings)
            if paths is not None:
                external = self._external_job_status(
                    self._cover_inspection_status_path or paths["status"],
                    "cover_inspection",
                    self._cover_inspection_process,
                    "표지 정밀 검사 프로세스가 종료되었습니다.",
                )
                if external:
                    self._cover_inspection_status = copy.deepcopy(external)
            status = copy.deepcopy(self._cover_inspection_status)

        data = None
        if paths is not None and status.get("result_ready"):
            result = self._read_database_migration_json(paths["result"])
            if (
                isinstance(result, dict)
                and result.get("fingerprint")
                and result.get("fingerprint") == status.get("fingerprint")
            ):
                data = self._cover_result_page(result, mode, page, page_size)
        return {"status": status, "data": data}

    def start_cover_inspection(
        self,
        db_type="general",
        library_id=None,
        mode="resolution",
        search="",
    ):
        mode = {"file": "resolution"}.get(mode, mode)
        if mode not in {"resolution", "file_size", "aspect"}:
            raise ValueError("파일 정밀 검사 유형이 올바르지 않습니다.")
        settings = self.settings()
        root = str(settings.get("cover_root_path") or "").strip()
        if not root or not Path(root).is_dir():
            raise FileNotFoundError("설정된 표지 디렉터리를 찾을 수 없습니다.")
        criteria, fingerprint = self._cover_inspection_criteria(
            settings,
            db_type,
            library_id,
            search,
        )
        current = self.cover_inspection_status(mode=mode)["status"]
        if current.get("is_working") == "run":
            return {
                "started": False,
                "message": "표지 정밀 검사가 이미 실행 중입니다.",
                "status": current,
            }

        paths = self._cover_inspection_paths(settings)
        if paths is None:
            raise ValueError("표지 정밀 검사 상태를 저장할 DB 경로가 설정되지 않았습니다.")
        try:
            paths["result"].unlink()
        except FileNotFoundError:
            pass
        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        initial_status = self._empty_cover_inspection_status()
        initial_status.update(
            {
                "is_working": "run",
                "db_type": criteria["db_type"],
                "library_id": criteria["library_id"],
                "search": criteria["search"],
                "mode": mode,
                "fingerprint": fingerprint,
                "started_at": started_at,
                "message": "표지 정밀 검사를 준비하고 있습니다.",
            }
        )
        safe_setting_keys = (
            "db_engine",
            "general_db_path",
            "adult_db_path",
            "audiobook_db_path",
            "adult_enabled",
            "mariadb_host",
            "mariadb_port",
            "mariadb_user",
            "mariadb_password",
            "mariadb_database_prefix",
            "mariadb_connect_timeout",
            "mariadb_read_timeout",
            "mariadb_write_timeout",
            "bookoasis_url",
            "cover_root_path",
            "cover_min_width",
            "cover_min_height",
            "cover_min_file_size_kb",
            "cover_min_aspect_percent",
        )
        worker_config = {
            "job_type": "cover_inspection",
            "delete_config_after_read": True,
            "settings": {key: settings.get(key) for key in safe_setting_keys},
            "db_type": criteria["db_type"],
            "library_id": criteria["library_id"],
            "search": criteria["search"],
            "fingerprint": fingerprint,
            "result_path": str(paths["result"]),
        }
        process = self._launch_maintenance_worker(
            paths,
            worker_config,
            initial_status,
            sensitive_config=True,
        )
        with self._lock:
            self._cover_inspection_process = process
            self._cover_inspection_status_path = paths["status"]
            self._cover_inspection_stop_path = paths["stop"]
            self._cover_inspection_status = copy.deepcopy(initial_status)
        self._debug(
            "표지 정밀 검사 별도 프로세스 시작",
            pid=process.pid,
            db_type=criteria["db_type"],
            library_id=criteria["library_id"] or "all",
            search=str(bool(criteria["search"])).lower(),
        )
        return {
            "started": True,
            "message": "표지 정밀 검사를 시작했습니다.",
            "status": copy.deepcopy(initial_status),
        }

    def stop_cover_inspection(self):
        current = self.cover_inspection_status()["status"]
        if current.get("is_working") != "run":
            return {
                "requested": False,
                "message": "실행 중인 표지 정밀 검사가 없습니다.",
                "status": current,
            }
        stop_path = self._cover_inspection_stop_path
        if stop_path is None:
            paths = self._cover_inspection_paths()
            stop_path = paths["stop"] if paths is not None else None
        if stop_path is not None:
            stop_path.parent.mkdir(parents=True, exist_ok=True)
            stop_path.touch()
        with self._lock:
            self._cover_inspection_status["message"] = "현재 표지 묶음 처리 후 중지합니다."
        self._debug("표지 정밀 검사 중지 요청")
        return {
            "requested": True,
            "message": "표지 정밀 검사 중지를 요청했습니다.",
            "status": copy.deepcopy(self._cover_inspection_status),
        }

    def cover_issue_book_ids(self, db_type="general", library_id=None, mode="missing", search=""):
        settings = self.settings()
        mode, unused_issue, unused_file_mode, search, cache_key = self._cover_query_context(
            settings,
            db_type,
            library_id,
            mode,
            search,
        )
        del unused_issue, unused_file_mode
        if mode != "missing":
            paths = self._cover_inspection_paths(settings)
            result = (
                self._read_database_migration_json(paths["result"])
                if paths is not None
                else None
            )
            unused_criteria, fingerprint = self._cover_inspection_criteria(
                settings,
                db_type,
                library_id,
                search,
            )
            del unused_criteria
            if not isinstance(result, dict) or result.get("fingerprint") != fingerprint:
                raise ValueError("현재 조건의 표지 정밀 검사 결과가 없습니다. 먼저 검사 버튼을 눌러 주세요.")
            data = self._cover_result_page(result, mode, 1, max(1, len(result.get("items") or [])))
            return sorted({
                int(item.get("id"))
                for item in data.get("items", [])
                if str(item.get("id") or "").isdigit() and int(item.get("id")) > 0
            })
        with self._lock:
            cached = self._cover_issue_cache if self._cover_issue_cache_key == cache_key else None
            if cached is None:
                raise ValueError("현재 조건의 표지 검사 결과가 없습니다. 먼저 검사 버튼을 눌러 주세요.")
            return sorted({
                int(item.get("id"))
                for item in cached.get("items", [])
                if str(item.get("id") or "").isdigit() and int(item.get("id")) > 0
            })

    @staticmethod
    def _maintenance_worker_paths(root_path, job_name):
        root = Path(str(root_path or "")).expanduser().resolve()
        prefix = f".bookoasis_mate_{job_name}"
        return {
            "config": root / f"{prefix}_job.json",
            "status": root / f"{prefix}_status.json",
            "stop": root / f"{prefix}_stop",
            "log": root / f"{prefix}_worker.log",
        }

    def _maintenance_state_root(self, settings=None):
        settings = settings or self.settings()
        if self.engine(settings).database_adapter.engine == "sqlite":
            db_path = str(
                settings.get("general_db_path")
                or settings.get("adult_db_path")
                or ""
            ).strip()
            if db_path:
                return (
                    Path(db_path).expanduser().resolve().parent
                    / ".bookoasis_mate_jobs"
                )
        for candidate in (
            settings.get("cover_root_path"),
            settings.get("bookoasis_root_path"),
            Path(str(settings.get("general_db_path") or "")).parent
            if str(settings.get("general_db_path") or "").strip()
            else "",
        ):
            value = str(candidate or "").strip()
            if value:
                return Path(value).expanduser().resolve() / ".bookoasis_mate_jobs"
        return None

    @staticmethod
    def _worker_pid_alive(pid):
        if os.name == "nt":
            return None
        try:
            numeric_pid = int(pid)
        except (TypeError, ValueError):
            return None
        if numeric_pid <= 0:
            return None
        try:
            os.kill(numeric_pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None

    def _external_job_status(self, status_path, job_type, process, failure_message):
        status = self._read_database_migration_json(status_path)
        if not status or status.get("job_type") != job_type:
            return None
        started_epoch = float(status.get("started_epoch") or 0)
        if status.get("is_working") == "run" and started_epoch:
            status["elapsed_seconds"] = round(
                max(0, time.time() - started_epoch),
                1,
            )
        exited = (
            process is not None and process.poll() is not None
        )
        if process is None and status.get("is_working") == "run":
            exited = self._worker_pid_alive(status.get("worker_pid")) is False
        if status.get("is_working") == "run" and exited:
            return_code = process.returncode if process is not None else "알 수 없음"
            status.update(
                {
                    "is_working": "error",
                    "message": failure_message,
                    "error": (
                        "작업 프로세스가 완료 상태를 기록하지 못했습니다. "
                        f"종료 코드: {return_code}"
                    ),
                }
            )
            self._write_database_migration_json(status_path, status)
        return status

    def _launch_maintenance_worker(self, paths, config, initial_status, sensitive_config=False):
        paths["config"].parent.mkdir(parents=True, exist_ok=True)
        if paths["stop"].exists():
            paths["stop"].unlink()
        self._write_database_migration_json(paths["config"], config)
        if sensitive_config and os.name != "nt":
            os.chmod(paths["config"], 0o600)
        self._write_database_migration_json(paths["status"], initial_status)
        worker_script = Path(__file__).with_name("maintenance_worker.py")
        command = [
            sys.executable,
            str(worker_script),
            str(paths["config"]),
            str(paths["status"]),
            str(paths["stop"]),
        ]
        with paths["log"].open("ab") as output:
            return subprocess.Popen(
                command,
                cwd=str(worker_script.parent),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _batch_rescan_paths(self, settings=None):
        settings = settings or self.settings()
        state_root = self._maintenance_state_root(settings)
        if state_root is None:
            return None
        return self._maintenance_worker_paths(
            state_root,
            "batch_book_rescan",
        )

    def batch_rescan_status(self):
        with self._lock:
            paths = self._batch_rescan_paths()
            if paths is not None:
                external = self._external_job_status(
                    self._batch_rescan_status_path or paths["status"],
                    "batch_book_rescan",
                    self._batch_rescan_process,
                    "검색 결과 일괄 재스캔 프로세스가 종료되었습니다.",
                )
                if external:
                    self._batch_rescan_status = copy.deepcopy(external)
                    finished_at = str(external.get("finished_at") or "")
                    if (
                        finished_at
                        and finished_at != self._batch_rescan_invalidated_finished_at
                        and external.get("is_working") in {"wait", "stop"}
                    ):
                        self._batch_rescan_invalidated_finished_at = finished_at
                        self.invalidate()
            return copy.deepcopy(self._batch_rescan_status)

    def start_batch_rescan(
        self,
        source,
        db_type="general",
        library_id=None,
        issue_type="all",
        mode="missing",
        search="",
    ):
        source = str(source or "").strip()
        if source not in {"issues", "covers"}:
            raise ValueError("일괄 재스캔 대상 화면이 올바르지 않습니다.")
        db_type = str(db_type or "general").strip()
        settings = self.settings()
        target = self.engine(settings).get_target(db_type)
        if not self._admin_credentials_configured(settings):
            raise ValueError("BookOasis 관리자 계정과 비밀번호를 먼저 설정해 주세요.")

        current_status = self.batch_rescan_status()
        with self._lock:
            if current_status.get("is_working") == "run":
                return {
                    "started": False,
                    "message": "검색 결과 일괄 재스캔이 이미 실행 중입니다.",
                    "status": current_status,
                }

        if source == "issues":
            book_ids = self.engine(settings).issue_book_ids(
                db_type=db_type,
                library_id=library_id,
                issue_type=issue_type,
                search=search,
            )
            source_label = "문제 도서"
            filter_summary = {
                "library_id": str(library_id or ""),
                "issue_type": str(issue_type or "all"),
                "search": str(search or ""),
            }
        else:
            book_ids = self.cover_issue_book_ids(
                db_type=db_type,
                library_id=library_id,
                mode=mode,
                search=search,
            )
            source_label = "표지 검사"
            filter_summary = {
                "library_id": str(library_id or ""),
                "mode": str(mode or "missing"),
                "search": str(search or ""),
            }
        book_ids = sorted({int(book_id) for book_id in book_ids if int(book_id or 0) > 0})
        if not book_ids:
            return {
                "started": False,
                "message": "현재 검색 조건에 재스캔할 도서가 없습니다.",
                "status": current_status,
            }

        login = self.admin_client(settings).login_admin()
        if not login.get("success"):
            raise ValueError(login.get("message") or "BookOasis 관리자 연결에 실패했습니다.")

        started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        initial_status = self._empty_batch_rescan_status()
        initial_status.update(
            {
                "is_working": "run",
                "source": source,
                "source_label": source_label,
                "db_type": db_type,
                "current": 0,
                "total": len(book_ids),
                "started_at": started_at,
                "message": f"{source_label} 검색 결과를 재스캔할 준비를 하고 있습니다.",
                "filters": filter_summary,
            }
        )
        paths = self._batch_rescan_paths(settings)
        if paths is None:
            raise ValueError("일괄 재스캔 상태를 저장할 DB 경로가 설정되지 않았습니다.")
        worker_config = {
            "job_type": "batch_book_rescan",
            "delete_config_after_read": True,
            "db_type": db_type,
            "db_settings": {
                key: settings.get(key)
                for key in (
                    "db_engine",
                    "general_db_path",
                    "adult_db_path",
                    "audiobook_db_path",
                    "mariadb_host",
                    "mariadb_port",
                    "mariadb_user",
                    "mariadb_password",
                    "mariadb_database_prefix",
                    "mariadb_connect_timeout",
                    "mariadb_read_timeout",
                    "mariadb_write_timeout",
                )
            },
            "book_ids": book_ids,
            "source": source,
            "source_label": source_label,
            "bookoasis_url": settings.get("bookoasis_url"),
            "bookoasis_username": settings.get("bookoasis_username"),
            "bookoasis_password": settings.get("bookoasis_password"),
            "api_timeout": settings.get("api_timeout", 30),
        }
        try:
            process = self._launch_maintenance_worker(
                paths,
                worker_config,
                initial_status,
                sensitive_config=True,
            )
        except Exception as error:
            initial_status.update(
                {
                    "is_working": "error",
                    "message": "검색 결과 일괄 재스캔 프로세스를 시작하지 못했습니다.",
                    "error": str(error),
                }
            )
            with self._lock:
                self._batch_rescan_status = initial_status
            raise

        with self._lock:
            self._batch_rescan_process = process
            self._batch_rescan_status_path = paths["status"]
            self._batch_rescan_stop_path = paths["stop"]
            self._batch_rescan_status = copy.deepcopy(initial_status)
        self._debug(
            "검색 결과 일괄 재스캔 별도 프로세스 시작",
            pid=process.pid,
            source=source,
            db_type=db_type,
            books=len(book_ids),
        )
        return {
            "started": True,
            "message": f"검색 결과 {len(book_ids):,}권의 일괄 재스캔을 시작했습니다.",
            "status": self.batch_rescan_status(),
        }

    def stop_batch_rescan(self):
        current_status = self.batch_rescan_status()
        with self._lock:
            if current_status.get("is_working") != "run":
                return {
                    "requested": False,
                    "message": "실행 중인 검색 결과 일괄 재스캔이 없습니다.",
                    "status": current_status,
                }
            stop_path = self._batch_rescan_stop_path
            if stop_path is None:
                paths = self._batch_rescan_paths()
                stop_path = paths["stop"] if paths is not None else None
            if stop_path is not None:
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.touch()
            self._batch_rescan_status["message"] = "현재 도서 처리 후 중지합니다."
            if self._batch_rescan_status_path is not None:
                self._write_database_migration_json(
                    self._batch_rescan_status_path,
                    self._batch_rescan_status,
                )
        self._debug("검색 결과 일괄 재스캔 중지 요청")
        return {
            "requested": True,
            "message": "일괄 재스캔 중지를 요청했습니다.",
            "status": self.batch_rescan_status(),
        }

    def _orphan_cleanup_paths(self, cover_root=None):
        root = str(
            cover_root or self.settings().get("cover_root_path") or ""
        ).strip()
        if not root:
            return None
        return self._maintenance_worker_paths(
            Path(root) / ".bookoasis_mate_jobs",
            "orphan_cleanup",
        )

    def orphan_cleanup_status(self):
        with self._lock:
            paths = self._orphan_cleanup_paths()
            if paths is not None:
                external = self._external_job_status(
                    self._orphan_cleanup_status_path or paths["status"],
                    "orphan_cleanup",
                    self._orphan_cleanup_process,
                    "고아 표지파일 정리 프로세스가 종료되었습니다.",
                )
                if external:
                    self._orphan_cleanup_status = copy.deepcopy(external)
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

        current_status = self.orphan_cleanup_status()
        with self._lock:
            if current_status.get("is_working") == "run":
                return {
                    "started": False,
                    "message": "고아 표지파일 정리가 이미 실행 중입니다.",
                    "status": current_status,
                }
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
        paths = self._orphan_cleanup_paths(root)
        worker_config = {
            "job_type": "orphan_cleanup",
            "delete_config_after_read": True,
            "settings": {
                "db_engine": settings.get("db_engine"),
                "general_db_path": settings.get("general_db_path"),
                "adult_db_path": settings.get("adult_db_path"),
                "audiobook_db_path": settings.get("audiobook_db_path"),
                "adult_enabled": settings.get("adult_enabled"),
                "mariadb_host": settings.get("mariadb_host"),
                "mariadb_port": settings.get("mariadb_port"),
                "mariadb_user": settings.get("mariadb_user"),
                "mariadb_password": settings.get("mariadb_password"),
                "mariadb_database_prefix": settings.get("mariadb_database_prefix"),
                "mariadb_connect_timeout": settings.get("mariadb_connect_timeout"),
                "mariadb_read_timeout": settings.get("mariadb_read_timeout"),
                "mariadb_write_timeout": settings.get("mariadb_write_timeout"),
                "cover_root_path": settings.get("cover_root_path"),
            },
            "db_type": db_type,
            "library_id": selected_library_id,
            "library_name": selected_name,
            "library_ids": library_ids,
            "dry_run": dry_run,
        }
        try:
            process = self._launch_maintenance_worker(
                paths,
                worker_config,
                self._orphan_cleanup_status,
                sensitive_config=True,
            )
        except Exception as error:
            with self._lock:
                self._orphan_cleanup_status.update(
                    {
                        "is_working": "error",
                        "message": "고아 표지파일 정리 프로세스를 시작하지 못했습니다.",
                        "error": str(error),
                    }
                )
            raise
        self._orphan_cleanup_process = process
        self._orphan_cleanup_status_path = paths["status"]
        self._orphan_cleanup_stop_path = paths["stop"]
        self._debug(
            "고아 표지파일 정리 별도 프로세스 시작",
            pid=process.pid,
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

    def stop_orphan_cleanup(self):
        current_status = self.orphan_cleanup_status()
        with self._lock:
            if current_status.get("is_working") != "run":
                return {
                    "requested": False,
                    "message": "실행 중인 고아 표지파일 정리 작업이 없습니다.",
                    "status": current_status,
                }
            stop_path = self._orphan_cleanup_stop_path
            if stop_path is None:
                paths = self._orphan_cleanup_paths()
                stop_path = paths["stop"] if paths is not None else None
            if stop_path is not None:
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.touch()
            self._orphan_cleanup_status["message"] = "중지 요청을 처리하는 중입니다."
            if self._orphan_cleanup_status_path is not None:
                self._write_database_migration_json(
                    self._orphan_cleanup_status_path,
                    self._orphan_cleanup_status,
                )
        self._debug("고아 표지파일 정리 중지 요청")
        return {
            "requested": True,
            "message": "중지를 요청했습니다.",
            "status": self.orphan_cleanup_status(),
        }

    def migration_config(self):
        model = self.P.ModelSetting
        config = {
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
        if config["import_mode"] == "merge":
            config["import_name"] = ""
        return config

    def _migration_engine(self, config=None, on_progress=None):
        config = config or self.migration_config()
        settings = self.settings()
        return CategoryMigrationEngine(
            config.get("work_dir"),
            settings.get("cover_root_path"),
            should_stop=self._migration_stop.is_set,
            on_progress=on_progress,
            database_settings=settings,
        )

    def migration_libraries(self, db_type="general"):
        target = self.engine().get_target(db_type)
        data = self._migration_engine().list_libraries(target.path, db_type)
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
            media_count=(
                data.get("audiobooks_count", 0)
                if data.get("media_kind") == "audiobook"
                else data.get("books_count", 0)
            ),
            covers=data.get("covers_count", 0),
            roots=data.get("root_paths_count", 0),
        )
        return data

    def migration_status(self):
        with self._lock:
            config = self.migration_config()
            if config.get("work_dir"):
                paths = self._maintenance_worker_paths(
                    config["work_dir"],
                    "category_migration",
                )
                external = self._external_job_status(
                    self._migration_status_path or paths["status"],
                    "category_migration",
                    self._migration_process,
                    "카테고리 이관 프로세스가 종료되었습니다.",
                )
                if external:
                    completed_now = (
                        self._migration_status.get("is_working") == "run"
                        and external.get("is_working") == "wait"
                    )
                    self._migration_status = copy.deepcopy(external)
                    if completed_now:
                        self._cached_report = None
                        self._cached_at = 0.0
                        self._settings_fingerprint = None
                        self._cover_issue_cache_key = None
                        self._cover_issue_cache = None
            return copy.deepcopy(self._migration_status)

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

        current_status = self.migration_status()
        with self._lock:
            if current_status.get("is_working") == "run":
                return {
                    "started": False,
                    "message": "카테고리 이관 작업이 이미 실행 중입니다.",
                    "status": current_status,
                }
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

        settings = self.settings()
        paths = self._maintenance_worker_paths(
            config["work_dir"],
            "category_migration",
        )
        worker_config = {
            **config,
            "job_type": "category_migration",
            "delete_config_after_read": True,
            "cover_root_path": settings.get("cover_root_path"),
            "target_general_db": settings.get("general_db_path"),
            "target_adult_db": settings.get("adult_db_path"),
            "target_audiobook_db": settings.get("audiobook_db_path"),
            "db_engine": settings.get("db_engine"),
            "mariadb_host": settings.get("mariadb_host"),
            "mariadb_port": settings.get("mariadb_port"),
            "mariadb_user": settings.get("mariadb_user"),
            "mariadb_password": settings.get("mariadb_password"),
            "mariadb_database_prefix": settings.get("mariadb_database_prefix"),
            "mariadb_connect_timeout": settings.get("mariadb_connect_timeout"),
            "mariadb_read_timeout": settings.get("mariadb_read_timeout"),
            "mariadb_write_timeout": settings.get("mariadb_write_timeout"),
        }
        try:
            process = self._launch_maintenance_worker(
                paths,
                worker_config,
                self._migration_status,
                sensitive_config=True,
            )
        except Exception as error:
            with self._lock:
                self._migration_status.update(
                    {
                        "is_working": "error",
                        "message": "카테고리 이관 프로세스를 시작하지 못했습니다.",
                        "error": str(error),
                    }
                )
                self._append_migration_log(self._migration_status["message"])
            raise
        self._migration_process = process
        self._migration_status_path = paths["status"]
        self._migration_stop_path = paths["stop"]
        self._debug(
            "카테고리 이관 별도 프로세스 시작",
            pid=process.pid,
            operation=operation,
        )
        return {
            "started": True,
            "message": "카테고리 이관 작업을 시작했습니다.",
            "status": self.migration_status(),
        }

    def stop_migration(self):
        current_status = self.migration_status()
        with self._lock:
            if current_status.get("is_working") != "run":
                return {
                    "requested": False,
                    "message": "실행 중인 카테고리 이관 작업이 없습니다.",
                    "status": current_status,
                }
            stop_path = self._migration_stop_path
            if stop_path is None:
                work_dir = self.migration_config().get("work_dir")
                if work_dir:
                    stop_path = self._maintenance_worker_paths(
                        work_dir,
                        "category_migration",
                    )["stop"]
            if stop_path is not None:
                stop_path.parent.mkdir(parents=True, exist_ok=True)
                stop_path.touch()
            self._migration_status["message"] = "중지 요청을 처리하는 중입니다."
            self._append_migration_log("사용자가 작업 중지를 요청했습니다.")
            if self._migration_status_path is not None:
                self._write_database_migration_json(
                    self._migration_status_path,
                    self._migration_status,
                )
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
                database_settings=settings,
                target_db_type=config["target_db_type"],
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
                database_settings=settings,
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
            settings = self.settings()
            data["target_users_source"] = "database"
            if self._admin_credentials_configured(settings):
                permissions = self.admin_client(settings).permissions()
                api_users = []
                if isinstance(permissions, dict) and permissions.get("success"):
                    api_users = sorted({
                        str(item.get("username") or "").strip()
                        for item in permissions.get("users", [])
                        if isinstance(item, dict) and str(item.get("username") or "").strip()
                    })
                if api_users:
                    data["target_users"] = api_users
                    targets_by_casefold = {
                        username.casefold(): username
                        for username in api_users
                    }
                    data["suggested_user_mappings"] = [
                        f"{source_user} => {targets_by_casefold[source_user.casefold()]}"
                        for source_user in data.get("source_users", [])
                        if source_user.casefold() in targets_by_casefold
                    ]
                    data["target_users_source"] = "permissions_api"
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
            "delete_config_after_read": True,
            "target_general_db": settings.get("general_db_path"),
            "target_adult_db": settings.get("adult_db_path"),
            "target_audiobook_db": settings.get("audiobook_db_path"),
            "target_cover_root": settings.get("cover_root_path"),
            "bookoasis_url": settings.get("bookoasis_url"),
            "api_timeout": settings.get("api_timeout", 30),
            "db_engine": settings.get("db_engine"),
            "mariadb_host": settings.get("mariadb_host"),
            "mariadb_port": settings.get("mariadb_port"),
            "mariadb_user": settings.get("mariadb_user"),
            "mariadb_password": settings.get("mariadb_password"),
            "mariadb_database_prefix": settings.get("mariadb_database_prefix"),
            "mariadb_connect_timeout": settings.get("mariadb_connect_timeout"),
            "mariadb_read_timeout": settings.get("mariadb_read_timeout"),
            "mariadb_write_timeout": settings.get("mariadb_write_timeout"),
        }
        self._database_migration_status_path = paths["status"]
        self._database_migration_stop_path = paths["stop"]
        self._write_database_migration_json(
            paths["config"],
            worker_config,
        )
        if os.name != "nt":
            os.chmod(paths["config"], 0o600)
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

    def custom_fonts(self, settings=None):
        settings = settings or self.settings()
        return CustomFontManager(settings.get("custom_font_dir")).list_fonts()

    def upload_custom_fonts(self, files, settings=None):
        settings = settings or self.settings()
        result = CustomFontManager(settings.get("custom_font_dir")).upload(files)
        self._debug(
            "커스텀 폰트 업로드 완료",
            uploaded=len(result.get("uploaded", [])),
            rejected=len(result.get("rejected", [])),
        )
        return result

    def connection_test(self, settings=None):
        started = time.monotonic()
        settings = settings or self.settings()
        engine = self.engine(settings)
        databases = [engine.inspect_target(target) for target in engine.targets()]
        api = BookOasisClient(settings.get("bookoasis_url"), settings.get("api_timeout", 30)).health()
        admin_api = self.admin_client(settings).login_admin()
        cover_root = str(settings.get("cover_root_path") or "").strip()
        cover_root_status = {"configured": bool(cover_root), "readable": bool(cover_root and Path(cover_root).is_dir())}
        font_root = str(settings.get("custom_font_dir") or "").strip()
        font_root_status = {
            "configured": bool(font_root),
            "writable": bool(
                font_root
                and Path(font_root).is_dir()
                and os.access(font_root, os.W_OK)
            ),
        }
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
            "font_root": font_root_status,
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
