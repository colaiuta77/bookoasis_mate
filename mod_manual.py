# BookOasis Mate 공통 요구사항과 설치 안내 화면을 제공합니다.
import traceback

from flask import jsonify, render_template

from .release_info import fetch_changelog
from .setup import *


class ModuleManual(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="manual")

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] 공통 매뉴얼 메뉴 열기")
        return render_template(f"{P.package_name}_{self.name}.html")

    def process_ajax(self, command, req):
        try:
            if command == "changelog":
                try:
                    data = fetch_changelog(force=req.form.get("force") == "true")
                    return jsonify({"ret": "success", "msg": "", "data": data})
                except Exception as error:
                    P.logger.warning(f"[BookOasisMate] GitHub Changelog 조회 실패: {error}")
                    return jsonify({
                        "ret": "warning",
                        "msg": "GitHub 변경 내역을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
                    })
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."})
        except Exception as error:
            P.logger.error(f"[BookOasisMate] 매뉴얼 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({
                "ret": "danger",
                "msg": "매뉴얼 요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요.",
            })
