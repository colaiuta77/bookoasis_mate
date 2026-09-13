# 독립 감지 프로세스와 로컬 폴더 전용 영속 큐·스캔 작업자를 관리합니다.
import json
import os
import posixpath
import subprocess
import sys
import threading
import time

from flask import jsonify, render_template

from .bookoasis_client import BookOasisClient
from .discord_notifier import DiscordWebhookNotifier
from .gdrive_scan import GDriveScanProcessor, map_path, parse_path_mappings, parse_extensions, validate_event
from .local_folder_watch import overlaps, validate_roots
from .setup import *


class ModuleLocalWatch(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="local_watch")
        self.db_default = {
            "local_watch_enabled": "False", "local_watch_roots": "[]",
            "local_watch_interval": "300", "local_watch_debounce": "10",
            "local_watch_max_entries": "200000",
            "local_watch_ignore_patterns": "@eaDir/\n#recycle/",
            "local_watch_discord_webhook_url": "",
            "local_watch_auto_cleanup": "True", "local_watch_retention_days": "30",
            "local_watch_extensions": ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json,.mp3,.m4b,.m4a,.flac,.aac,.wav,.ogg,.opus,.wma,.mp4,.mkv,.avi,.webm,.mov,.m4v,.ts,.smi,.srt,.vtt",
        }
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._process = None
        self._reader = None
        self._worker = None
        self._states = {}
        self._error = ""
        self._busy = False
        self._active_roots = []

    @property
    def installer(self):
        return next(module.installer for module in P.module_list if module.name == "setting")

    @property
    def model(self):
        return P.local_folder_model

    def _settings(self):
        with F.app.app_context():
            values = P.ModelSetting.to_dict()
        if not isinstance(values, dict):
            raise RuntimeError("설정 DB를 읽지 못했습니다. FlaskFarm 로그를 확인해 주세요.")
        return values

    def _config(self, overrides=None):
        values = {**self._settings(), **(overrides or {})}
        if not 1 <= int(values.get("local_watch_retention_days", 30)) <= 3650:
            raise ValueError("이벤트 보관 기간은 1~3650일로 입력해 주세요.")
        max_entries = int(values.get("local_watch_max_entries", 200000))
        if not 1 <= max_entries <= 700000:
            raise ValueError("감시 항목 한도는 1~700000으로 입력해 주세요.")
        drive = []
        if str(values.get("gdrive_scan_enabled")).lower() == "true" and values.get("gdrive_scan_input_mode") == "builtin":
            configured = json.loads(values.get("gdrive_scan_builtin_roots") or "[]")
            drive = [row.get("local_root", "") for row in configured]
            drive.append(values.get("gdrive_scan_builtin_local_root") or "")
            mappings = parse_path_mappings(values.get("gdrive_scan_path_mappings"))
            drive = [map_path(path, mappings) for path in drive if path]
        roots = validate_roots(values.get("local_watch_roots") or "[]", drive)
        return {"roots": roots, "interval": max(30, min(int(values.get("local_watch_interval") or 300), 86400)),
                "max_entries": max_entries, "extensions": sorted(parse_extensions(values.get("local_watch_extensions"))),
                "ignore_patterns": [line.strip() for line in values.get("local_watch_ignore_patterns", "@eaDir/\n#recycle/").splitlines() if line.strip()],
                "debounce": max(2, min(int(values.get("local_watch_debounce") or 10), 120))}

    def plugin_load(self):
        if self.model:
            self.model.recover_processing()
        try:
            if str(self._settings().get("local_watch_enabled")).lower() == "true":
                self.start()
        except Exception as error:
            self._error = str(error)
            P.logger.error(f"로컬 폴더 감지 시작 실패: {error}")

    def plugin_unload(self):
        self.stop()

    def running(self):
        return bool((self._process and self._process.poll() is None) or
                    (self._reader and self._reader.is_alive()) or (self._worker and self._worker.is_alive()))

    def start(self):
        with self._lock:
            if self.running():
                raise ValueError("이미 실행 중이거나 중지 중입니다.")
            job = self.installer.status("watchdog").get("job") or {}
            if job.get("key") == "watchdog" and job.get("status") in {"ready", "running"}:
                raise ValueError("watchdog 설치 완료 후 시작해 주세요.")
            if self.model is None:
                raise ValueError("로컬 이벤트 DB를 초기화하지 못했습니다.")
            config = self._config()
            self._stop.clear()
            self._error = ""
            self._states = {}
            self._active_roots = config["roots"]
            self.model.recover_processing()
            self._process = subprocess.Popen(
                [sys.executable, "-u", os.path.join(os.path.dirname(__file__), "local_folder_watch.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", bufsize=1,
            )
            self._process.stdin.write(json.dumps(config) + "\n")
            self._process.stdin.flush()
            self._reader = threading.Thread(target=self._read, daemon=True, name="mate-local-collector")
            self._worker = threading.Thread(target=self._work, daemon=True, name="mate-local-scanner")
            self._reader.start()
            self._worker.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            for state in self._states.values():
                state["status"] = "중지됨"
        process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        # 현재 BookOasis API 요청은 반환을 기다리고 다음 요청부터 중단합니다.

    def _read(self):
        process = self._process
        try:
            settings = self._settings()
            extensions = parse_extensions(settings.get("local_watch_extensions"))
            for line in process.stdout:
                if self._stop.is_set():
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    P.logger.warning("로컬 감지 프로세스에서 비정형 진단 출력을 받았습니다.")
                    continue
                if "events" in message:
                    if self._config()["roots"] != self._active_roots:
                        raise ValueError("설정이 변경되었습니다. 중지 후 다시 시작해 주세요.")
                    events = []
                    for raw in message["events"]:
                        if not any(raw["path"] == root["target"] or raw["path"].startswith(root["target"] + "/") for root in self._active_roots):
                            raise ValueError("감시 범위 밖 이벤트를 거부했습니다.")
                        event = validate_event(raw["action"], raw["item_type"], raw["path"], raw.get("removed_path"), extensions)
                        if event["relevant"]:
                            events.append(event)
                    with self._lock:
                        if self._stop.is_set():
                            break
                        self.model.enqueue_many(events, int(settings.get("local_watch_debounce") or 10))
                    process.stdin.write("ok\n")
                    process.stdin.flush()
                else:
                    with self._lock:
                        if self._stop.is_set():
                            break
                        key = message.get("path") or "service"
                        self._states[key] = {**self._states.get(key, {}), **message}
            if not self._stop.is_set():
                raise RuntimeError("감지 프로세스가 종료되었습니다. 오류 확인 후 다시 시작해 주세요.")
        except Exception as error:
            if not self._stop.is_set():
                self._error = str(error)
            self.stop()
        finally:
            process.stdout.close()
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def _scan(self, settings, db_type, library_id, name, relative=None, libraries=()):
        library = next((row for row in libraries if row["id"] == library_id and row["db_type"] == db_type), None)
        target = posixpath.join(library["root"], relative or "") if library else ""
        def blocked():
            with self._lock:
                roots = [root for root in self._active_roots if target and overlaps(root["target"], target)]
                return self._stop.is_set() or not roots or any(
                    self._states.get(root["path"], {}).get("status") != "감시 중" for root in roots)
        client = BookOasisClient(settings.get("bookoasis_url"), int(settings.get("api_timeout") or 30),
                                username=settings.get("bookoasis_username"), password=settings.get("bookoasis_password"))
        client.should_stop = blocked
        if blocked():
            return {"success": False, "cancelled": True, "message": "작업 중지 또는 감시 경로 확인 대기"}
        if settings.get("bookoasis_username") and settings.get("bookoasis_password"):
            if relative:
                result = client.scan_library_path(library_id, relative, db_type=db_type, force=False, timeout=120)
            else:
                result = client.scan_library(library_id, db_type=db_type, force=False)
            if result.get("success") or result.get("http_status") not in {404, 405} or result.get("outcome_unknown"):
                return result
        if blocked():
            return {"success": False, "cancelled": True, "message": "작업 중지 또는 감시 경로 확인 대기"}
        if relative:
            return client.request_scan_path(settings.get("webhook_token"), library_id, relative, db_type=db_type, timeout=120)
        return client.request_scan(settings.get("webhook_token"), library_id, db_type=db_type)

    def _work(self):
        last_cleanup = 0
        while not self._stop.wait(2):
            events = []
            try:
                if self._config()["roots"] != self._active_roots:
                    raise ValueError("설정이 변경되었습니다. 중지 후 다시 시작해 주세요.")
                if time.monotonic() - last_cleanup > 3600:
                    cleanup_settings = self._settings()
                    if str(cleanup_settings.get("local_watch_auto_cleanup", "True")).lower() == "true":
                        self.model.cleanup_terminal(int(cleanup_settings.get("local_watch_retention_days", 30)))
                    last_cleanup = time.monotonic()
                with self._lock:
                    if self._stop.is_set():
                        break
                    events = self.model.claim_ready(limit=100)
                    self._busy = bool(events)
                if not events:
                    continue
                ready = []
                for event in events:
                    root = next((root for root in self._active_roots if event["path"] == root["target"] or event["path"].startswith(root["target"] + "/")), None)
                    with self._lock:
                        healthy = root and self._states.get(root["path"], {}).get("status") == "감시 중"
                    if healthy:
                        ready.append(event)
                    else:
                        self.model.fail_or_retry(event, "감시 경로 확인 대기", result={"cancelled": True})
                events = ready
                if not events:
                    continue
                settings = self._settings()
                settings["gdrive_scan_path_mappings"] = ""
                settings["gdrive_scan_vfs_rules"] = ""
                processor = GDriveScanProcessor(settings,
                    scan_callback=lambda *args: self._scan(settings, *args, libraries=processor.libraries),
                    path_scan_callback=lambda *args: self._scan(settings, *args, libraries=processor.libraries),
                    logger=P.logger, should_stop=self._stop.is_set, refresh_vfs=False)
                results = processor.process_batch(events)
                statuses = {}
                for event in events:
                    result = results.get(event["id"], {"success": False, "message": "처리 결과 없음"})
                    if result.get("success"):
                        self.model.finish(event["id"], result)
                        statuses[event["id"]] = "completed"
                    else:
                        statuses[event["id"]] = self.model.fail_or_retry(event, result.get("message"), result=result)
                if not self._stop.is_set():
                    try:
                        DiscordWebhookNotifier(settings.get("local_watch_discord_webhook_url"), source_name="로컬 폴더").send_batch(events, results, statuses)
                    except Exception as error:
                        P.logger.warning(f"로컬 폴더 Discord 알림 실패: {error}")
            except Exception as error:
                self._error = str(error)
                for event in events:
                    self.model.fail_or_retry(event, str(error), result={"success": False, "cancelled": self._stop.is_set()})
                if not events:
                    self.stop()
            finally:
                self._busy = False

    def process_menu(self, page, req):
        return render_template(f"{P.package_name}_local_watch.html", arg=self._settings())

    def process_ajax(self, command, req):
        try:
            if command == "start":
                self.start()
                P.ModelSetting.set("local_watch_enabled", "True")
            elif command == "validate":
                if self.running():
                    raise ValueError("설정을 바꾸기 전에 작업을 중지해 주세요.")
                self._config(req.form.to_dict())
                DiscordWebhookNotifier(req.form.get("local_watch_discord_webhook_url", ""))
            elif command == "stop":
                P.ModelSetting.set("local_watch_enabled", "False")
                self.stop()
            elif command == "status":
                with self._lock:
                    roots = list(self._states.values())
                return jsonify({"ret": "success", "data": {"running": self.running(), "stopping": self._stop.is_set() and self.running(),
                    "busy": self._busy, "error": self._error, "roots": roots,
                    "counts": self.model.counts() if self.model else {},
                    "libraries": self.model.filter_options() if self.model else [],
                    "events": self.model.list_page(page=req.form.get("page", 1), page_size=req.form.get("page_size", 50),
                        order=req.form.get("order", "desc"), db_type=req.form.get("db_type", ""), library_id=req.form.get("library_id", ""),
                        action=req.form.get("action", ""), status=req.form.get("status", ""), search=req.form.get("search", "")) if self.model else {}}})
            elif command == "retry":
                self.model.retry_many([int(req.form["id"])])
            elif command == "delete":
                if not self.model.delete_terminal(int(req.form["id"])):
                    raise ValueError("완료·최종 실패 이벤트만 삭제할 수 있습니다. 상태를 새로 확인해 주세요.")
            elif command == "watchdog_status":
                return jsonify({"ret": "success", "data": self.installer.status("watchdog")})
            elif command == "install_watchdog":
                with self._lock:
                    if self.running():
                        raise ValueError("감지·스캔 작업을 중지한 뒤 설치해 주세요.")
                    if req.form.get("confirm_install") != "true":
                        raise ValueError("설치 확인이 필요합니다.")
                    job = self.installer.start("watchdog")
                return jsonify({"ret": "success", "data": job})
            elif command == "clear_pending":
                with self._lock:
                    if self.running():
                        raise ValueError("감지·스캔 작업을 중지한 뒤 삭제해 주세요.")
                    self.model.clear_pending()
            else:
                return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
            return jsonify({"ret": "success", "msg": "요청을 처리했습니다."})
        except Exception as error:
            return jsonify({"ret": "warning", "msg": str(error)}), 400
