# gd-poller 이벤트 수신 API와 영속 큐 기반 BookOasis 스캔 작업자를 제공합니다.
import threading
import time
import traceback
from datetime import datetime

from flask import jsonify, render_template

from .bookoasis_client import BookOasisClient
from .discord_notifier import DiscordWebhookError, DiscordWebhookNotifier
from .gdrive_scan import GDriveScanProcessor, parse_extensions, validate_event
from .setup import *


class ModuleGDriveScan(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="gdrive_scan", first_menu="setting")
        self.db_default = {
            "gdrive_scan_enabled": "False",
            "gdrive_scan_buffer_seconds": "60",
            "gdrive_scan_worker_interval": "2",
            "gdrive_scan_max_attempts": "3",
            "gdrive_scan_extensions": ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json,.mp3,.m4b,.m4a,.flac,.aac,.wav,.ogg,.opus,.wma",
            "gdrive_scan_path_mappings": "/GDRIVE => /mnt/gds/GDRIVE",
            "gdrive_scan_vfs_rules": "/mnt/gds/GDRIVE|/GDRIVE|http://127.0.0.1:5572",
            "gdrive_scan_rc_timeout": "30",
            "gdrive_scan_path_timeout": "120",
            "gdrive_scan_history_limit": "200",
            "gdrive_scan_retention_days": "30",
            "gdrive_scan_auto_cleanup": "True",
            "gdrive_scan_discord_webhook_url": "",
        }
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread = None
        self._worker_lock = threading.RLock()
        self._last_cleanup_monotonic = 0.0
        self._worker_state = {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": "",
            "last_batch_size": 0,
        }

    @property
    def model(self):
        return getattr(P, "gdrive_scan_model", None)

    @staticmethod
    def _as_int(value, default, minimum, maximum):
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    def _settings(self):
        model = P.ModelSetting
        extensions = str(model.get("gdrive_scan_extensions") or "").strip()
        legacy_extensions = ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json"
        if extensions == legacy_extensions:
            extensions = self.db_default["gdrive_scan_extensions"]
        return {
            "db_engine": model.get("db_engine") or "sqlite",
            "general_db_path": model.get("general_db_path"),
            "adult_enabled": model.get_bool("adult_enabled"),
            "adult_db_path": model.get("adult_db_path"),
            "audiobook_db_path": model.get("audiobook_db_path"),
            "video_db_path": model.get("video_db_path"),
            "mariadb_host": model.get("mariadb_host"),
            "mariadb_port": model.get("mariadb_port"),
            "mariadb_user": model.get("mariadb_user"),
            "mariadb_password": model.get("mariadb_password"),
            "mariadb_database_prefix": model.get("mariadb_database_prefix"),
            "mariadb_connect_timeout": model.get("mariadb_connect_timeout"),
            "mariadb_read_timeout": model.get("mariadb_read_timeout"),
            "mariadb_write_timeout": model.get("mariadb_write_timeout"),
            "bookoasis_url": model.get("bookoasis_url"),
            "bookoasis_username": model.get("bookoasis_username"),
            "bookoasis_password": model.get("bookoasis_password"),
            "webhook_token": model.get("webhook_token"),
            "api_timeout": self._as_int(model.get("api_timeout"), 30, 1, 30),
            "gdrive_scan_enabled": model.get_bool("gdrive_scan_enabled"),
            "gdrive_scan_buffer_seconds": self._as_int(
                model.get("gdrive_scan_buffer_seconds"), 60, 0, 3600
            ),
            "gdrive_scan_worker_interval": self._as_int(
                model.get("gdrive_scan_worker_interval"), 2, 1, 60
            ),
            "gdrive_scan_max_attempts": self._as_int(
                model.get("gdrive_scan_max_attempts"), 3, 1, 20
            ),
            "gdrive_scan_extensions": extensions,
            "gdrive_scan_path_mappings": model.get("gdrive_scan_path_mappings"),
            "gdrive_scan_vfs_rules": model.get("gdrive_scan_vfs_rules"),
            "gdrive_scan_rc_timeout": self._as_int(
                model.get("gdrive_scan_rc_timeout"), 30, 1, 300
            ),
            "gdrive_scan_path_timeout": self._as_int(
                model.get("gdrive_scan_path_timeout"), 120, 10, 600
            ),
            "gdrive_scan_history_limit": self._as_int(
                model.get("gdrive_scan_history_limit"), 200, 10, 1000
            ),
            "gdrive_scan_retention_days": self._as_int(
                model.get("gdrive_scan_retention_days"), 30, 1, 3650
            ),
            "gdrive_scan_auto_cleanup": model.get_bool("gdrive_scan_auto_cleanup"),
            "gdrive_scan_discord_webhook_url": str(
                model.get("gdrive_scan_discord_webhook_url") or ""
            ).strip(),
        }

    def process_menu(self, page, req):
        page = page if page in {"setting", "list", "manual"} else "setting"
        P.logger.debug(f"[BookOasisMate] Google Drive 연동 메뉴 열기 page={page}")
        arg = P.ModelSetting.to_dict()
        arg["gdrive_scan_extensions"] = self._settings()["gdrive_scan_extensions"]
        webhook_url = str(arg.pop("gdrive_scan_discord_webhook_url", "") or "")
        arg["gdrive_scan_discord_webhook_configured"] = bool(webhook_url.strip())
        arg["page"] = page
        return render_template(
            f"{P.package_name}_{self.name}_{page}.html",
            arg=arg,
        )

    def process_api(self, sub, req):
        if sub != "event":
            return jsonify({"ret": "fail", "msg": "지원하지 않는 API 요청입니다."}), 404
        if self.model is None:
            return jsonify({"ret": "fail", "msg": "이벤트 저장 모델을 사용할 수 없습니다."}), 503
        settings = self._settings()
        if not settings["gdrive_scan_enabled"]:
            return jsonify({"ret": "fail", "msg": "Google Drive 변경 감지 연동이 꺼져 있습니다."}), 503
        payload = req.get_json(silent=True) if req.is_json else {}
        payload = payload or req.form
        try:
            event = validate_event(
                payload.get("action"),
                payload.get("item_type") or payload.get("type"),
                payload.get("path"),
                payload.get("removed_path") or payload.get("previous_path"),
                parse_extensions(settings["gdrive_scan_extensions"]),
            )
            if not event["relevant"]:
                P.logger.debug(
                    "[BookOasisMate] 지원 대상이 아닌 파일 변경 이벤트를 건너뜁니다. "
                    f"action={event['action']} path={event['path']}"
                )
                return jsonify(
                    {
                        "ret": "success",
                        "accepted": False,
                        "ignored": True,
                        "msg": "지원 대상이 아닌 파일 유형이라 이벤트를 건너뛰었습니다.",
                    }
                )
            saved = self.model.enqueue(
                event,
                buffer_seconds=settings["gdrive_scan_buffer_seconds"],
            )
            self.wake_worker()
            P.logger.info(
                "[BookOasisMate] gd-poller 이벤트를 수신했습니다. "
                f"id={saved['id']} action={saved['action']} type={saved['item_type']} "
                f"path={saved['path']}"
            )
            return jsonify(
                {
                    "ret": "success",
                    "accepted": True,
                    "id": saved["id"],
                    "ready_at": saved["ready_at"],
                    "msg": "변경 이벤트를 스캔 대기열에 등록했습니다.",
                }
            )
        except ValueError as error:
            return jsonify({"ret": "fail", "msg": str(error)}), 400
        except Exception as error:
            P.logger.error(f"gd-poller 이벤트 저장 실패: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "fail", "msg": "변경 이벤트 저장에 실패했습니다."}), 500

    def process_ajax(self, command, req):
        try:
            if command == "discord_webhook_save":
                webhook_url = str(req.form.get("webhook_url") or "").strip()
                DiscordWebhookNotifier(webhook_url)
                P.ModelSetting.set("gdrive_scan_discord_webhook_url", webhook_url)
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "Discord 웹훅 설정을 저장했습니다."
                        if webhook_url
                        else "Discord 알림을 비활성화했습니다.",
                        "data": {"configured": bool(webhook_url)},
                    }
                )
            if command == "discord_webhook_clear":
                P.ModelSetting.set("gdrive_scan_discord_webhook_url", "")
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "Discord 웹훅 설정을 삭제했습니다.",
                        "data": {"configured": False},
                    }
                )
            if command == "path_preview":
                settings = self._settings()
                settings["gdrive_scan_path_mappings"] = req.form.get(
                    "path_mappings", settings["gdrive_scan_path_mappings"]
                )
                settings["gdrive_scan_vfs_rules"] = req.form.get(
                    "vfs_rules", settings["gdrive_scan_vfs_rules"]
                )
                processor = GDriveScanProcessor(
                    settings,
                    scan_callback=lambda *args: {},
                    logger=P.logger,
                )
                preview = processor.preview_path(req.form.get("path"))
                return jsonify(
                    {
                        "ret": "warning" if preview["warning"] else "success",
                        "msg": preview["warning"] or "경로 매핑과 보관함 연결을 확인했습니다.",
                        "data": preview,
                    }
                )
            if self.model is None:
                return jsonify(
                    {"ret": "danger", "msg": "이벤트 저장 모델을 사용할 수 없습니다."}
                ), 503
            if command == "list":
                settings = self._settings()
                max_page_size = settings["gdrive_scan_history_limit"]
                requested_page_size = self._as_int(
                    req.form.get("page_size"), 50, 10, max_page_size
                )
                listing = self.model.list_page(
                    page=req.form.get("page", 1),
                    page_size=requested_page_size,
                    status=req.form.get("status", ""),
                    action=req.form.get("action", ""),
                    db_type=req.form.get("db_type", ""),
                    library_id=req.form.get("library_id", ""),
                    search=req.form.get("search", ""),
                    order=req.form.get("order", "desc"),
                )
                data = {
                    **listing,
                    "counts": self.model.counts(),
                    "libraries": self.model.filter_options(),
                    "worker": self.worker_status(),
                    "enabled": settings["gdrive_scan_enabled"],
                }
                return jsonify({"ret": "success", "data": data})
            if command == "retry":
                retried = self.model.retry(req.form.get("id"))
                if retried:
                    self.wake_worker()
                return jsonify(
                    {
                        "ret": "success" if retried else "warning",
                        "msg": "실패 이벤트를 다시 대기열에 등록했습니다."
                        if retried
                        else "재시도할 실패 이벤트를 찾을 수 없습니다.",
                    }
                )
            if command == "wake":
                self.start_worker()
                self.wake_worker()
                return jsonify(
                    {"ret": "success", "msg": "변경 감지 작업자를 깨웠습니다."}
                )
            if command == "delete":
                deleted = self.model.delete_terminal(req.form.get("id"))
                return jsonify(
                    {
                        "ret": "success" if deleted else "warning",
                        "msg": "변경 이벤트를 삭제했습니다."
                        if deleted
                        else "완료 또는 최종 실패 이벤트만 삭제할 수 있습니다.",
                    }
                )
            if command in {"cleanup", "clear"}:
                settings = self._settings()
                deleted = self.model.cleanup_terminal(
                    retention_days=settings["gdrive_scan_retention_days"],
                    delete_all=command == "clear",
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": f"종료된 변경 이벤트 {deleted}건을 삭제했습니다.",
                        "deleted": deleted,
                    }
                )
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as error:
            P.logger.error(f"Google Drive 연동 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify(
                {"ret": "danger", "msg": "요청 처리에 실패했습니다. 로그를 확인해 주세요."}
            ), 500

    def _scan_callback(self, db_type, library_id, library_name):
        settings = self._settings()
        client = BookOasisClient(
            settings["bookoasis_url"],
            settings["api_timeout"],
        )
        response = client.request_scan(
            settings["webhook_token"],
            library_id,
            db_type=db_type,
            force=False,
        )
        P.logger.info(
            "[BookOasisMate] BookOasis 증분 스캔 요청 결과 "
            f"db={db_type} library_id={library_id} library={library_name} "
            f"success={str(bool(response.get('success'))).lower()} "
            f"already_queued={str(bool(response.get('already_queued'))).lower()}"
        )
        return response

    def _path_scan_callback(self, db_type, library_id, library_name, relative_path):
        settings = self._settings()
        client = BookOasisClient(
            settings["bookoasis_url"],
            settings["api_timeout"],
            username=settings["bookoasis_username"],
            password=settings["bookoasis_password"],
        )
        response = client.request_scan_path(
            settings["webhook_token"],
            library_id,
            relative_path,
            db_type=db_type,
            force=False,
            timeout=settings["gdrive_scan_path_timeout"],
        )
        if not response.get("success") and response.get("http_status") in {404, 405}:
            admin_response = client.scan_library_path(
                library_id,
                relative_path,
                db_type=db_type,
                force=False,
                timeout=settings["gdrive_scan_path_timeout"],
            )
            if admin_response.get("success"):
                response = {**admin_response, "mode": "admin_scan_path"}
            elif response.get("http_status") in {404, 405} and admin_response.get("http_status") in {404, 405}:
                full_response = client.request_scan(
                    settings["webhook_token"],
                    library_id,
                    db_type=db_type,
                    force=False,
                )
                response = {**full_response, "mode": "full_webhook_fallback"}
        P.logger.info(
            "[BookOasisMate] BookOasis 개별 경로 스캔 결과 "
            f"db={db_type} library_id={library_id} library={library_name} "
            f"path={relative_path} "
            f"mode={response.get('mode', 'unknown')} "
            f"success={str(bool(response.get('success'))).lower()}"
        )
        return response

    def _process_once(self):
        settings = self._settings()
        if not settings["gdrive_scan_enabled"] or self.model is None:
            return 0
        events = self.model.claim_ready(limit=500)
        if not events:
            return 0
        with self._worker_lock:
            self._worker_state["running"] = True
            self._worker_state["last_started_at"] = datetime.now().isoformat(
                timespec="seconds"
            )
            self._worker_state["last_batch_size"] = len(events)
            self._worker_state["last_error"] = ""
        P.logger.info(
            f"[BookOasisMate] Google Drive 변경 이벤트 {len(events)}건 처리를 시작합니다."
        )
        try:
            processor = GDriveScanProcessor(
                settings,
                scan_callback=self._scan_callback,
                path_scan_callback=self._path_scan_callback,
                logger=P.logger,
            )
            results = processor.process_batch(events)
        except Exception as error:
            P.logger.error(f"Google Drive 변경 이벤트 배치 처리 실패: {error}")
            P.logger.error(traceback.format_exc())
            results = {
                int(event["id"]): {
                    "success": False,
                    "message": str(error),
                }
                for event in events
            }
        try:
            max_attempts = settings["gdrive_scan_max_attempts"]
            statuses = {}
            for event in events:
                result = results.get(
                    int(event["id"]),
                    {"success": False, "message": "이벤트 처리 결과가 없습니다."},
                )
                if result.get("success"):
                    self.model.finish(event["id"], result)
                    statuses[int(event["id"])] = "completed"
                else:
                    statuses[int(event["id"])] = self.model.fail_or_retry(
                        event,
                        result.get("message"),
                        max_attempts=max_attempts,
                    )
            try:
                DiscordWebhookNotifier(
                    settings["gdrive_scan_discord_webhook_url"]
                ).send_batch(events, results, statuses)
            except DiscordWebhookError as error:
                P.logger.warning(
                    "[BookOasisMate] Discord 변경 요약 전송 실패: "
                    f"{error}"
                )
            except Exception as error:
                P.logger.warning(
                    "[BookOasisMate] Discord 변경 요약 전송 실패: "
                    f"{type(error).__name__}"
                )
            P.logger.info(
                f"[BookOasisMate] Google Drive 변경 이벤트 {len(events)}건 처리를 마쳤습니다."
            )
            return len(events)
        finally:
            with self._worker_lock:
                self._worker_state["running"] = False
                self._worker_state["last_finished_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )

    def _worker_loop(self):
        if self.model is not None:
            try:
                recovered = self.model.recover_processing()
                if recovered:
                    P.logger.warning(
                        f"[BookOasisMate] 중단된 변경 이벤트 {recovered}건을 복구했습니다."
                    )
            except Exception as error:
                P.logger.error(f"변경 이벤트 복구 실패: {error}")
                P.logger.error(traceback.format_exc())
        while not self._stop_event.is_set():
            settings = self._settings()
            interval = settings["gdrive_scan_worker_interval"]
            try:
                self._cleanup_if_due(settings)
                processed = self._process_once()
                if processed:
                    continue
            except Exception as error:
                with self._worker_lock:
                    self._worker_state["last_error"] = str(error)
                P.logger.error(f"Google Drive 변경 감지 작업자 오류: {error}")
                P.logger.error(traceback.format_exc())
            self._wake_event.wait(interval)
            self._wake_event.clear()

    def _cleanup_if_due(self, settings):
        if self.model is None or not settings["gdrive_scan_auto_cleanup"]:
            return 0
        now = time.monotonic()
        if self._last_cleanup_monotonic and now - self._last_cleanup_monotonic < 3600:
            return 0
        self._last_cleanup_monotonic = now
        deleted = self.model.cleanup_terminal(
            retention_days=settings["gdrive_scan_retention_days"]
        )
        if deleted:
            P.logger.info(
                "[BookOasisMate] 보존기간이 지난 Google Drive 변경 이벤트 "
                f"{deleted}건을 자동 삭제했습니다."
            )
        return deleted

    def start_worker(self):
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return False
            self._stop_event.clear()
            self._wake_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="bookoasis-mate-gdrive-scan",
                daemon=True,
            )
            self._worker_thread.start()
            return True

    def wake_worker(self):
        self._wake_event.set()

    def stop_worker(self):
        self._stop_event.set()
        self._wake_event.set()
        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)

    def worker_status(self):
        with self._worker_lock:
            data = dict(self._worker_state)
            data["alive"] = bool(
                self._worker_thread is not None and self._worker_thread.is_alive()
            )
        return data

    def plugin_load(self):
        self.start_worker()

    def plugin_unload(self):
        self.stop_worker()

    def setting_save_after(self, change_list):
        keys = {
            "gdrive_scan_enabled",
            "gdrive_scan_buffer_seconds",
            "gdrive_scan_worker_interval",
            "gdrive_scan_max_attempts",
            "gdrive_scan_extensions",
            "gdrive_scan_path_mappings",
            "gdrive_scan_vfs_rules",
            "gdrive_scan_rc_timeout",
            "gdrive_scan_history_limit",
            "gdrive_scan_retention_days",
            "gdrive_scan_auto_cleanup",
            "general_db_path",
            "db_engine",
            "adult_enabled",
            "adult_db_path",
            "audiobook_db_path",
            "video_db_path",
            "mariadb_host",
            "mariadb_port",
            "mariadb_user",
            "mariadb_password",
            "mariadb_database_prefix",
            "mariadb_connect_timeout",
            "mariadb_read_timeout",
            "mariadb_write_timeout",
            "bookoasis_url",
            "webhook_token",
        }
        if keys.intersection(change_list):
            self.start_worker()
            self.wake_worker()
