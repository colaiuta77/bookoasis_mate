# gd-poller 변경 이벤트를 BookOasis 보관함 스캔과 rclone VFS 갱신으로 변환합니다.
import json
import posixpath
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

try:
    from .bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError
except ImportError:
    from bookoasis_db import BookOasisDatabaseAdapter, BookOasisDatabaseError


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
    ".mp4",
    ".mkv",
    ".avi",
    ".webm",
    ".mov",
    ".m4v",
    ".ts",
    ".smi",
    ".srt",
    ".vtt",
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


def find_repeated_leading_prefix(path):
    """선두에서 두 번 반복된 디렉터리 접두사를 진단용으로 찾습니다."""
    normalized = normalize_event_path(path)
    parts = [part for part in normalized.split("/") if part]
    for size in range(2, (len(parts) // 2) + 1):
        if parts[:size] == parts[size : size * 2]:
            return "/" + "/".join(parts[:size])
    return ""


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

    if action in {"move", "rename"}:
        if previous:
            operations.append(("forget", previous, item_type))
    elif action == "delete":
        target = previous or current
        if target:
            operations.append(("forget", target, item_type))
    result = []
    seen = set()
    for operation in operations:
        key = operation[:2]
        if operation[1] and key not in seen:
            seen.add(key)
            result.append(operation)
    return result


def read_bookoasis_libraries(db_type, settings, optional=False):
    adapter = BookOasisDatabaseAdapter(settings)
    path = settings.get(f"{db_type}_db_path")
    if adapter.engine == "sqlite" and not str(path or "").strip():
        return []
    target = adapter.target(db_type, db_type, path)
    try:
        connection = adapter.connect(target)
    except BookOasisDatabaseError:
        if optional:
            return []
        raise
    with closing(connection):
        if "libraries" not in connection.tables():
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


def event_scan_targets(event, libraries):
    """변경 이벤트를 보관함 기준 스캔 디렉터리로 변환합니다."""
    action = event["action"]
    item_type = event["item_type"]
    current = event.get("mapped_path") or event.get("path") or ""
    previous = event.get("mapped_removed_path") or event.get("removed_path") or ""
    candidates = []

    if action in {"move", "rename"}:
        if previous:
            candidates.append((previous, True))
        if current:
            candidates.append((current, False))
    elif action == "delete":
        target = previous or current
        if target:
            candidates.append((target, True))
    elif current:
        candidates.append((current, False))

    targets = []
    seen = set()
    for path, removed in candidates:
        library = find_library(path, libraries)
        if library is None:
            continue
        directory = (
            posixpath.dirname(path) or "/"
            if item_type == "file" or removed
            else path
        )
        root = library["root"].rstrip("/") or "/"
        if directory == root:
            relative_path = ""
        elif root == "/":
            relative_path = directory.lstrip("/")
        elif directory.startswith(f"{root}/"):
            relative_path = directory[len(root) :].lstrip("/")
        else:
            continue
        key = (library["db_type"], library["id"], relative_path)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"library": library, "path": relative_path})
    return targets


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
        path_scan_callback=None,
        rc_client=None,
        logger=None,
    ):
        self.settings = dict(settings or {})
        self.scan_callback = scan_callback
        self.path_scan_callback = path_scan_callback
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
        libraries = read_bookoasis_libraries("general", self.settings)
        if str(self.settings.get("adult_enabled")).lower() in {"1", "true", "yes", "on"}:
            libraries.extend(
                read_bookoasis_libraries("adult", self.settings)
            )
        libraries.extend(
            read_bookoasis_libraries("audiobook", self.settings, optional=True)
        )
        libraries.extend(
            read_bookoasis_libraries("video", self.settings, optional=True)
        )
        return sorted(libraries, key=lambda item: len(item["root"]), reverse=True)

    def _log(self, level, message):
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(message)

    @staticmethod
    def _duplicate_prefix_error(path, prefix):
        return (
            f"변경 경로에 중복 접두사 '{prefix}'가 감지되었습니다: {path}. "
            "gd-poller target에 이미 로컬 경로가 포함되어 있으면 "
            "CommandDispatcher mappings를 제거해 주세요. 기존 실패 이벤트는 "
            "손상된 수신 경로를 유지하므로 삭제한 뒤 새 이벤트를 받아야 합니다."
        )

    def preview_path(self, path):
        received_path = normalize_event_path(path)
        mapped_path = map_path(received_path, self.path_mappings)
        library = find_library(mapped_path, self.libraries)
        vfs_rule = find_vfs_rule(mapped_path, self.vfs_rules)
        repeated_prefix = find_repeated_leading_prefix(mapped_path)
        warning = ""
        if repeated_prefix:
            warning = self._duplicate_prefix_error(mapped_path, repeated_prefix)
        elif library is None:
            warning = "변환 경로에 해당하는 BookOasis 보관함을 찾을 수 없습니다."
        elif vfs_rule is None:
            warning = "변환 경로에 일치하는 rclone VFS 규칙이 없습니다."
        return {
            "received_path": received_path,
            "mapped_path": mapped_path,
            "mapping_applied": received_path != mapped_path,
            "repeated_prefix": repeated_prefix,
            "library": {
                "db_type": library["db_type"],
                "id": library["id"],
                "name": library["name"],
                "root": library["root"],
            }
            if library
            else None,
            "vfs_rule": {
                "local": vfs_rule["local"],
                "remote": vfs_rule["remote"],
            }
            if vfs_rule
            else None,
            "warning": warning,
        }

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
            for candidate in candidates:
                repeated_prefix = find_repeated_leading_prefix(candidate)
                if repeated_prefix:
                    raise ValueError(
                        self._duplicate_prefix_error(candidate, repeated_prefix)
                    )
            raise ValueError("변경 경로에 해당하는 BookOasis 보관함을 찾을 수 없습니다.")
        item["scan_targets"] = event_scan_targets(item, self.libraries)
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
        for event in prepared:
            event_id = int(event["id"])
            for operation in event_vfs_operations(event):
                operations.append((event_id,) + operation)

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

        fallback_libraries = {
            (target["library"]["db_type"], target["library"]["id"])
            for event in prepared
            for target in event.get("scan_targets", [])
            if not self.path_scan_callback or not target["path"]
        }
        paths_by_library = {}
        for event in prepared:
            for target in event.get("scan_targets", []):
                library = target["library"]
                library_key = (library["db_type"], library["id"])
                if library_key in fallback_libraries or not target["path"]:
                    continue
                paths_by_library.setdefault(library_key, set()).add(target["path"])

        path_aliases = {}
        for library_key, paths in paths_by_library.items():
            parents = []
            for relative_path in sorted(
                paths,
                key=lambda value: (value.count("/"), len(value), value),
            ):
                parent = next(
                    (
                        candidate
                        for candidate in parents
                        if relative_path == candidate
                        or relative_path.startswith(f"{candidate.rstrip('/')}/")
                    ),
                    None,
                )
                if parent is None:
                    parent = relative_path
                    parents.append(relative_path)
                path_aliases[library_key + (relative_path,)] = parent

        scan_requests = {}
        event_scan_keys = {}
        for event in prepared:
            event_id = int(event["id"])
            for target in event.get("scan_targets", []):
                library = target["library"]
                library_key = (library["db_type"], library["id"])
                if library_key in fallback_libraries:
                    request_key = ("library",) + library_key + ("",)
                else:
                    relative_path = path_aliases.get(
                        library_key + (target["path"],),
                        target["path"],
                    )
                    request_key = ("path",) + library_key + (relative_path,)
                scan_requests.setdefault(
                    request_key,
                    {"library": library, "event_ids": set()},
                )["event_ids"].add(event_id)
                event_scan_keys.setdefault(event_id, [])
                if request_key not in event_scan_keys[event_id]:
                    event_scan_keys[event_id].append(request_key)

        scan_results = {}
        for request_key, request_data in scan_requests.items():
            mode, db_type, library_id, relative_path = request_key
            library = request_data["library"]
            related_event_ids = request_data["event_ids"]
            vfs_failed = any(
                not operation_result.get("success")
                for event_id in related_event_ids
                for operation_result in operation_results.get(event_id, [])
            )
            if vfs_failed:
                scan_results[request_key] = {
                    "success": False,
                    "message": "VFS 갱신에 실패하여 BookOasis 스캔 요청을 보류했습니다.",
                    "response": {},
                    "mode": mode,
                    "path": relative_path,
                }
                continue
            try:
                if mode == "path":
                    response = self.path_scan_callback(
                        db_type,
                        library_id,
                        library["name"],
                        relative_path,
                    )
                else:
                    response = self.scan_callback(
                        db_type,
                        library_id,
                        library["name"],
                    )
                success = bool(response.get("success"))
                message = response.get("message") or response.get("error") or ""
            except Exception as error:
                success = False
                message = str(error)
                response = {}
            scan_results[request_key] = {
                "success": success,
                "message": message,
                "response": response,
                "mode": response.get("mode") or mode,
                "path": relative_path,
            }

        for event in prepared:
            event_id = int(event["id"])
            event_scan_results = [
                scan_results[key]
                for key in event_scan_keys.get(event_id, [])
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
