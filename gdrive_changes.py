# FF rclone 인증을 재사용해 Google Drive Changes API 변경을 Mate 이벤트로 변환합니다.
import json
import posixpath
import shlex
import subprocess
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DRIVE_API = "https://www.googleapis.com/drive/v3"
RCLONE_INSPECT_MAX_OUTPUT = 512 * 1024
GOOGLE_DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveApiError(RuntimeError):
    PERMANENT_REASONS = {
        "accessNotConfigured",
        "appNotAuthorizedToFile",
        "domainPolicy",
        "forbidden",
        "insufficientFilePermissions",
        "insufficientPermissions",
    }

    def __init__(self, status_code, reason, message):
        self.status_code = int(status_code or 0)
        self.reason = str(reason or "unknown")
        self.permanent = self.status_code == 403 and self.reason in self.PERMANENT_REASONS
        super().__init__(
            f"Google Drive API {self.status_code} ({self.reason}): "
            f"{str(message or '요청이 거부되었습니다.')}"
        )


def resolve_ff_rclone_settings(plugin_manager, rclone_path=None, config_path=None):
    direct_binary = str(rclone_path or "").strip()
    direct_config = str(config_path or "").strip()
    if direct_binary or direct_config:
        if not direct_binary or not direct_config:
            raise ValueError("Mate rclone 실행 파일과 설정 파일 경로를 함께 입력해 주세요.")
        return direct_binary, direct_config
    plugin = plugin_manager.get_plugin_instance("rclone") if plugin_manager else None
    model = getattr(plugin, "ModelSetting", None)
    if model is None:
        raise RuntimeError("FF rclone 플러그인 설정을 찾을 수 없습니다.")
    binary = str(model.get("rclone_path") or "rclone").strip()
    config = str(model.get("rclone_config_path") or "").strip()
    if not config:
        raise RuntimeError("FF rclone 설정 파일 경로가 비어 있습니다.")
    return binary, config


def _token_is_expired(token):
    expiry = str((token or {}).get("expiry") or "").strip()
    if not expiry:
        return False
    try:
        value = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)


def _rclone_config_dump(rclone_path, config_path, timeout=30, runner=None):
    runner = runner or subprocess.run
    command = [rclone_path, "--config", config_path, "config", "dump"]
    result = runner(
        command,
        capture_output=True,
        text=True,
        timeout=max(5, min(int(timeout or 30), 300)),
        check=True,
    )
    return json.loads(result.stdout or "{}")


def rclone_version_output(rclone_path, timeout=30, runner=None):
    binary = str(rclone_path or "").strip()
    if not binary:
        raise ValueError("확인할 rclone 실행 파일 경로를 입력해 주세요.")
    runner = runner or subprocess.run
    result = runner(
        [binary, "version"],
        capture_output=True,
        text=True,
        timeout=max(5, min(int(timeout or 30), 300)),
        check=True,
    )
    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value and value.strip()
    )
    if not output:
        raise RuntimeError("rclone 버전 출력이 비어 있습니다.")
    if len(output) > RCLONE_INSPECT_MAX_OUTPUT:
        raise RuntimeError("rclone 확인 결과가 너무 큽니다.")
    return output


def rclone_config_output(rclone_path, config_path, timeout=30, runner=None):
    payload = _rclone_config_dump(
        rclone_path, config_path, timeout=timeout, runner=runner
    )
    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(output) > RCLONE_INSPECT_MAX_OUTPUT:
        raise RuntimeError("rclone 확인 결과가 너무 큽니다.")
    return output


def list_google_drive_remotes(rclone_path, config_path, timeout=30, runner=None):
    payload = _rclone_config_dump(rclone_path, config_path, timeout, runner)
    return sorted(
        str(name)
        for name, config in payload.items()
        if isinstance(config, dict) and str(config.get("type") or "").lower() == "drive"
    )


def list_google_drive_sources(rclone_path, config_path, timeout=30, runner=None):
    payload = _rclone_config_dump(rclone_path, config_path, timeout, runner)
    drives = {
        str(name)
        for name, config in payload.items()
        if isinstance(config, dict) and str(config.get("type") or "").lower() == "drive"
    }
    sources = [
        {"name": name, "type": "drive", "upstreams": [name]}
        for name in sorted(drives)
    ]
    for name, config in sorted(payload.items()):
        if not isinstance(config, dict) or str(config.get("type") or "").lower() != "union":
            continue
        upstreams = []
        for token in shlex.split(str(config.get("upstreams") or "")):
            parts = token.rsplit(":", 1)
            if len(parts) == 2 and parts[1].lower() in {"ro", "nc", "writeback", "rw"}:
                token = parts[0]
            remote = token.split(":", 1)[0].strip()
            if remote in drives and remote not in upstreams:
                upstreams.append(remote)
        if upstreams:
            sources.append({"name": str(name), "type": "union", "upstreams": upstreams})
    return sources


