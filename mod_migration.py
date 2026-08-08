# BookOasis 카테고리 이관 설정, 상태와 패키지 검사 요청을 처리합니다.
import time
import traceback

from flask import jsonify, render_template

from .setup import *


class ModuleMigration(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(
            plugin,
            name="migration",
            first_menu="setting",
        )
        self.db_default = {
            "migration_work_dir": "",
            "migration_operation": "export",
            "migration_export_db_type": "general",
            "migration_export_library_ids": "",
            "migration_import_package": "",
            "migration_import_db_type": "auto",
            "migration_import_mode": "new",
            "migration_merge_library_id": "",
            "migration_import_name": "",
            "migration_import_target_paths": "",
            "migration_backup_before_import": "True",
        }

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        page = page if page in {"setting", "status", "manual"} else "setting"
        P.logger.debug(f"[BookOasisMate] 카테고리 이관 메뉴 열기 page={page}")
        arg = P.ModelSetting.to_dict()
        arg["database_engine"] = self.service.database_engine_info()
        arg["page"] = page
        return render_template(
            f"{P.package_name}_{self.name}_{page}.html",
            arg=arg,
        )

    def process_ajax(self, command, req):
        started = time.monotonic()
        safe_command = str(command or "").replace("\r", " ").replace("\n", " ")[:80]
        P.logger.debug(
            f"[BookOasisMate] 카테고리 이관 AJAX 시작 command={safe_command}"
        )
        try:
            if command == "libraries":
                data = self.service.migration_libraries(
                    req.form.get("db_type", "general")
                )
                return jsonify({"ret": "success", "data": data})
            if command == "packages":
                data = self.service.migration_packages(
                    work_dir=req.form.get("work_dir")
                )
                return jsonify({"ret": "success", "data": data})
            if command == "inspect":
                data = self.service.inspect_migration_package(
                    package_path=req.form.get("package_path"),
                    work_dir=req.form.get("work_dir"),
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "카테고리 패키지 검사를 완료했습니다.",
                        "data": data,
                    }
                )
            if command == "start":
                data = self.service.start_migration()
                return jsonify(
                    {
                        "ret": "success" if data.get("started") else "warning",
                        "msg": data.get("message"),
                        "data": data,
                    }
                )
            if command == "status":
                return jsonify(
                    {
                        "ret": "success",
                        "data": self.service.migration_status(),
                    }
                )
            if command == "stop":
                data = self.service.stop_migration()
                return jsonify(
                    {
                        "ret": "success" if data.get("requested") else "warning",
                        "msg": data.get("message"),
                        "data": data,
                    }
                )
            return jsonify(
                {"ret": "warning", "msg": "지원하지 않는 요청입니다."}
            ), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate 카테고리 이관 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify(
                {
                    "ret": "danger",
                    "msg": str(error) or "카테고리 이관 요청에 실패했습니다.",
                }
            ), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(
                f"[BookOasisMate] 카테고리 이관 AJAX 종료 "
                f"command={safe_command} duration_ms={duration_ms}"
            )
