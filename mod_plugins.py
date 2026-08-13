# BookOasis 플러그인 카탈로그와 GitHub·ZIP 설치 관리 화면을 제공합니다.
import json
import time
import traceback

from flask import jsonify, render_template

from .plugin_manager import BookOasisPluginManager, PluginManagerError
from .setup import *


class ModulePlugins(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="plugins")
        self.db_default = {
            "plugin_manager_plugins_path": "",
            "plugin_manager_work_dir": "",
            "plugin_manager_backup_keep": "5",
            "plugin_manager_max_archive_mb": "100",
            "plugin_manager_max_extracted_mb": "500",
            "plugin_manager_max_files": "2000",
            "plugin_manager_discovery_topics": "bookoasis-plugin",
            "plugin_manager_discovery_cache_hours": "6",
        }
        self.manager = BookOasisPluginManager(P.logger)

    def _settings(self):
        return P.ModelSetting.to_dict()

    def _installed_overview(self, settings):
        local_plugins = self.manager.installed(settings)
        runtime_data = {}
        runtime_available = False
        runtime_message = ""
        try:
            runtime_data = P.bookoasis_mate_service.plugin_management() or {}
            runtime_available = bool(runtime_data.get("success"))
            runtime_message = str(
                runtime_data.get("message") or runtime_data.get("error") or ""
            )
        except Exception as error:
            runtime_message = "BookOasis 실행 상태를 조회하지 못했습니다."
            P.logger.warning(
                "[BookOasisMate] 플러그인 실행 상태 조회 실패: "
                f"{type(error).__name__}"
            )

        runtime_by_id = {
            str(item.get("id") or ""): item
            for item in runtime_data.get("plugins") or []
            if item.get("id")
        }
        merged = []
        for local_item in local_plugins:
            item = dict(local_item)
            runtime = runtime_by_id.get(str(item.get("id") or ""))
            item.update(
                {
                    "runtime_available": bool(runtime_available and runtime),
                    "enabled": runtime.get("enabled") if runtime else None,
                    "load_status": runtime.get("load_status") if runtime else "unknown",
                    "load_message": runtime.get("load_message") if runtime else "",
                    "config_fields": runtime.get("config_fields") if runtime else [],
                    "update_supported": bool(
                        runtime and runtime.get("update_supported")
                    ),
                    "custom_settings_ui": bool(
                        runtime and runtime.get("custom_settings_ui")
                    ),
                }
            )
            merged.append(item)
        return {
            "plugins": merged,
            "runtime_available": runtime_available,
            "runtime_message": runtime_message,
            "load_status_supported": bool(
                runtime_data.get("load_status_supported")
            ),
        }

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] BookOasis 플러그인 관리 메뉴 열기")
        arg = self._settings()
        arg["effective_paths"] = self.manager.effective_paths(arg)
        return render_template(f"{P.package_name}_{self.name}.html", arg=arg)

    def process_ajax(self, command, req):
        started = time.monotonic()
        safe_command = str(command or "").replace("\r", " ").replace("\n", " ")[:80]
        P.logger.debug(
            f"[BookOasisMate] 플러그인 관리 AJAX 시작 command={safe_command}"
        )
        try:
            settings = self._settings()
            if command == "catalog":
                return jsonify(
                    {
                        "ret": "success",
                        "data": self.manager.catalog(
                            settings,
                            refresh_remote=req.form.get("refresh_remote") == "true",
                        ),
                    }
                )
            if command == "installed":
                return jsonify(
                    {"ret": "success", "data": self._installed_overview(settings)}
                )
            if command == "runtime":
                data = P.bookoasis_mate_service.plugin_management()
                return jsonify({
                    "ret": "success" if data.get("success") else "warning",
                    "msg": data.get("message") or data.get("error") or "",
                    "data": data,
                })
            if command == "runtime_toggle":
                data = P.bookoasis_mate_service.toggle_plugin(
                    req.form.get("plugin_id"),
                    str(req.form.get("enabled") or "").lower() == "true",
                )
                return jsonify({"ret": "success" if data.get("success") else "warning", "msg": data.get("message") or data.get("error") or "", "data": data})
            if command == "runtime_config_save":
                changes = json.loads(req.form.get("changes") or "{}")
                clear_keys = json.loads(req.form.get("clear_keys") or "[]")
                if not isinstance(changes, dict) or not isinstance(clear_keys, list):
                    raise PluginManagerError("플러그인 설정 데이터가 올바르지 않습니다.")
                data = P.bookoasis_mate_service.save_plugin_config(
                    req.form.get("plugin_id"), changes, clear_keys
                )
                return jsonify({"ret": "success" if data.get("success") else "warning", "msg": data.get("message") or data.get("error") or "", "data": data})
            if command == "runtime_update":
                data = P.bookoasis_mate_service.sample_update_plugin(req.form.get("plugin_id"))
                return jsonify({"ret": "success" if data.get("success") else "warning", "msg": data.get("message") or data.get("error") or "", "data": data})
            if command == "discovery":
                return jsonify({"ret": "success", "data": self.manager.discovery(settings)})
            if command == "discovery_refresh":
                return jsonify({"ret": "success", "data": self.manager.start_discovery_refresh(settings)})
            if command == "discovery_status":
                return jsonify({"ret": "success", "data": self.manager.discovery_status()})
            if command == "installed_delete":
                if req.form.get("confirm_delete") != "true":
                    raise PluginManagerError("설치 플러그인 삭제 확인이 필요합니다.")
                data = self.manager.delete_installed_plugin(
                    req.form.get("plugin_id"), settings
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "설치 플러그인을 백업한 뒤 삭제했습니다. BookOasis를 재시작해 주세요.",
                        "data": data,
                    }
                )
            if command == "paths":
                return jsonify(
                    {
                        "ret": "success",
                        "data": self.manager.effective_paths(settings),
                    }
                )
            if command == "status":
                return jsonify({"ret": "success", "data": self.manager.status()})
            if command == "history":
                return jsonify(
                    {"ret": "success", "data": self.manager.history(settings)}
                )
            if command == "custom_catalog_save":
                data = self.manager.save_custom_catalog_item(
                    req.form.get("repository"),
                    req.form.get("ref"),
                    req.form.get("plugin_id"),
                    req.form.get("name"),
                    req.form.get("description"),
                    req.form.get("verified"),
                    settings,
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "사용자 GitHub 플러그인을 카탈로그에 추가했습니다.",
                        "data": data,
                    }
                )
            if command == "custom_catalog_delete":
                data = self.manager.delete_custom_catalog_item(
                    req.form.get("plugin_id"), settings
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "사용자 카탈로그 항목을 삭제했습니다. 설치된 플러그인은 삭제하지 않았습니다.",
                        "data": data,
                    }
                )
            if command == "catalog_install":
                if req.form.get("confirm_install") != "true":
                    raise PluginManagerError("플러그인 실행 권한 부여 확인이 필요합니다.")
                data = self.manager.start_catalog_install(
                    req.form.get("plugin_id"), settings
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "카탈로그 플러그인 설치·업데이트를 시작했습니다.",
                        "data": data,
                    }
                )
            if command == "github_inspect":
                data = self.manager.start_github_inspect(
                    req.form.get("repository"),
                    req.form.get("ref"),
                    req.form.get("plugin_id"),
                    settings,
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "GitHub 플러그인 패키지 검사를 시작했습니다.",
                        "data": data,
                    }
                )
            if command == "zip_inspect":
                data = self.manager.start_zip_inspect(
                    req.files.get("archive"),
                    req.form.get("plugin_id"),
                    settings,
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "업로드 ZIP 플러그인 검사를 시작했습니다.",
                        "data": data,
                    }
                )
            if command == "prepared_install":
                if req.form.get("confirm_install") != "true":
                    raise PluginManagerError("미검증 플러그인 실행 권한 부여 확인이 필요합니다.")
                data = self.manager.start_prepared_install(
                    req.form.get("inspect_job_id"), settings
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "검사 완료 플러그인 설치를 시작했습니다.",
                        "data": data,
                    }
                )
            if command == "stop":
                data = self.manager.stop()
                return jsonify(
                    {
                        "ret": "success" if data.get("requested") else "warning",
                        "msg": data.get("message"),
                        "data": data,
                    }
                )
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except PluginManagerError as error:
            return jsonify({"ret": "warning", "msg": str(error)}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate 플러그인 관리 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify(
                {
                    "ret": "danger",
                    "msg": "플러그인 관리 요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요.",
                }
            ), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(
                f"[BookOasisMate] 플러그인 관리 AJAX 종료 command={safe_command} duration_ms={duration_ms}"
            )
