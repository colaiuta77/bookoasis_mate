# BookOasis Mate의 메뉴와 FlaskFarm 플러그인 모듈을 등록합니다.
import traceback

setting = {
    "filepath": __file__,
    "use_db": True,
    "use_default_setting": True,
    "home_module": "main",
    "menu": {
        "uri": __package__,
        "name": "BookOasis Mate",
        "list": [
            {"uri": "setting", "name": "설정"},
            {
                "uri": "main",
                "name": "라이브러리 진단",
                "list": [
                    {"uri": "dashboard", "name": "상태 요약"},
                    {"uri": "statistics", "name": "라이브러리 통계"},
                    {"uri": "scanner", "name": "스캔 상태"},
                    {"uri": "issues", "name": "문제 도서"},
                    {"uri": "gaps", "name": "시리즈 누락"},
                    {"uri": "covers", "name": "표지 검사"},
                    {"uri": "orphan_covers", "name": "고아 표지파일 정리"},
                    {"uri": "history", "name": "검사 이력"},
                    {"uri": "logs", "name": "BookOasis 로그"},
                    {"uri": "manual", "name": "매뉴얼"},
                ],
            },
            {
                "uri": "migration",
                "name": "카테고리 이관",
                "list": [
                    {"uri": "setting", "name": "복사 설정"},
                    {"uri": "status", "name": "복사 상태"},
                    {"uri": "manual", "name": "매뉴얼"},
                ],
            },
            {
                "uri": "database_migration",
                "name": "통DB 이관",
                "list": [
                    {"uri": "setting", "name": "이관 설정"},
                    {"uri": "status", "name": "이관 상태"},
                    {"uri": "manual", "name": "이관 매뉴얼"},
                ],
            },
            {
                "uri": "gdrive_scan",
                "name": "Google Drive 연동",
                "list": [
                    {"uri": "setting", "name": "연동 설정"},
                    {"uri": "list", "name": "변경 이벤트"},
                    {"uri": "manual", "name": "연동 매뉴얼"},
                ],
            },
            {"uri": "sql", "name": "SQL 도구"},
            {"uri": "plugins", "name": "플러그인 관리"},
            {"uri": "font", "name": "커스텀 폰트"},
            {"uri": "manual", "name": "매뉴얼"},
            {"uri": "log", "name": "로그"},
        ],
    },
    "setting_menu": None,
    "default_route": "normal",
}

from plugin import *

P = create_plugin_instance(setting)

P.history_model = None
try:
    from .model_history import ModelScanHistory

    P.history_model = ModelScanHistory
except Exception as error:
    P.logger.error(f"BookOasis Mate 검사 이력 모델을 초기화하지 못했습니다: {error}")
    P.logger.error(traceback.format_exc())

P.gdrive_scan_model = None
try:
    from .model_gdrive_scan import ModelGDriveScanEvent

    P.gdrive_scan_model = ModelGDriveScanEvent
except Exception as error:
    P.logger.error(f"BookOasis Mate 변경 이벤트 모델을 초기화하지 못했습니다: {error}")
    P.logger.error(traceback.format_exc())

P.library_statistics_model = None
try:
    from .model_library_statistics import ModelLibraryStatisticsSnapshot

    P.library_statistics_model = ModelLibraryStatisticsSnapshot
except Exception as error:
    P.logger.error(f"BookOasis Mate 통계 스냅샷 모델을 초기화하지 못했습니다: {error}")
    P.logger.error(traceback.format_exc())

from .mod_main import ModuleMain
from .mod_database_migration import ModuleDatabaseMigration
from .mod_gdrive_scan import ModuleGDriveScan
from .mod_font import ModuleFont
from .mod_migration import ModuleMigration
from .mod_manual import ModuleManual
from .mod_sql import ModuleSql
from .mod_plugins import ModulePlugins
from .mod_setting import ModuleSetting

P.set_module_list(
    [
        ModuleMain,
        ModuleMigration,
        ModuleDatabaseMigration,
        ModuleGDriveScan,
        ModuleSql,
        ModulePlugins,
        ModuleSetting,
        ModuleFont,
        ModuleManual,
    ]
)
logger = P.logger
