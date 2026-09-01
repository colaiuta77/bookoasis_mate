# gd-poller 이벤트 수신 API와 영속 큐 기반 BookOasis 스캔 작업자를 제공합니다.
import threading
import time
import traceback
from datetime import datetime

from flask import jsonify, render_template

from .bookoasis_client import BookOasisClient
from .discord_notifier import DiscordWebhookError, DiscordWebhookNotifier
from .gdrive_changes import (
    GoogleDriveChangesClient,
    GoogleDriveChangesWatcher,
    google_drive_state_remote,
    list_google_drive_sources,
    parse_builtin_roots,
    rclone_config_output,
    rclone_version_output,
    resolve_google_drive_source,
    resolve_ff_rclone_settings,
)
from .gdrive_scan import (
    BookOasisDatabaseError,
    GDriveScanProcessor,
    infer_path_prefix_replacement,
    parse_extensions,
    replace_path_prefix,
    suggest_path_mapping,
    validate_event,
    webhook_payload_to_event_fields,
)
from .setup import *


class ModuleGDriveScan(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="gdrive_scan", first_menu="setting")
        self.db_default = {
            "gdrive_scan_enabled": "False",
            "gdrive_scan_input_mode": "command",
            "gdrive_scan_builtin_poll_seconds": "60",
            "gdrive_scan_builtin_remote": "",
            "gdrive_scan_builtin_root_id": "",
            "gdrive_scan_builtin_remote_path": "",
            "gdrive_scan_builtin_local_root": "",
            "gdrive_scan_builtin_roots": "[]",
            "gdrive_scan_rclone_path": "",
            "gdrive_scan_rclone_config_path": "",
            "gdrive_scan_buffer_seconds": "60",
            "gdrive_scan_worker_interval": "2",
            "gdrive_scan_max_attempts": "3",
            "gdrive_scan_extensions": ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json,.mp3,.m4b,.m4a,.flac,.aac,.wav,.ogg,.opus,.wma,.mp4,.mkv,.avi,.webm,.mov,.m4v,.ts,.smi,.srt,.vtt",
            "gdrive_scan_path_mappings": "/GDRIVE => /mnt/gds/GDRIVE",
            "gdrive_scan_vfs_rules": "/mnt/gds/GDRIVE|/GDRIVE|http://127.0.0.1:5572",
            "gdrive_scan_rc_timeout": "30",
            "gdrive_scan_changes_timeout": "60",
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
        self._last_builtin_poll_monotonic = 0.0
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

    @property
    def state_model(self):
        return getattr(P, "gdrive_scan_state_model", None)

    @property
    def item_model(self):
        return getattr(P, "gdrive_item_state_model", None)

    @staticmethod
    def _as_int(value, default, minimum, maximum):
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _builtin_state_view(root, state=None):
        state = state or {}
        return {
            "remote": str(root.get("remote") or ""),
            "source_remote": str(root.get("source_remote") or root.get("remote") or ""),
            "root_id": str(root.get("root_id") or ""),
            "remote_path": str(root.get("remote_path") or ""),
            "local_root": str(root.get("local_root") or ""),
            "status": str(state.get("status") or "unchecked"),
            "checkpoint_ready": bool(state.get("page_token")),
            "last_poll_at": state.get("last_poll_at"),
            "error": str(state.get("error") or ""),
        }

    def _settings(self):
        model = P.ModelSetting
        extensions = str(model.get("gdrive_scan_extensions") or "").strip()
        legacy_extensions = {
            ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json",
            ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json,.mp3,.m4b,.m4a,.flac,.aac,.wav,.ogg,.opus,.wma",
        }
        if extensions in legacy_extensions:
            extensions = self.db_default["gdrive_scan_extensions"]
        input_mode = str(model.get("gdrive_scan_input_mode") or "command").strip().lower()
        if input_mode not in {"command", "webhook", "builtin"}:
            input_mode = "command"
        builtin_roots = parse_builtin_roots(
            model.get("gdrive_scan_builtin_roots"),
            {
                "remote": model.get("gdrive_scan_builtin_remote"),
                "root_id": model.get("gdrive_scan_builtin_root_id"),
                "remote_path": model.get("gdrive_scan_builtin_remote_path"),
                "local_root": model.get("gdrive_scan_builtin_local_root"),
            },
        )
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
            "gdrive_scan_input_mode": input_mode,
            "gdrive_scan_builtin_poll_seconds": self._as_int(
                model.get("gdrive_scan_builtin_poll_seconds"), 60, 15, 3600
            ),
            "gdrive_scan_builtin_remote": str(model.get("gdrive_scan_builtin_remote") or "").strip(),
            "gdrive_scan_builtin_root_id": str(model.get("gdrive_scan_builtin_root_id") or "").strip(),
            "gdrive_scan_builtin_remote_path": str(model.get("gdrive_scan_builtin_remote_path") or "").strip(),
            "gdrive_scan_builtin_local_root": str(model.get("gdrive_scan_builtin_local_root") or "").strip(),
            "gdrive_scan_builtin_roots": builtin_roots,
            "gdrive_scan_rclone_path": str(
                model.get("gdrive_scan_rclone_path") or ""
            ).strip(),
            "gdrive_scan_rclone_config_path": str(
                model.get("gdrive_scan_rclone_config_path") or ""
            ).strip(),
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
            "gdrive_scan_changes_timeout": self._as_int(
                model.get("gdrive_scan_changes_timeout"), 60, 1, 300
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
        current_settings = self._settings()
        for key in self.db_default:
            if key == "gdrive_scan_builtin_roots":
                continue
            arg[key] = current_settings.get(key, arg.get(key, self.db_default[key]))
        arg["gdrive_scan_builtin_root_rows"] = current_settings["gdrive_scan_builtin_roots"]
        try:
            arg["gdrive_scan_builtin_states"] = [
                self._builtin_state_view(
                    root,
                    self.state_model.get(
                        google_drive_state_remote(root["remote"], root.get("source_remote")),
                        root["root_id"],
                    )
                    if self.state_model is not None
                    else None,
                )
                for root in current_settings["gdrive_scan_builtin_roots"]
            ]
        except Exception as error:
            arg["gdrive_scan_builtin_states"] = [
                self._builtin_state_view(root, {"status": "error", "error": str(error)})
                for root in current_settings["gdrive_scan_builtin_roots"]
            ]
        webhook_url = str(arg.pop("gdrive_scan_discord_webhook_url", "") or "")
        arg["gdrive_scan_discord_webhook_configured"] = bool(webhook_url.strip())
        try:
            rclone_path, rclone_config_path = resolve_ff_rclone_settings(
                getattr(F, "PluginManager", None),
                current_settings["gdrive_scan_rclone_path"],
                current_settings["gdrive_scan_rclone_config_path"],
            )
            arg["gdrive_scan_rclone"] = {
                "available": True,
                "path": rclone_path,
                "config": rclone_config_path,
                "source": "Mate 직접 설정"
                if current_settings["gdrive_scan_rclone_path"]
                else "FF rclone 설정",
            }
            arg["gdrive_scan_drive_sources"] = (
                list_google_drive_sources(
                    rclone_path,
                    rclone_config_path,
                    timeout=current_settings["gdrive_scan_rc_timeout"],
                )
                if page == "setting"
                else []
            )
        except Exception as error:
            arg["gdrive_scan_rclone"] = {"available": False, "error": str(error)}
            arg["gdrive_scan_drive_sources"] = []
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
        if settings["gdrive_scan_input_mode"] == "builtin":
            return jsonify({"ret": "fail", "msg": "자체 변경 감지 모드에서는 외부 이벤트 수신을 사용하지 않습니다."}), 409
        if settings["gdrive_scan_input_mode"] == "webhook" and not req.is_json:
            return jsonify({"ret": "fail", "msg": "WebhookDispatcher 모드는 JSON 요청만 받습니다."}), 415
        payload = req.get_json(silent=True) if req.is_json else {}
        payload = payload or req.form
        try:
            fields = webhook_payload_to_event_fields(payload)
            event = validate_event(
                fields["action"],
                fields["item_type"],
                fields["path"],
                fields["removed_path"],
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
            if command == "rclone_version":
                settings = self._settings()
                rclone_path = str(req.form.get("rclone_path") or "").strip()
                if not rclone_path:
                    rclone_path, _ = resolve_ff_rclone_settings(
                        getattr(F, "PluginManager", None),
                        settings["gdrive_scan_rclone_path"],
                        settings["gdrive_scan_rclone_config_path"],
                    )
                output = rclone_version_output(
                    rclone_path, timeout=settings["gdrive_scan_rc_timeout"]
                )
                return jsonify({
                    "ret": "success",
                    "msg": "rclone 버전을 확인했습니다.",
                    "data": {"output": output},
                })
            if command == "rclone_config":
                settings = self._settings()
                rclone_path, config_path = resolve_ff_rclone_settings(
                    getattr(F, "PluginManager", None),
                    req.form.get("rclone_path"),
                    req.form.get("config_path"),
                )
                output = rclone_config_output(
                    rclone_path,
                    config_path,
                    timeout=settings["gdrive_scan_rc_timeout"],
                )
                return jsonify({
                    "ret": "success",
                    "msg": "rclone.conf 설정을 확인했습니다.",
                    "data": {"output": output},
                })
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
            if command == "path_suggest":
                processor = GDriveScanProcessor(
                    self._settings(),
                    scan_callback=lambda *args: {},
                    logger=P.logger,
                )
                suggestion = suggest_path_mapping(
                    req.form.get("path"), processor.libraries
                )
                return jsonify(
                    {
                        "ret": "warning" if suggestion["warning"] else "success",
                        "msg": suggestion["warning"] or "경로 매핑 후보를 찾았습니다.",
                        "data": suggestion,
                    }
                )
            if command in {
                "failed_path_preview",
                "failed_path_retry",
                "failed_path_batch_preview",
                "failed_path_batch_retry",
            }:
                if self.model is None:
                    return jsonify({"ret": "danger", "msg": "이벤트 저장 모델을 사용할 수 없습니다."}), 503
                event = self.model.failed(req.form.get("id"))
                if not event:
                    return jsonify({"ret": "warning", "msg": "수정할 최종 실패 이벤트를 찾을 수 없습니다."}), 404
                validated, previews = self._validate_failed_paths(
                    event,
                    req.form.get("path"),
                    req.form.get("removed_path"),
                )
                if command.startswith("failed_path_batch_"):
                    data = self._prepare_failed_path_batch(event, validated)
                    response_data = {
                        key: value for key, value in data.items() if key != "updates"
                    }
                    if command == "failed_path_batch_preview":
                        return jsonify({
                            "ret": "success",
                            "msg": f"공통 오류 경로 {data['valid']}건을 확인했습니다.",
                            "data": response_data,
                        })
                    updated = self.model.update_failed_paths_batch_and_retry(data["updates"])
                    if updated:
                        self.wake_worker()
                    return jsonify({
                        "ret": "success" if updated else "warning",
                        "msg": f"공통 오류 경로 {updated}건을 수정해 재시도에 등록했습니다." if updated else "대상 이벤트 상태가 변경되어 수정하지 못했습니다.",
                        "data": {**response_data, "updated": updated},
                    })
                if command == "failed_path_preview":
                    return jsonify({"ret": "success", "msg": "수정 경로를 확인했습니다.", "data": {"event": validated, "previews": previews}})
                updated = self.model.update_failed_paths_and_retry(
                    event["id"], validated["path"], validated["removed_path"]
                )
                if updated:
                    self.wake_worker()
                return jsonify({"ret": "success" if updated else "warning", "msg": "수정한 경로로 재시도를 등록했습니다." if updated else "실패 이벤트 상태가 변경되어 수정하지 못했습니다."})
            if command in {"failed_retry_preview", "failed_retry_batch"}:
                if self.model is None:
                    return jsonify({"ret": "danger", "msg": "이벤트 저장 모델을 사용할 수 없습니다."}), 503
                event = self.model.failed(req.form.get("id"))
                if not event:
                    return jsonify({"ret": "warning", "msg": "재시도할 최종 실패 이벤트를 찾을 수 없습니다."}), 404
                data = self._prepare_failed_retry_batch(event)
                response_data = {
                    key: value for key, value in data.items() if key != "event_ids"
                }
                if command == "failed_retry_preview":
                    return jsonify({
                        "ret": "success",
                        "msg": f"같은 오류 {data['matched']}건 중 현재 처리 가능한 {data['valid']}건을 확인했습니다.",
                        "data": response_data,
                    })
                updated = self.model.retry_many(data["event_ids"])
                if updated:
                    self.wake_worker()
                return jsonify({
                    "ret": "success" if updated else "warning",
                    "msg": f"검증된 실패 이벤트 {updated}건을 재시도에 등록했습니다." if updated else "대상 이벤트 상태가 변경되어 재시도하지 못했습니다.",
                    "data": {**response_data, "updated": updated},
                })
            if command == "builtin_test":
                clients = self._builtin_clients(self._settings())
                checked = []
                for client in clients:
                    state_remote = getattr(client, "state_remote", client.remote)
                    source_remote = getattr(client, "source_remote", client.remote)
                    root = client.validate_root()
                    token = client.start_page_token()
                    saved = None
                    if self.state_model is not None:
                        current = self.state_model.get(state_remote, client.root_id) or {}
                        saved = self.state_model.save_cursor(
                            state_remote,
                            client.root_id,
                            current.get("page_token") or token,
                            status="ready",
                            error="",
                        )
                    checked.append({
                        **self._builtin_state_view(
                            {
                                "remote": client.remote,
                                "source_remote": source_remote,
                                "root_id": client.root_id,
                                "remote_path": client.remote_path,
                                "local_root": client.local_root,
                            },
                            saved or {"page_token": token, "status": "ready"},
                        ),
                        "root_name": root.get("name") or client.root_id,
                    })
                with self._worker_lock:
                    self._worker_state["last_error"] = ""
                return jsonify({"ret": "success", "msg": f"rclone 토큰과 자체 변경 감지 설정 {len(checked)}개의 연결을 확인했습니다.", "data": {"roots": checked}})
            if command == "builtin_reset":
                settings = self._settings()
                if self.state_model is None or self.item_model is None:
                    return jsonify({"ret": "danger", "msg": "자체 변경 감지 상태 모델을 사용할 수 없습니다."}), 503
                clients = self._builtin_clients(settings)
                for client in clients:
                    GoogleDriveChangesWatcher(
                        client, self.state_model, self.item_model, self.model,
                        parse_extensions(settings["gdrive_scan_extensions"]),
                        settings["gdrive_scan_buffer_seconds"],
                    ).reset()
                self._last_builtin_poll_monotonic = 0.0
                self.wake_worker()
                roots = [
                    self._builtin_state_view(
                        {
                            "remote": client.remote,
                            "source_remote": getattr(client, "source_remote", client.remote),
                            "root_id": client.root_id,
                            "remote_path": client.remote_path,
                            "local_root": client.local_root,
                        }
                    )
                    for client in clients
                ]
                return jsonify({"ret": "success", "msg": f"자체 변경 감지 체크포인트 {len(clients)}개를 초기화했습니다.", "data": {"roots": roots}})
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
        except ValueError as error:
            return jsonify({"ret": "warning", "msg": str(error)}), 400
        except Exception as error:
            P.logger.error(f"Google Drive 연동 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify(
                {"ret": "danger", "msg": "요청 처리에 실패했습니다. 로그를 확인해 주세요."}
            ), 500

    def _validate_failed_paths(self, event, path, removed_path, processor=None):
        path = str(path or "").strip()
        removed_path = str(removed_path or "").strip()
        if not path:
            raise ValueError("처리 대상 경로를 입력해 주세요.")
        if event.get("action") in {"move", "rename"} and not removed_path:
            raise ValueError("이동·이름 변경 이벤트는 이전 경로가 필요합니다.")
        settings = self._settings()
        validated = validate_event(
            event.get("action"), event.get("item_type"), path, removed_path,
            parse_extensions(settings["gdrive_scan_extensions"]),
        )
        if not validated["relevant"]:
            raise ValueError("수정한 경로는 현재 대상 확장자에 포함되지 않습니다.")
        processor = processor or GDriveScanProcessor(
            settings, scan_callback=lambda *args: {}, logger=P.logger
        )
        previews = []
        current_label = "이동 후 경로" if event.get("action") in {"move", "rename"} else "처리 대상 경로"
        for label, candidate in ((current_label, validated["path"]), ("이동 전 경로", validated["removed_path"])):
            if not candidate:
                continue
            preview = processor.preview_path(candidate)
            if preview.get("warning"):
                raise ValueError(f"{label}: {preview['warning']}")
            previews.append({"label": label, **preview})
        return validated, previews

    def _prepare_failed_path_batch(self, seed_event, corrected_event):
        source, replacement = infer_path_prefix_replacement(
            seed_event.get("path"), corrected_event.get("path")
        )
        candidates = self.model.failed_matching_prefix(source)
        processor = GDriveScanProcessor(
            self._settings(), scan_callback=lambda *args: {}, logger=P.logger
        )
        updates = []
        excluded = []
        for event in candidates:
            try:
                path = replace_path_prefix(event.get("path"), source, replacement)
                removed_path = replace_path_prefix(
                    event.get("removed_path"), source, replacement
                ) if event.get("removed_path") else ""
                validated, _ = self._validate_failed_paths(
                    event, path, removed_path, processor=processor
                )
                updates.append({"id": event["id"], **validated})
            except (BookOasisDatabaseError, ValueError) as error:
                excluded.append({"id": event.get("id"), "reason": str(error)})
        if not updates:
            raise ValueError("공통 오류 접두사로 수정할 수 있는 최종 실패 이벤트가 없습니다.")
        return {
            "source_prefix": source,
            "replacement_prefix": replacement,
            "matched": len(candidates),
            "valid": len(updates),
            "excluded": excluded[:50],
            "updates": updates,
        }

    def _prepare_failed_retry_batch(self, seed_event):
        error = str(seed_event.get("error") or "").strip()
        if not error:
            raise ValueError("같은 오류 대상을 찾으려면 실패 원인이 필요합니다.")
        candidates = self.model.failed_matching_error(error)
        processor = GDriveScanProcessor(
            self._settings(), scan_callback=lambda *args: {}, logger=P.logger
        )
        event_ids = []
        excluded = []
        for event in candidates:
            try:
                self._validate_failed_paths(
                    event,
                    event.get("path"),
                    event.get("removed_path"),
                    processor=processor,
                )
                event_ids.append(int(event["id"]))
            except (BookOasisDatabaseError, ValueError) as error_value:
                excluded.append({"id": event.get("id"), "reason": str(error_value)})
        if not event_ids:
            raise ValueError("같은 오류 중 현재 보관함 설정으로 처리 가능한 이벤트가 없습니다.")
        return {
            "error": error,
            "matched": len(candidates),
            "valid": len(event_ids),
            "excluded": excluded[:50],
            "event_ids": event_ids,
        }

    def _builtin_clients(self, settings):
        rclone_path, rclone_config_path = resolve_ff_rclone_settings(
            getattr(F, "PluginManager", None),
            settings.get("gdrive_scan_rclone_path"),
            settings.get("gdrive_scan_rclone_config_path"),
        )
        roots = settings.get("gdrive_scan_builtin_roots") or []
        if not roots:
            raise ValueError("Mate 자체 변경 감지 경로를 하나 이상 추가해 주세요.")
        sources = list_google_drive_sources(
            rclone_path,
            rclone_config_path,
            timeout=settings["gdrive_scan_rc_timeout"],
        )
        return [
            GoogleDriveChangesClient(
                rclone_path,
                rclone_config_path,
                root["remote"],
                root["root_id"],
                root["remote_path"],
                root["local_root"],
                timeout=settings["gdrive_scan_rc_timeout"],
                api_timeout=settings["gdrive_scan_changes_timeout"],
                source_remote=resolve_google_drive_source(
                    sources, root["remote"], root.get("source_remote")
                ),
            )
            for root in roots
        ]

    def _builtin_client(self, settings):
        return self._builtin_clients(settings)[0]

    def _poll_builtin_if_due(self, settings):
        if not settings["gdrive_scan_enabled"] or settings["gdrive_scan_input_mode"] != "builtin":
            return 0
        now = time.monotonic()
        if self._last_builtin_poll_monotonic and now - self._last_builtin_poll_monotonic < settings["gdrive_scan_builtin_poll_seconds"]:
            return 0
        self._last_builtin_poll_monotonic = now
        if self.model is None or self.state_model is None or self.item_model is None:
            raise RuntimeError("자체 변경 감지 상태 모델을 사용할 수 없습니다.")
        accepted = 0
        errors = []
        for client in self._builtin_clients(settings):
            watcher = GoogleDriveChangesWatcher(
                client, self.state_model, self.item_model, self.model,
                parse_extensions(settings["gdrive_scan_extensions"]),
                settings["gdrive_scan_buffer_seconds"],
            )
            try:
                accepted += watcher.poll_once()
            except Exception as error:
                errors.append(f"{client.remote}:{client.root_id} · {error}")
        if accepted:
            P.logger.info(f"[BookOasisMate] 자체 Google Drive 변경 이벤트 {accepted}건을 등록했습니다.")
        if errors:
            raise RuntimeError(" / ".join(errors))
        with self._worker_lock:
            self._worker_state["last_error"] = ""
        return accepted

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
                self._poll_builtin_if_due(settings)
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
            "gdrive_scan_input_mode",
            "gdrive_scan_builtin_poll_seconds",
            "gdrive_scan_builtin_remote",
            "gdrive_scan_builtin_root_id",
            "gdrive_scan_builtin_remote_path",
            "gdrive_scan_builtin_local_root",
            "gdrive_scan_builtin_roots",
            "gdrive_scan_rclone_path",
            "gdrive_scan_rclone_config_path",
            "gdrive_scan_buffer_seconds",
            "gdrive_scan_worker_interval",
            "gdrive_scan_max_attempts",
            "gdrive_scan_extensions",
            "gdrive_scan_path_mappings",
            "gdrive_scan_vfs_rules",
            "gdrive_scan_rc_timeout",
            "gdrive_scan_changes_timeout",
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
