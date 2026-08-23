# BookOasis 연결 설정과 안전한 연결·무결성 검사를 제공합니다.
import time
import traceback

from flask import jsonify, render_template

from .bookoasis_env import EnvFileError, read_env_file, save_env_file
from .bookoasis_compose import ComposeFileError, list_compose_files, read_compose_file, save_compose_file
from .docker_manager import DockerManagerError
from .dependency_installer import DependencyInstaller, DependencyInstallerError
from .release_info import REPOSITORY_URL, read_local_version, release_status
from .setup import *


class ModuleSetting(PluginModuleBase):
    def __init__(self, plugin):
        super().__init__(plugin, name="setting")
        self.installer = DependencyInstaller(P.logger)

    @property
    def service(self):
        return P.bookoasis_mate_service

    def process_menu(self, page, req):
        P.logger.debug("[BookOasisMate] 설정 메뉴 열기")
        arg = P.ModelSetting.to_dict()
        settings = self.service.settings()
        arg["mate_version"] = read_local_version()
        arg["mate_repository_url"] = REPOSITORY_URL
        arg["dependency_status"] = self.installer.status()
        for key in (
            "bookoasis_root_path",
            "bookoasis_docker_path",
            "bookoasis_compose_file",
            "bookoasis_override_file",
            "general_db_path",
            "adult_db_path",
            "audiobook_db_path",
            "video_db_path",
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
            if command == "release_status":
                return jsonify({"ret": "success", "msg": "", "data": release_status()})
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
                    }), 400
                job = self.installer.start(req.form.get("key"))
                return jsonify({
                    "ret": "success",
                    "msg": "MariaDB 필수 구성요소 설치를 시작했습니다.",
                    "data": job,
                })
            if command == "env_load":
                data = read_env_file(self.service.settings().get("bookoasis_root_path"))
                return jsonify({
                    "ret": "success",
                    "msg": (
                        "BookOasis .env 파일을 불러왔습니다."
                        if data["exists"]
                        else "BookOasis .env 파일이 없어 새 파일로 편집합니다."
                    ),
                    "data": data,
                })
            if command == "env_save":
                if req.form.get("confirm_save") != "true":
                    return jsonify({"ret": "warning", "msg": "저장 확인이 필요합니다."}), 400
                data = save_env_file(
                    self.service.settings().get("bookoasis_root_path"),
                    req.form.get("env_content", ""),
                )
                return jsonify({
                    "ret": "success",
                    "msg": "BookOasis .env 파일을 저장했습니다. BookOasis를 직접 재시작해 주세요.",
                    "data": data,
                })
            if command == "docker_status":
                return jsonify({
                    "ret": "success",
                    "msg": "",
                    "data": self.service.docker_status(),
                })
            if command == "docker_action_start":
                data = self.service.start_docker_action(
                    req.form.get("docker_action"),
                    req.form.get("confirm_action"),
                )
                return jsonify({
                    "ret": "success",
                    "msg": "BookOasis Docker 작업을 시작했습니다.",
                    "data": data,
                })
            if command == "docker_action_status":
                return jsonify({
                    "ret": "success",
                    "msg": "",
                    "data": self.service.docker_action_status(),
                })
            if command == "compose_options":
                current = self.service.settings()
                docker_root = current.get("bookoasis_docker_path") or current.get("bookoasis_root_path")
                data = list_compose_files(docker_root)
                data["compose_selected"] = str(current.get("bookoasis_compose_file") or "auto")
                data["override_selected"] = str(current.get("bookoasis_override_file") or "auto")
                return jsonify({"ret": "success", "msg": "", "data": data})
            if command == "compose_load":
                selection = self.service.docker_compose_editor_selection(req.form.get("compose_kind"))
                data = read_compose_file(
                    selection["root"],
                    selection["kind"],
                    selected=selection["selected"],
                )
                return jsonify({
                    "ret": "success",
                    "msg": (
                        "Compose 파일을 불러왔습니다."
                        if data["exists"]
                        else (
                            "Compose Override 파일이 없어 새 파일로 편집합니다."
                            if data.get("can_create")
                            else "기본 Compose 파일을 찾지 못했습니다."
                        )
                    ),
                    "data": data,
                })
            if command == "compose_save":
                if req.form.get("confirm_save") != "true":
                    return jsonify({"ret": "warning", "msg": "저장 확인이 필요합니다."}), 400
                selection = self.service.docker_compose_editor_selection(req.form.get("compose_kind"))
                data = save_compose_file(
                    selection["root"],
                    selection["kind"],
                    req.form.get("compose_content", ""),
                    selected=selection["selected"],
                )
                return jsonify({
                    "ret": "success",
                    "msg": "Compose 파일을 저장했습니다. 변경 적용은 BookOasis Docker 재생성 또는 재시작 시 반영됩니다.",
                    "data": data,
                })
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
        except (EnvFileError, ComposeFileError, DockerManagerError, DependencyInstallerError) as error:
            return jsonify({"ret": "warning", "msg": str(error)}), 400
        except Exception as error:
            P.logger.error(f"BookOasis Mate 설정 요청 오류: {error}")
            P.logger.error(traceback.format_exc())
            return jsonify({"ret": "danger", "msg": "요청 처리에 실패했습니다. 플러그인 로그를 확인해 주세요."}), 500
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            P.logger.debug(f"[BookOasisMate] 설정 AJAX 종료 command={safe_command} duration_ms={duration_ms}")
