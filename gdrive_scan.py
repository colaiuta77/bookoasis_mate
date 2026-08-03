# gd-poller 변경 이벤트를 BookOasis 보관함 스캔과 rclone VFS 갱신으로 변환합니다.
import json
import posixpath
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


SUPPORTED_ACTIONS = {
    "create",
    "edit",
    "move",
    "rename",
    "restore",
    "delete",
}
SUPPORTED_ITEM_TYPES = {"file", "directory"}
DEFAULT_EXTENSIONS = (
    ".zip",
    ".cbz",
    ".epub",
    ".pdf",
    ".txt",
    ".yaml",
    ".xml",
    ".json",
    ".mp3",
    ".m4b",
    ".m4a",
    ".flac",
    ".aac",
    ".wav",
    ".ogg",
    ".opus",
    ".wma",
)


def normalize_event_path(value):
    path = str(value or "").strip().replace("\\", "/")
    if not path:
        return ""
    if "\x00" in path or len(path) > 4096:
        raise ValueError("이벤트 경로가 올바르지 않습니다.")
    if not path.startswith("/"):
        raise ValueError("이벤트 경로는 절대 경로여야 합니다.")
    parts = [part for part in path.split("/") if part]
    if ".." in parts:
        raise ValueError("이벤트 경로에 상위 경로 이동을 사용할 수 없습니다.")
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized


def parse_extensions(value):
    if isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = str(value or "").replace("\r", "\n").replace(",", "\n").splitlines()
    result = []
    for item in values:
        extension = str(item or "").strip().lower()
        if not extension:
            continue
        if not extension.startswith("."):
            extension = f".{extension}"
        if extension not in result:
            result.append(extension)
    return tuple(result or DEFAULT_EXTENSIONS)


