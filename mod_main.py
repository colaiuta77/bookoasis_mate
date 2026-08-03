# BookOasis Mate의 진단 메뉴, AJAX 조회와 자동 검사 스케줄을 처리합니다.
import json
import time
import traceback

from flask import Response, jsonify, render_template

from .setup import *
from .mate_service import BookOasisMateService


class ModuleMain(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="main", first_menu="dashboard", scheduler_desc="BookOasis 라이브러리 자동 진단")
        self.db_default = {
            "bookoasis_root_path": "",
            "general_db_path": "/bookoasis-db/media_general.db",
            "adult_enabled": "False",
            "adult_db_path": "/bookoasis-db/media_adult.db",
            "audiobook_db_path": "/bookoasis-db/media_audiobook.db",
            "bookoasis_url": "http://127.0.0.1:5930",
            "bookoasis_username": "admin",
            "bookoasis_password": "",
            "webhook_token": "",
            "bookoasis_log_dir": "/volume1/docker/BookOasis_stable/logs",
            "cover_root_path": "",
            "cover_root_custom": "False",
            "custom_font_dir": "",
            "cover_min_width": "200",
            "cover_min_height": "280",
            "cover_min_file_size_kb": "5",
            "cover_min_aspect_percent": "35",
            "api_timeout": "30",
            "check_missing_isbn": "False",
            "stale_days": "14",
            "page_size": "50",
            "cache_seconds": "30",
            "history_limit": "100",
            "main_auto_start": "False",
            "main_interval": "60",
        }
        P.bookoasis_mate_service = BookOasisMateService(P)

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        page = page if page in {"dashboard", "issues", "scanner", "logs", "gaps", "covers", "orphan_covers", "history", "manual"} else "dashboard"
        P.logger.debug(f"[BookOasisMate] 메인 메뉴 열기 page={page}")
        arg = P.ModelSetting.to_dict()
        arg["page"] = page
        return render_template(f"{P.package_name}_{self.name}_{page}.html", arg=arg)

    def process_ajax(self, command, req):
        started = time.monotonic()
        safe_command = str(command or "").replace("\r", " ").replace("\n", " ")[:80]
        details = []
        for key in ("db_type", "library_id", "book_id", "issue_type", "mode", "source", "page", "all_libraries", "force", "dry_run", "filename"):
            value = req.form.get(key)
            if value not in (None, ""):
                safe_value = str(value).replace("\r", " ").replace("\n", " ")[:80]
                details.append(f"{key}={safe_value}")
        if req.form.get("search"):
            details.append("search=true")
        context = f" {' '.join(details)}" if details else ""
        P.logger.debug(f"[BookOasisMate] AJAX 시작 command={safe_command}{context}")
        try:
            if command == "report":
                return jsonify({"ret": "success", "data": self.service.report()})
            if command == "run_scan":
                return jsonify({"ret": "success", "msg": "검사를 완료했습니다.", "data": self.service.run_and_record("manual")})
            if command == "quick_check":
                data = self.service.quick_check(req.form.get("db_type", "general"))
                return jsonify({
                    "ret": "success" if data["success"] else "danger",
                    "msg": data["message"],
                    "data": data,
                })
            if command == "issues":
                data = self.service.issues(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    issue_type=req.form.get("issue_type", "all"),
                    search=req.form.get("search", ""),
                    page=req.form.get("page", 1),
                    page_size=req.form.get("page_size", P.ModelSetting.get("page_size")),
                )
                return jsonify({"ret": "success", "data": data})
            if command == "scanner":
                data = self.service.scanner(
                    db_type=req.form.get("db_type", "general"),
                    limit=req.form.get("limit", 100),
                    include_live=req.form.get("live", "false"),
                )
                return jsonify({"ret": "success", "data": data})
            if command == "rescan":
                data = self.service.request_rescan(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    all_libraries=req.form.get("all_libraries") == "true",
                    force=req.form.get("force"),
                )
                return jsonify({
                    "ret": "success" if data["success"] else "warning",
                    "msg": data.get("message") or (
                        f"재스캔 요청 {data['requested']}건 중 "
                        f"{data['queued']}건을 처리했습니다."
                    ),
                    "data": data,
                })
            if command == "cancel_library_scan":
                data = self.service.cancel_library_scan(
                    req.form.get("library_id"),
                    req.form.get("db_type", "general"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "보관함 스캔 취소 요청을 처리했습니다.",
                    "data": data,
                })
            if command == "scan_library_covers":
                data = self.service.scan_library_covers(
                    req.form.get("library_id"),
                    req.form.get("db_type", "general"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "보관함 표지 스캔 요청을 처리했습니다.",
                    "data": data,
                })
            if command == "clear_scan_queue":
                data = self.service.clear_scan_queue()
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "스캔 대기열 정리를 처리했습니다.",
                    "data": data,
                })
            if command == "cancel_scan_queue_task":
                data = self.service.cancel_scan_queue_task(
                    req.form.get("task_key"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "대기 작업 취소를 처리했습니다.",
                    "data": data,
                })
            if command == "book_scan":
                data = self.service.scan_book(
                    req.form.get("book_id"),
                    req.form.get("db_type", "general"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "개별 도서 재스캔 요청을 처리했습니다.",
                    "data": data,
                })
            if command == "batch_rescan_start":
                data = self.service.start_batch_rescan(
                    source=req.form.get("source"),
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    issue_type=req.form.get("issue_type", "all"),
                    mode=req.form.get("mode", "missing"),
                    search=req.form.get("search", ""),
                )
                return jsonify({
                    "ret": "success" if data.get("started") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            if command == "batch_rescan_status":
                return jsonify({
                    "ret": "success",
                    "data": self.service.batch_rescan_status(),
                })
            if command == "batch_rescan_stop":
                data = self.service.stop_batch_rescan()
                return jsonify({
                    "ret": "success" if data.get("requested") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            if command == "metadata_plugins":
                data = self.service.metadata_plugins()
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": None if data.get("success") else data.get("message") or data.get("error"),
                    "data": data,
                })
            if command == "metadata_search":
                data = self.service.search_metadata(
                    req.form.get("query", ""),
                    req.form.get("source"),
                    req.form.get("db_type", "general"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": None if data.get("success") else data.get("message") or data.get("error"),
                    "data": data,
                })
            if command == "metadata_apply":
                raw_item = req.form.get("item_data", "")
                if len(raw_item.encode("utf-8")) > 512 * 1024:
                    raise ValueError("메타데이터 적용 데이터가 너무 큽니다.")
                item_data = json.loads(raw_item)
                if not isinstance(item_data, dict) or not item_data:
                    raise ValueError("적용할 메타데이터가 올바르지 않습니다.")
                data = self.service.apply_metadata(
                    req.form.get("book_id"),
                    item_data,
                    req.form.get("source"),
                    req.form.get("db_type", "general"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "메타데이터 적용 요청을 처리했습니다.",
                    "data": data,
                })
            if command == "gaps":
                data = self.service.gaps(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    search=req.form.get("search", ""),
                    page=req.form.get("page", 1),
                    page_size=req.form.get("page_size", P.ModelSetting.get("page_size")),
                )
                return jsonify({"ret": "success", "data": data})
            if command == "gaps_export":
                data = self.service.gaps_csv(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    search=req.form.get("search", ""),
                )
                return Response(
                    data["content"],
                    mimetype="text/csv",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="{data["filename"]}"'
                        ),
                        "X-BookOasis-Export-Count": str(data["total"]),
                    },
                )
            if command == "log_catalog":
                return jsonify({"ret": "success", "data": self.service.log_catalog()})
            if command == "log_tail":
                data = self.service.log_tail(
                    req.form.get("filename"),
                    cursor_identity=req.form.get("cursor_identity", ""),
                    cursor_offset=req.form.get("cursor_offset"),
                    line_limit=req.form.get("line_limit", 500),
                )
                return jsonify({"ret": "success", "data": data})
            if command == "covers":
                data = self.service.covers(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    mode=req.form.get("mode", "missing"),
                    search=req.form.get("search", ""),
                    page=req.form.get("page", 1),
                    page_size=req.form.get("page_size") or None,
                    force=req.form.get("force"),
                )
                return jsonify({"ret": "success", "data": data})
            if command == "cover_inspection_start":
                data = self.service.start_cover_inspection(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    mode=req.form.get("mode", "resolution"),
                    search=req.form.get("search", ""),
                )
                return jsonify({
                    "ret": "success" if data.get("started") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            if command == "cover_inspection_status":
                data = self.service.cover_inspection_status(
                    mode=req.form.get("mode", "resolution"),
                    page=req.form.get("page", 1),
                    page_size=req.form.get("page_size") or None,
                )
                return jsonify({"ret": "success", "data": data})
            if command == "cover_inspection_stop":
                data = self.service.stop_cover_inspection()
                return jsonify({
                    "ret": "success" if data.get("requested") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            if command == "orphan_cleanup_start":
                data = self.service.start_orphan_cleanup(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    dry_run=req.form.get("dry_run", "true"),
                    confirm_delete=req.form.get("confirm_delete", "false"),
                )
                return jsonify({
                    "ret": "success" if data.get("started") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            if command == "orphan_cleanup_status":
                return jsonify({"ret": "success", "data": self.service.orphan_cleanup_status()})
            if command == "orphan_cleanup_stop":
                data = self.service.stop_orphan_cleanup()
                return jsonify({
                    "ret": "success" if data.get("requested") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            if command == "history":
                return jsonify({"ret": "success", "data": self.service.history()})
            if command == "clear_history":
                count = self.service.clear_history()
                return jsonify({"ret": "success", "msg": f"검사 이력 {count}건을 삭제했습니다.", "data": count})
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate AJAX 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요."}), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(f"[BookOasisMate] AJAX 종료 command={safe_command} duration_ms={duration_ms}")

    def scheduler_function(self):
        started = time.monotonic()
        P.logger.debug("[BookOasisMate] 자동 검사 시작")
        try:
            self.service.run_and_record("schedule")
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.info(f"BookOasis Mate 자동 검사를 완료했습니다. duration_ms={duration_ms}")
        except Exception as error:
            P.logger.error(f"BookOasis Mate 자동 검사 실패: {error}")
            P.logger.error(traceback.format_exc())

    def setting_save_after(self, change_list):
        scheduler_changed = any(key in change_list for key in ("main_auto_start", "main_interval"))
        P.logger.debug(
            f"[BookOasisMate] 설정 저장 후 처리 changed_count={len(change_list)} "
            f"scheduler_changed={str(scheduler_changed).lower()}"
        )
        self.service.invalidate()
        if scheduler_changed:
            try:
                P.logic.scheduler_stop(self.name)
            except Exception:
                pass
            if P.ModelSetting.get_bool("main_auto_start"):
                P.logic.scheduler_start(self.name)
