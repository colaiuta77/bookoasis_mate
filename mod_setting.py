# BookOasis 연결 설정과 안전한 연결·무결성 검사를 제공합니다.
import time
import traceback

from flask import jsonify, render_template

from .setup import *


class ModuleSetting(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="setting")

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] 설정 메뉴 열기")
        arg = P.ModelSetting.to_dict()
        settings = self.service.settings()
        for key in (
            "bookoasis_root_path",
            "general_db_path",
            "adult_db_path",
            "audiobook_db_path",
            "bookoasis_log_dir",
            "cover_root_path",
        ):
            arg[key] = settings.get(key, "")
        return render_template(f"{P.package_name}_{self.name}.html", arg=arg)

    def process_ajax(self, command, req):
        started = time.monotonic()
        safe_command = str(command or "").replace("\r", " ").replace("\n", " ")[:80]
        db_type = str(req.form.get("db_type", "general")).replace("\r", " ").replace("\n", " ")[:80]
        P.logger.debug(f"[BookOasisMate] 설정 AJAX 시작 command={safe_command} db_type={db_type}")
        try:
            values = req.form.to_dict()
            settings = self.service.settings_from_mapping(values) if values else self.service.settings()
            if command == "connection_test":
                data = self.service.connection_test(settings)
                return jsonify({
                    "ret": "success" if data["success"] else "warning",
                    "msg": "BookOasis 연결 확인을 완료했습니다.",
                    "data": data,
                })
            if command == "database_details":
                data = self.service.database_details(
                    req.form.get("db_type", "general"),
                    settings,
                )
                return jsonify({
                    "ret": "success" if data["success"] else "danger",
                    "msg": (
                        "DB 정보를 확인했습니다."
                        if data["success"]
                        else data.get("message") or "DB 정보를 확인하지 못했습니다."
                    ),
                    "data": data,
                })
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate 설정 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요."}), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(f"[BookOasisMate] 설정 AJAX 종료 command={safe_command} duration_ms={duration_ms}")