def parse_path_mappings(value):
    mappings = []
    for line in str(value or "").replace("\r", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            raise ValueError(f"경로 매핑 형식이 올바르지 않습니다: {line}")
        source, target = [part.strip() for part in line.split("=>", 1)]
        source = normalize_event_path(source)
        target = normalize_event_path(target)
        mappings.append((source.rstrip("/") or "/", target.rstrip("/") or "/"))
    return sorted(mappings, key=lambda item: len(item[0]), reverse=True)


def map_path(path, mappings):
    normalized = normalize_event_path(path)
    if not normalized:
        return ""
    for source, target in mappings:
        if normalized == source or normalized.startswith(f"{source.rstrip('/')}/"):
            suffix = normalized[len(source) :]
            return normalize_event_path(f"{target.rstrip('/')}{suffix}")
    return normalized


def parse_vfs_rules(value):
    rules = []
    for line in str(value or "").replace("\r", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 3:
            raise ValueError("VFS 규칙 형식이 올바르지 않습니다.")
        local = normalize_event_path(parts[0]).rstrip("/") or "/"
        remote = normalize_event_path(parts[1]).rstrip("/") if parts[1] else ""
        endpoint, separator, vfs = parts[2].partition("#")
        endpoint = endpoint.strip()
        if not endpoint:
            raise ValueError("VFS RC 주소가 비어 있습니다.")
        if "://" not in endpoint:
            endpoint = f"http://{endpoint}"
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("VFS RC 주소가 올바르지 않습니다.")
        username = parts[3] if len(parts) >= 4 else ""
        password = parts[4] if len(parts) >= 5 else ""
        if parsed.username and not username:
            username = parsed.username
        if parsed.password and not password:
            password = parsed.password
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        endpoint = f"{parsed.scheme}://{host}{parsed.path.rstrip('/')}"
        vfs = vfs.strip()
        if separator and vfs and not vfs.endswith(":"):
            vfs = f"{vfs}:"
        rules.append(
            {
                "local": local,
                "remote": remote,
                "endpoint": endpoint,
                "vfs": vfs,
                "username": username,
                "password": password,
            }
        )
    return sorted(rules, key=lambda item: len(item["local"]), reverse=True)


def find_vfs_rule(path, rules):
    normalized = normalize_event_path(path)
    for rule in rules:
        local = rule["local"]
        if normalized == local or normalized.startswith(f"{local.rstrip('/')}/"):
            return rule
    return None


def to_remote_path(path, rule):
    normalized = normalize_event_path(path)
    local = rule["local"]
    suffix = normalized[len(local) :]
    remote = rule["remote"]
    if remote:
        return normalize_event_path(f"{remote.rstrip('/')}{suffix}")
    return normalize_event_path(suffix or "/")


def is_relevant_event(item_type, path, removed_path, extensions):
    if item_type == "directory":
        return True
    paths = [value for value in (path, removed_path) if value]
    return any(
        posixpath.basename(value).lower() == ".bookoasisignore"
        or posixpath.splitext(value)[1].lower() in extensions
        for value in paths
    )


def validate_event(action, item_type, path, removed_path="", extensions=None):
    action = str(action or "").strip().lower()
    item_type = str(item_type or "").strip().lower()
    if action not in SUPPORTED_ACTIONS:
        raise ValueError(f"지원하지 않는 action입니다: {action}")
    if item_type not in SUPPORTED_ITEM_TYPES:
        raise ValueError(f"지원하지 않는 파일 유형입니다: {item_type}")
    path = normalize_event_path(path)
    removed_path = normalize_event_path(removed_path) if removed_path else ""
    extensions = parse_extensions(extensions)
    return {
        "action": action,
        "item_type": item_type,
        "path": path,
        "removed_path": removed_path,
        "relevant": is_relevant_event(item_type, path, removed_path, extensions),
    }


def event_vfs_operations(event):
    action = event["action"]
    item_type = event["item_type"]
    current = event.get("mapped_path") or event.get("path") or ""
    previous = event.get("mapped_removed_path") or event.get("removed_path") or ""
    operations = []

    def parent_or_self(path):
        if not path:
            return ""
        return path if item_type == "directory" else posixpath.dirname(path) or "/"

    if action in {"move", "rename"}:
        if previous:
            operations.append(("forget", previous, item_type))
            operations.append(("refresh", parent_or_self(previous), "directory"))
        if current:
            operations.append(("refresh", parent_or_self(current), "directory"))
    elif action == "delete":
        target = previous or current
        if target:
            operations.append(("forget", target, item_type))
            operations.append(("refresh", parent_or_self(target), "directory"))
    else:
        if current:
            operations.append(("refresh", parent_or_self(current), "directory"))
    result = []
    seen = set()
    for operation in operations:
        key = operation[:2]
        if operation[1] and key not in seen:
            seen.add(key)
            result.append(operation)
    return result


def read_bookoasis_libraries(db_type, db_path):
    path = Path(str(db_path or "")).expanduser()
    if not path.is_file():
        return []
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='libraries'"
        ).fetchone()
        if not table:
            return []
        rows = connection.execute(
            "SELECT id, name, physical_path FROM libraries ORDER BY id"
        ).fetchall()
    libraries = []
    for row in rows:
        for root in str(row["physical_path"] or "").replace("\r", "").splitlines():
            root = root.strip()
            if not root:
                continue
            try:
                root = normalize_event_path(root)
            except ValueError:
                continue
            libraries.append(
                {
                    "db_type": db_type,
                    "id": int(row["id"]),
                    "name": str(row["name"] or f"보관함 {row['id']}"),
                    "root": root.rstrip("/") or "/",
                }
            )
    return sorted(libraries, key=lambda item: len(item["root"]), reverse=True)


def find_library(path, libraries):
    normalized = normalize_event_path(path)
    for library in libraries:
        root = library["root"]
        if normalized == root or normalized.startswith(f"{root.rstrip('/')}/"):
            return library
    return None


class RcloneRcClient:
    def __init__(self, timeout=30, opener=None):
        self.timeout = max(1, min(int(timeout or 30), 300))
        self._opener = opener or urlopen

    @staticmethod
    def _safe_error(error):
        if isinstance(error, HTTPError):
            return f"rclone RC HTTP 오류 {error.code}"
        if isinstance(error, URLError):
            return f"rclone RC 연결 실패: {error.reason}"
        return f"rclone RC 요청 실패: {error}"

    def _request(self, rule, command, payload):
        data = dict(payload)
        if rule.get("vfs"):
            data["fs"] = rule["vfs"]
        request = Request(
            f"{rule['endpoint'].rstrip('/')}/{command.lstrip('/')}",
            data=urlencode(data).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        if rule.get("username"):
            import base64

            credentials = f"{rule['username']}:{rule.get('password', '')}".encode("utf-8")
            request.add_header(
                "Authorization",
                f"Basic {base64.b64encode(credentials).decode('ascii')}",
            )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            return payload
        except (HTTPError, URLError, ValueError, OSError) as error:
            raise RuntimeError(self._safe_error(error)) from error

    def refresh(self, rule, local_path):
        remote_path = to_remote_path(local_path, rule)
        return self._request(
            rule,
            "vfs/refresh",
            {"dir": remote_path, "recursive": "false"},
        )

    def forget(self, rule, local_path, item_type):
        remote_path = to_remote_path(local_path, rule)
        key = "file" if item_type == "file" else "dir"
        return self._request(rule, "vfs/forget", {key: remote_path})


class GDriveScanProcessor:
    def __init__(
        self,
        settings,
        scan_callback,
        rc_client=None,
        logger=None,
    ):
        self.settings = dict(settings or {})
        self.scan_callback = scan_callback
        self.rc_client = rc_client or RcloneRcClient(
            self.settings.get("gdrive_scan_rc_timeout", 30)
        )
        self.logger = logger
        self.path_mappings = parse_path_mappings(
            self.settings.get("gdrive_scan_path_mappings", "")
        )
        self.vfs_rules = parse_vfs_rules(
            self.settings.get("gdrive_scan_vfs_rules", "")
        )
        self.libraries = self._load_libraries()

    def _load_libraries(self):
        libraries = read_bookoasis_libraries(
            "general", self.settings.get("general_db_path")
        )
        if str(self.settings.get("adult_enabled")).lower() in {"1", "true", "yes", "on"}:
            libraries.extend(
                read_bookoasis_libraries(
                    "adult", self.settings.get("adult_db_path")
                )
            )
        libraries.extend(
            read_bookoasis_libraries(
                "audiobook", self.settings.get("audiobook_db_path")
            )
        )
        return sorted(libraries, key=lambda item: len(item["root"]), reverse=True)

    def _log(self, level, message):
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message)

    def prepare_event(self, event):
        item = dict(event)
        item["mapped_path"] = map_path(item.get("path"), self.path_mappings)
        item["mapped_removed_path"] = map_path(
            item.get("removed_path"), self.path_mappings
        )
        candidates = [
            value
            for value in (item["mapped_path"], item["mapped_removed_path"])
            if value
        ]
        matched = [find_library(value, self.libraries) for value in candidates]
        item["libraries"] = []
        seen = set()
        for library in matched:
            if library and (library["db_type"], library["id"]) not in seen:
                seen.add((library["db_type"], library["id"]))
                item["libraries"].append(library)
        if not item["libraries"]:
            raise ValueError("변경 경로에 해당하는 BookOasis 보관함을 찾을 수 없습니다.")
        return item

    def process_batch(self, events):
        prepared = []
        results = {}
        for event in events:
            event_id = int(event["id"])
            try:
                prepared.append(self.prepare_event(event))
            except Exception as error:
                results[event_id] = {
                    "success": False,
                    "message": str(error),
                }

        operations = []
        libraries = {}
        event_ids_by_library = {}
        for event in prepared:
            event_id = int(event["id"])
            for operation in event_vfs_operations(event):
                operations.append((event_id,) + operation)
            for library in event["libraries"]:
                key = (library["db_type"], library["id"])
                libraries[key] = library
                event_ids_by_library.setdefault(key, set()).add(event_id)

        operation_results = {}
        operation_cache = {}
        for event_id, operation, path, item_type in operations:
            key = (operation, path, item_type)
            if key not in operation_cache:
                rule = find_vfs_rule(path, self.vfs_rules)
                if rule is None:
                    operation_cache[key] = {
                        "success": False,
                        "message": f"변경 경로에 일치하는 VFS 규칙이 없습니다: {path}",
                    }
                else:
                    try:
                        if operation == "forget":
                            response = self.rc_client.forget(rule, path, item_type)
                        else:
                            response = self.rc_client.refresh(rule, path)
                        operation_cache[key] = {
                            "success": True,
                            "response": response,
                        }
                    except Exception as error:
                        operation_cache[key] = {
                            "success": False,
                            "message": str(error),
                        }
            operation_results.setdefault(event_id, []).append(operation_cache[key])

        scan_results = {}
        for key, library in libraries.items():
            related_event_ids = event_ids_by_library.get(key, set())
            vfs_failed = any(
                not operation_result.get("success")
                for event_id in related_event_ids
                for operation_result in operation_results.get(event_id, [])
            )
            if vfs_failed:
                scan_results[key] = {
                    "success": False,
                    "message": "VFS 갱신에 실패하여 BookOasis 스캔 요청을 보류했습니다.",
                    "response": {},
                }
                continue
            try:
                response = self.scan_callback(
                    library["db_type"], library["id"], library["name"]
                )
                success = bool(response.get("success"))
                message = response.get("message") or response.get("error") or ""
            except Exception as error:
                success = False
                message = str(error)
                response = {}
            scan_results[key] = {
                "success": success,
                "message": message,
                "response": response,
            }

        for event in prepared:
            event_id = int(event["id"])
            event_scan_results = [
                scan_results[(library["db_type"], library["id"])]
                for library in event["libraries"]
            ]
            event_operation_results = operation_results.get(event_id, [])
            failed_operations = [
                item for item in event_operation_results if not item.get("success")
            ]
            failed_scans = [item for item in event_scan_results if not item.get("success")]
            success = not failed_operations and not failed_scans
            messages = [
                item.get("message")
                for item in failed_operations + failed_scans
                if item.get("message")
            ]
            results[event_id] = {
                "success": success,
                "message": "; ".join(messages) if messages else "변경 이벤트를 처리했습니다.",
                "mapped_path": event.get("mapped_path"),
                "libraries": event["libraries"],
                "vfs": event_operation_results,
                "scans": event_scan_results,
            }
        return results
