# BookOasis Mate의 읽기 전용 SQL 화면과 AJAX 실행 요청을 처리합니다.
import hashlib
import time
import traceback

from flask import jsonify, render_template

from .setup import *
from .sql_query import ReadOnlySqlTool


class ModuleSql(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="sql")

    def _tool(self):
        settings = P.bookoasis_mate_service.settings()
        paths = {"general": settings.get("general_db_path")}
        if settings.get("adult_enabled"):
            paths["adult"] = settings.get("adult_db_path")
        paths["audiobook"] = settings.get("audiobook_db_path")
        return ReadOnlySqlTool(paths)

    def process_menu(self, page, req):
        del page, req
        P.logger.debug("[BookOasisMate] 읽기 전용 SQL 도구 메뉴 열기")
        arg = P.ModelSetting.to_dict()
        arg["adult_enabled"] = P.bookoasis_mate_service.settings().get(
            "adult_enabled", False
        )
        arg["audiobook_enabled"] = bool(
            P.bookoasis_mate_service.settings().get("audiobook_db_path")
        )
        return render_template(f"{P.package_name}_{self.name}.html", arg=arg)

    def process_ajax(self, command, req):
        started = time.monotonic()
        try:
            tool = self._tool()
            if command == "presets":
                return jsonify({"ret": "success", "data": tool.presets()})
            if command == "execute":
                query = req.form.get("query", "")
                query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
                P.logger.info(
                    "[BookOasisMate] 읽기 전용 SQL 실행 "
                    f"db_type={req.form.get('db_type', 'general')} query_hash={query_hash}"
                )
                data = tool.execute(
                    req.form.get("db_type", "general"),
                    query,
                    max_rows=req.form.get("max_rows", 200),
                    timeout_seconds=req.form.get("timeout_seconds", 3),
                )
                return jsonify(
                    {
                        "ret": "success",
                        "msg": f"{data['row_count']}개 행을 조회했습니다.",
                        "data": data,
                    }
                )
            return jsonify({"ret": "warning", "msg": "지원하지 않는 요청입니다."}), 400
        except (ValueError, FileNotFoundError, TimeoutError) as error:
            return jsonify({"ret": "warning", "msg": str(error)}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate SQL 도구 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify(
                {
                    "ret": "danger",
                    "msg": "SQL 조회에 실패했습니다. 플러그인 로그를 확인해 주세요.",
                }
            ), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(
                f"[BookOasisMate] SQL 도구 AJAX 종료 command={command} duration_ms={duration_ms}"
            )
