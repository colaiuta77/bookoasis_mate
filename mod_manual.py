# BookOasis Mate 공통 요구사항과 설치 안내 화면을 제공합니다.
import traceback

from flask import jsonify, render_template

from .dependency_installer import DependencyInstaller, DependencyInstallerError
from .setup import *


class ModuleManual(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="manual")
        self.installer = DependencyInstaller(P.logger)

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] 공통 매뉴얼 메뉴 열기")
        return render_template(
            f"{P.package_name}_{self.name}.html",
            arg={"dependency_status": self.installer.status()},
        )

    def process_ajax(self, command, req):
        try:
            if command == "dependency_status":
                return jsonify({
                    "ret": "success",
                    "msg": "",
                    "data": self.installer.status(),
                })
            if command == "install_dependency":
                if req.form.get("confirm_install") != "true":
                    return jsonify({
                        "ret": "warning",
                        "msg": "패키지 설치 확인이 필요합니다.",
                    })
                job = self.installer.start(req.form.get("key"))
                return jsonify({
                    "ret": "success",
                    "msg": "필수 패키지 설치를 시작했습니다.",
                    "data": job,
                })
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."})
        except DependencyInstallerError as error:
            return jsonify({"ret": "warning", "msg": str(error)})
        except Exception as error:
            P.logger.error(f"[BookOasisMate] 필수 패키지 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({
                "ret": "danger",
                "msg": "필수 패키지 요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요.",
            })
