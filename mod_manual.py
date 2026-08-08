# BookOasis Mate 공통 요구사항과 설치 안내 화면을 제공합니다.
from flask import render_template

from .setup import *


class ModuleManual(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="manual")

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] 공통 매뉴얼 메뉴 열기")
        return render_template(f"{P.package_name}_{self.name}.html")