def resolve_google_drive_source(sources, remote, source_remote=None):
    remote = str(remote or "").strip().rstrip(":")
    source_remote = str(source_remote or remote).strip().rstrip(":")
    source = next((item for item in sources if item.get("name") == remote), None)
    if source is None:
        raise ValueError(f"rclone 리모트 {remote or '-'}를 설정 파일에서 찾을 수 없습니다.")
    upstreams = source.get("upstreams") or []
    if source_remote not in upstreams:
        raise ValueError(f"{remote}에서 감시할 Google Drive upstream을 선택해 주세요.")
    return source_remote


def google_drive_state_remote(remote, source_remote=None):
    remote = str(remote or "").strip().rstrip(":")
    source_remote = str(source_remote or remote).strip().rstrip(":")
    return remote if remote == source_remote else f"{remote}->{source_remote}"


def parse_builtin_roots(value, legacy=None, limit=10):
    if isinstance(value, str):
        text = value.strip()
        rows = json.loads(text) if text else []
    else:
        rows = value or []
    if not isinstance(rows, list):
        raise ValueError("자체 변경 감지 설정은 목록 형식이어야 합니다.")
    if not rows and legacy and any(str(item or "").strip() for item in legacy.values()):
        rows = [legacy]
    if len(rows) > limit:
        raise ValueError(f"자체 변경 감지 경로는 최대 {limit}개까지 설정할 수 있습니다.")

    normalized = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"자체 변경 감지 {index}번 설정 형식이 올바르지 않습니다.")
        item = {
            "remote": str(row.get("remote") or "").strip().rstrip(":"),
            "source_remote": str(row.get("source_remote") or row.get("remote") or "").strip().rstrip(":"),
            "root_id": str(row.get("root_id") or "").strip(),
            "remote_path": str(row.get("remote_path") or "").strip().rstrip("/"),
            "local_root": str(row.get("local_root") or "").strip().replace("\\", "/").rstrip("/"),
        }
        if not item["remote"] or not item["root_id"] or not item["local_root"]:
            raise ValueError(f"자체 변경 감지 {index}번의 리모트, 폴더 ID와 BookOasis 경로를 입력해 주세요.")
        if item["remote_path"].startswith("/"):
            raise ValueError(f"자체 변경 감지 {index}번 rclone 경로는 리모트 내부 상대 경로여야 합니다.")
        if not item["local_root"].startswith("/"):
            raise ValueError(f"자체 변경 감지 {index}번 BookOasis 경로는 절대 경로여야 합니다.")
        normalized.append(item)

    for index, left in enumerate(normalized):
        for right in normalized[index + 1 :]:
            remote_overlap = left["remote"] == right["remote"] and (
                not left["remote_path"]
                or not right["remote_path"]
                or left["remote_path"] == right["remote_path"]
                or left["remote_path"].startswith(right["remote_path"] + "/")
                or right["remote_path"].startswith(left["remote_path"] + "/")
            )
            local_overlap = (
                left["local_root"] == right["local_root"]
                or left["local_root"].startswith(right["local_root"] + "/")
                or right["local_root"].startswith(left["local_root"] + "/")
            )
            if remote_overlap or local_overlap:
                raise ValueError("자체 변경 감지 경로가 서로 중첩됩니다. 각 파일이 한 설정에만 포함되게 입력해 주세요.")
    return normalized


def build_change_event(previous, current):
    previous = previous or {}
    current = current or {}
    old_path = str(previous.get("path") or "")
    new_path = str(current.get("path") or "")
    item_type = "directory" if bool(current.get("is_directory", previous.get("is_directory"))) else "file"
    if current.get("trashed") or (old_path and not new_path):
        return {"action": "delete", "item_type": item_type, "path": old_path, "removed_path": old_path}
    if not old_path:
        return {"action": "create", "item_type": item_type, "path": new_path, "removed_path": ""}
    if old_path != new_path:
        return {"action": "rename", "item_type": item_type, "path": new_path, "removed_path": old_path}
    return {"action": "edit", "item_type": item_type, "path": new_path, "removed_path": ""}


