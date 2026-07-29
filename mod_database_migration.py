# Kavita와 BookOasis 원본의 통DB 이관 설정·상태·실행 요청을 처리합니다.
import time
import traceback

from flask import jsonify, render_template

from .setup import *


class ModuleDatabaseMigration(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(
            plugin,
            name="database_migration",
            first_menu="setting",
        )
        self.db_default = {
            "database_migration_source": "kavita",
            "kavita_db_path": "",
            "kavita_cover_path": "",
            "kavita_target_db_type": "general",
            "kavita_selected_libraries": "",
            "kavita_path_mappings": "",
            "kavita_user_mappings": "",
            "kavita_import_covers": "True",
            "kavita_import_progress": "False",
            "kavita_lock_metadata": "True",
            "kavita_backup_before_import": "True",
            "kavita_dry_run": "True",
            "bookoasis_db_package_path": "",
            "bookoasis_cover_package_path": "",
            "bookoasis_package_action": "import",
            "bookoasis_export_name": "dist_books",
            "bookoasis_package_path_mappings": "",
            "bookoasis_backup_before_import": "True",
            "bookoasis_package_dry_run": "True",
        }

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        page = page if page in {"setting", "status", "manual"} else "setting"
        P.logger.debug(f"[BookOasisMate] 통DB 이관 메뉴 열기 page={page}")
        arg = P.ModelSetting.to_dict()
        arg["page"] = page
        return render_template(
            f"{P.package_name}_{self.name}_{page}.html",
            arg=arg,
        )

    def process_ajax(self, command, req):
        started = time.monotonic()
        safe_command = str(command or "").replace("\r", " ").replace("\n", " ")[:80]
        P.logger.debug(
            f"[BookOasisMate] 통DB 이관 AJAX 시작 command={safe_command}"
        )
        try:
            if command == "packages":
                data = self.service.database_migration_packages(req.form)
                return jsonify({"ret": "success", "data": data})
            if command == "inspect_database":
                data = self.service.inspect_bookoasis_database_package(req.form)
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "공유 DB 패키지 검사를 완료했습니다.",
                        "data": data,
                    }
                )
            if command == "inspect":
                data = self.service.inspect_database_migration(req.form)
                return jsonify(
                    {
                        "ret": "success",
                        "msg": "통DB 이관 미리보기를 완료했습니다.",
                        "data": data,
                    }
                )
            if command == "start":
                data = self.service.start_database_migration(req.form)
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
                        "data": self.service.database_migration_status(),
                    }
                )
            if command == "stop":
                data = self.service.stop_database_migration()
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
            P.logger.error(f"BookOasis Mate 통DB 이관 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify(
                {
                    "ret": "danger",
                    "msg": str(error) or "통DB 이관 요청에 실패했습니다.",
                }
            ), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(
                f"[BookOasisMate] 통DB 이관 AJAX 종료 "
                f"command={safe_command} duration_ms={duration_ms}"
            )
