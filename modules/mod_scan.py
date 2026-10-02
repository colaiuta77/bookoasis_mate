# 스캔 상태와 코어 스캔 스케줄 관리 화면 및 요청을 처리합니다.
import traceback

from flask import jsonify, render_template

from ..setup import *


class ModuleScan(PluginModuleBase):
    commands = {
        "tts_status", "scanner", "scan_scheduler_data", "scheduler_preview",
        "scheduler_update", "scheduler_enabled", "rescan", "cancel_library_scan",
        "scan_library_covers", "clear_scan_queue", "cancel_scan_queue_task",
        "core_scan_status", "event_book",
    }

    def __init__(self, plugin):
        super().__init__(plugin, name="scan", first_menu="scanner")

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        page = page if page in {"scanner", "scheduler"} else "scanner"
        arg = P.ModelSetting.to_dict()
        arg["page"] = page
        return render_template(f"{P.package_name}_{self.name}_{page}.html", arg=arg)

    def process_ajax(self, command, req):
        try:
            if command == "core_scan_status":
                return jsonify({"ret": "success", "data": self.service.core_scan_status(req.form.get("db_type", "general"))})
            if command == "event_book":
                book = self.service.engine().event_book(
                    req.form.get("db_type", "general"), req.form.get("library_id"),
                    req.form.get("path"), req.form.get("item_type", "file"),
                )
                return jsonify({"ret": "success", "data": book})
            if command == "tts_status":
                return jsonify({"ret": "success", "data": self.service.admin_client().tts_status()})
            if command == "scanner":
                data = self.service.scanner(
                    db_type=req.form.get("db_type", "general"),
                    limit=req.form.get("limit", 100),
                    include_live=req.form.get("live", "false"),
                )
                return jsonify({"ret": "success", "data": data})
            if command == "scan_scheduler_data":
                data = self.service.scan_scheduler()
                data["backend_revision"] = "scheduler-v2"
                return jsonify({"ret": "success", "data": data})
            if command == "scheduler_preview":
                data = self.service.scan_scheduler(
                    preview={
                        "db_type": req.form.get("db_type", "general"),
                        "library_id": req.form.get("library_id"),
                        "cron_schedule": req.form.get("cron_schedule", ""),
                    }
                )
                return jsonify({"ret": "success", "data": data})
            if command == "scheduler_update":
                data = self.service.update_scan_schedule(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    cron_schedule=req.form.get("cron_schedule", ""),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "스캔 스케줄을 저장했습니다.",
                    "data": data,
                })
            if command == "scheduler_enabled":
                data = self.service.update_scan_schedule_enabled(
                    db_type=req.form.get("db_type", "general"),
                    library_id=req.form.get("library_id"),
                    enabled=req.form.get("enabled", "false"),
                )
                return jsonify({
                    "ret": "success" if data.get("success") else "danger",
                    "msg": data.get("message") or data.get("error") or "스캔 스케줄 상태를 변경했습니다.",
                    "data": data,
                })
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
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate 스캔 관리 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요."}), 500
