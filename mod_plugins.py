# BookOasis 플러그인 카탈로그와 GitHub·ZIP 설치 관리 화면을 제공합니다.
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
        }
        self.manager = BookOasisPluginManager(P.logger)

    def _settings(self):
        return P.ModelSetting.to_dict()

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
                    {"ret": "success", "data": self.manager.installed(settings)}
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
