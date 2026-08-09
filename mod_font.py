# BookOasis 커스텀 폰트 설정과 안전한 파일 업로드 화면을 제공합니다.
import time
import traceback

from flask import jsonify, render_template

from .setup import *


class ModuleFont(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="font")

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] 커스텀 폰트 메뉴 열기")
        arg = P.ModelSetting.to_dict()
        arg["custom_font_dir"] = self.service.settings().get("custom_font_dir", "")
        return render_template(f"{P.package_name}_{self.name}.html", arg=arg)

    def process_ajax(self, command, req):
        started = time.monotonic()
        safe_command = str(command or "").replace("\r", " ").replace("\n", " ")[:80]
        P.logger.debug(f"[BookOasisMate] 커스텀 폰트 AJAX 시작 command={safe_command}")
        try:
            values = req.form.to_dict()
            settings = self.service.settings_from_mapping(values) if values else self.service.settings()
            if command == "list":
                return jsonify({
                    "ret": "success",
                    "data": self.service.custom_fonts(settings),
                })
            if command == "upload":
                data = self.service.upload_custom_fonts(
                    req.files.getlist("files"),
                    settings,
                )
                return jsonify({
                    "ret": "success" if data.get("uploaded") else "warning",
                    "msg": data.get("message"),
                    "data": data,
                })
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate 커스텀 폰트 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요."}), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(f"[BookOasisMate] 커스텀 폰트 AJAX 종료 command={safe_command} duration_ms={duration_ms}")