class GoogleDriveChangesClient:
    def __init__(
        self, rclone_path, config_path, remote, root_id, remote_path, local_root,
        timeout=30, api_timeout=None, runner=None, opener=None, source_remote=None,
    ):
        self.rclone_path = rclone_path
        self.config_path = config_path
        self.remote = str(remote or "").strip().rstrip(":")
        self.source_remote = str(source_remote or self.remote).strip().rstrip(":")
        self.state_remote = google_drive_state_remote(self.remote, self.source_remote)
        self.root_id = str(root_id or "").strip()
        self.remote_path = str(remote_path or "").strip().strip("/")
        self.local_root = str(local_root or "").strip().rstrip("/")
        self.item_scope = f"{self.state_remote}:{self.root_id}"
        self.timeout = max(5, min(int(timeout or 30), 300))
        self.api_timeout = max(5, min(int(api_timeout or self.timeout), 300))
        self.runner = runner or subprocess.run
        self.opener = opener or urlopen
        self._credentials = None
        if not self.remote or not self.source_remote or not self.root_id or not self.local_root:
            raise ValueError("자체 변경 감지의 rclone 리모트, 감시 폴더 ID와 로컬 루트를 입력해 주세요.")

    def _run_json(self, *args):
        command = [self.rclone_path, "--config", self.config_path, *args]
        result = self.runner(command, capture_output=True, text=True, timeout=self.timeout, check=True)
        return json.loads(result.stdout or "{}")

    def _access(self):
        if self._credentials is not None:
            return self._credentials
        self._run_json("about", f"{self.source_remote}:", "--json")
        config = self._run_json("config", "dump")
        remote = config.get(self.source_remote) or {}
        if str(remote.get("type") or "").lower() != "drive":
            raise RuntimeError("선택한 rclone 리모트는 Google Drive 형식이 아닙니다.")
        try:
            token = json.loads(remote.get("token") or "{}")
        except (TypeError, ValueError):
            token = {}
        access_token = str(token.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("rclone Google Drive 액세스 토큰을 읽을 수 없습니다.")
        if _token_is_expired(token):
            raise RuntimeError(
                "rclone이 갱신된 토큰을 설정 파일에 저장하지 못했습니다. "
                "rclone.conf와 부모 디렉터리의 쓰기·rename 권한 및 디렉터리 마운트를 확인해 주세요."
            )
        self._credentials = (access_token, str(remote.get("team_drive") or "").strip())
        return self._credentials

    def _get(self, path, params=None):
        for attempt in range(2):
            token, drive_id = self._access()
            query = dict(params or {})
            if path.startswith("changes") or path.startswith("files/"):
                query.setdefault("supportsAllDrives", "true")
            if drive_id and path.startswith("changes"):
                query["driveId"] = drive_id
            url = f"{DRIVE_API}/{path}"
            if query:
                url += "?" + urlencode(query)
            request = Request(
                url,
                headers={"Authorization": f"Bearer {token}", "User-Agent": "BookOasisMate/1.0"},
            )
            try:
                with self.opener(request, timeout=self.api_timeout) as response:
                    return json.loads(response.read().decode("utf-8") or "{}")
            except HTTPError as error:
                try:
                    body = error.read()
                finally:
                    error.close()
                try:
                    payload = json.loads(body.decode("utf-8") or "{}")
                except (AttributeError, UnicodeDecodeError, ValueError):
                    payload = {}
                detail = payload.get("error") if isinstance(payload, dict) else {}
                detail = detail if isinstance(detail, dict) else {}
                errors = detail.get("errors") or []
                first = errors[0] if errors and isinstance(errors[0], dict) else {}
                api_error = GoogleDriveApiError(
                    error.code,
                    first.get("reason") or detail.get("status") or "unknown",
                    detail.get("message") or error.reason,
                )
                if api_error.status_code == 401 and attempt == 0:
                    self._credentials = None
                    continue
                raise api_error from None
            except TimeoutError:
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise
            except URLError as error:
                if isinstance(error.reason, TimeoutError) and attempt == 0:
                    time.sleep(1)
                    continue
                raise

    def start_page_token(self):
        return str(self._get("changes/startPageToken").get("startPageToken") or "")

    def list_changes(self, page_token):
        token = str(page_token or "").strip()
        while token:
            payload = self._get(
                "changes",
                {
                    "pageToken": token,
                    "pageSize": 1000,
                    "includeRemoved": "true",
                    "includeItemsFromAllDrives": "true",
                    "fields": "nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,mimeType,parents,trashed,modifiedTime))",
                },
            )
            for change in payload.get("changes") or []:
                yield change, ""
            next_token = str(payload.get("nextPageToken") or "")
            if not next_token:
                yield None, str(payload.get("newStartPageToken") or token)
                return
            token = next_token

    def file(self, file_id):
        return self._get(
            f"files/{file_id}",
            {"fields": "id,name,mimeType,parents,trashed,modifiedTime", "supportsAllDrives": "true"},
        )

    def validate_root(self):
        root = self.file(self.root_id)
        if str(root.get("mimeType") or "") != GOOGLE_DRIVE_FOLDER_MIME:
            raise ValueError("감시 폴더 ID가 Google Drive 폴더가 아닙니다.")
        return root

    def resolve_item(self, file_data, item_model):
        file_data = file_data or {}
        file_id = str(file_data.get("id") or "")
        name = str(file_data.get("name") or "")
        parents = file_data.get("parents") or []
        parent_id = str(parents[0]) if parents else ""
        if parent_id == self.root_id:
            parent_path = self.local_root
        else:
            parent = item_model.get(self.item_scope, parent_id) if parent_id else None
            if parent is None and parent_id:
                parent_data = self.file(parent_id)
                parent = self.resolve_item(parent_data, item_model)
                if parent.get("path"):
                    item_model.upsert(self.item_scope, parent)
            parent_path = str((parent or {}).get("path") or "")
        path = posixpath.join(parent_path, name) if parent_path and name else ""
        return {
            "file_id": file_id,
            "parent_id": parent_id,
            "path": path,
            "mime_type": str(file_data.get("mimeType") or ""),
            "is_directory": str(file_data.get("mimeType") or "") == "application/vnd.google-apps.folder",
            "trashed": bool(file_data.get("trashed")),
        }

class GoogleDriveChangesWatcher:
    def __init__(self, client, state_model, item_model, event_model, extensions, buffer_seconds=60):
        self.client = client
        self.state_remote = getattr(client, "state_remote", client.remote)
        self.state_model = state_model
        self.item_model = item_model
        self.event_model = event_model
        self.extensions = extensions
        self.buffer_seconds = buffer_seconds

    def reset(self):
        self.state_model.reset(self.state_remote, self.client.root_id)
        self.item_model.clear_remote(self.client.item_scope)

    def poll_once(self):
        try:
            state = self.state_model.get(self.state_remote, self.client.root_id) or {}
            if state.get("status") == "blocked":
                return 0
            page_token = str(state.get("page_token") or "")
            if not page_token:
                page_token = self.client.start_page_token()
                if not page_token:
                    raise RuntimeError("Google Drive 시작 페이지 토큰을 받지 못했습니다.")
                self.state_model.save_cursor(
                    self.state_remote, self.client.root_id, page_token, status="ready"
                )
                return 0
            accepted = 0
            final_token = page_token
            try:
                from .gdrive_scan import validate_event
            except ImportError:
                from gdrive_scan import validate_event
            for change, token in self.client.list_changes(page_token):
                if change is None:
                    final_token = token or final_token
                    continue
                file_id = str(change.get("fileId") or "")
                previous = self.item_model.get(self.client.item_scope, file_id)
                removed = bool(change.get("removed"))
                current = None if removed else self.client.resolve_item(change.get("file") or {}, self.item_model)
                if current is not None and current.get("trashed"):
                    removed = True
                event = build_change_event(previous, None if removed else current)
                if event.get("path"):
                    validated = validate_event(
                        event["action"], event["item_type"], event["path"], event.get("removed_path"), self.extensions
                    )
                    if validated["relevant"]:
                        self.event_model.enqueue(validated, buffer_seconds=self.buffer_seconds)
                        accepted += 1
                old_path = str((previous or {}).get("path") or "")
                new_path = str((current or {}).get("path") or "")
                if previous and previous.get("is_directory") and old_path and new_path and old_path != new_path:
                    self.item_model.move_prefix(self.client.item_scope, old_path, new_path)
                if removed or not new_path:
                    if previous and previous.get("is_directory"):
                        self.item_model.delete_prefix(self.client.item_scope, old_path)
                    else:
                        self.item_model.delete(self.client.item_scope, file_id)
                elif current and current.get("path"):
                    self.item_model.upsert(self.client.item_scope, current)
            self.state_model.save_cursor(
                self.state_remote, self.client.root_id, final_token, status="ready"
            )
            return accepted
        except Exception as error:
            if getattr(error, "permanent", False):
                current = self.state_model.get(self.state_remote, self.client.root_id) or {}
                self.state_model.save_cursor(
                    self.state_remote,
                    self.client.root_id,
                    current.get("page_token", ""),
                    status="blocked",
                    error=str(error),
                )
            else:
                self.state_model.set_error(self.state_remote, self.client.root_id, str(error))
            raise
