# BookOasis 주요 로그를 허용 목록과 증분 커서로 안전하게 읽습니다.
import re
from datetime import datetime
from pathlib import Path


ALLOWED_LOG_FILES = {
    "lazy_scanner.log": "Lazy Scanner",
    "media_server.log": "Media Server",
    "media_server_worker.log": "Scanner Worker",
    "scanner.log": "Scanner",
}
DEFAULT_MAX_BYTES = 256 * 1024
DEFAULT_MAX_LINES = 500
MAX_ARCHIVE_FILES = 500
_LAZY_PROGRESS_PATTERN = re.compile(
    r"\((?P<done>\d+)/(?P<total>\d+)\).*?처리 시작\s*->\s*(?P<filename>.+?)\s*$"
)
_LAZY_DB_PATTERN = re.compile(r"DB=(?P<db_type>general|adult)\b")


def _log_root(log_dir):
    value = str(log_dir or "").strip()
    if not value:
        raise ValueError("BookOasis 로그 디렉터리가 설정되지 않았습니다.")
    root = Path(value).expanduser()
    if not root.exists() or not root.is_dir():
        raise ValueError("BookOasis 로그 디렉터리를 찾을 수 없습니다.")
    if root.is_symlink():
        raise ValueError("심볼릭 링크 로그 디렉터리는 사용할 수 없습니다.")
    return root.resolve()


def _safe_log_file(log_dir, filename):
    if filename not in ALLOWED_LOG_FILES:
        raise ValueError("허용되지 않은 BookOasis 로그 파일입니다.")
    root = _log_root(log_dir)
    candidate = root / filename
    if candidate.is_symlink():
        raise ValueError("심볼릭 링크 로그 파일은 읽을 수 없습니다.")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("로그 디렉터리 밖의 파일은 읽을 수 없습니다.") from error
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("선택한 BookOasis 로그 파일을 찾을 수 없습니다.")
    return root, resolved


def _identity(stat_result):
    return f"{stat_result.st_dev}:{stat_result.st_ino}"


def _modified_at(stat_result):
    return datetime.fromtimestamp(stat_result.st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def list_log_files(log_dir):
    root = _log_root(log_dir)
    files = []
    for filename, label in ALLOWED_LOG_FILES.items():
        candidate = root / filename
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        stat_result = resolved.stat()
        files.append({
            "name": filename,
            "label": label,
            "size": int(stat_result.st_size),
            "modified_at": _modified_at(stat_result),
        })
    return {
        "success": True,
        "files": files,
        "message": "" if files else "표시할 BookOasis 로그 파일이 없습니다.",
    }


def _safe_log_archive(log_dir, filename):
    name = str(filename or "").strip()
    if not name or Path(name).name != name or Path(name).suffix.lower() != ".zip":
        raise ValueError("삭제할 과거 ZIP 로그 파일명이 올바르지 않습니다.")
    root = _log_root(log_dir)
    candidate = root / name
    if candidate.is_symlink():
        raise ValueError("심볼릭 링크 ZIP 로그 파일은 삭제할 수 없습니다.")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("로그 디렉터리 밖의 파일은 삭제할 수 없습니다.") from error
    if not resolved.exists() or not resolved.is_file():
        raise ValueError("삭제할 과거 ZIP 로그 파일을 찾을 수 없습니다.")
    return root, resolved


def list_log_archives(log_dir, limit=MAX_ARCHIVE_FILES):
    root = _log_root(log_dir)
    limit = max(1, min(int(limit or MAX_ARCHIVE_FILES), MAX_ARCHIVE_FILES))
    files = []
    for candidate in root.iterdir():
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.suffix.lower() != ".zip"
        ):
            continue
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        stat_result = resolved.stat()
        files.append({
            "name": candidate.name,
            "size": int(stat_result.st_size),
            "modified_at": _modified_at(stat_result),
            "modified_timestamp": float(stat_result.st_mtime),
        })
    files.sort(key=lambda item: (-item["modified_timestamp"], item["name"].lower()))
    total = len(files)
    visible = files[:limit]
    for item in visible:
        item.pop("modified_timestamp", None)
    return {
        "success": True,
        "files": visible,
        "total": total,
        "truncated": total > len(visible),
        "message": "" if visible else "삭제할 과거 ZIP 로그 파일이 없습니다.",
    }


def delete_log_archive(log_dir, filename):
    _, path = _safe_log_archive(log_dir, filename)
    stat_result = path.stat()
    name = path.name
    size = int(stat_result.st_size)
    path.unlink()
    return {
        "success": True,
        "name": name,
        "size": size,
        "message": f"과거 ZIP 로그 {name} 파일을 삭제했습니다.",
    }


def read_log_tail(log_dir, filename, cursor_identity="", cursor_offset=None, max_bytes=DEFAULT_MAX_BYTES, max_lines=DEFAULT_MAX_LINES):
    _, path = _safe_log_file(log_dir, filename)
    max_bytes = max(1024, min(int(max_bytes or DEFAULT_MAX_BYTES), DEFAULT_MAX_BYTES))
    max_lines = max(10, min(int(max_lines or DEFAULT_MAX_LINES), 2000))
    stat_result = path.stat()
    identity = _identity(stat_result)
    size = int(stat_result.st_size)
    try:
        requested_offset = int(cursor_offset)
    except (TypeError, ValueError):
        requested_offset = None

    incremental = (
        str(cursor_identity or "") == identity
        and requested_offset is not None
        and 0 <= requested_offset <= size
    )
    reset = bool(cursor_identity) and not incremental
    start = requested_offset if incremental else max(0, size - max_bytes)
    truncated = size - start > max_bytes
    if truncated:
        start = max(0, size - max_bytes)
        incremental = False
        reset = bool(cursor_identity)

    with path.open("rb") as handle:
        handle.seek(start)
        if start > 0 and not incremental:
            handle.readline()
        payload = handle.read(max_bytes)
        offset = handle.tell()

    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    return {
        "success": True,
        "file": filename,
        "label": ALLOWED_LOG_FILES[filename],
        "text": "".join(lines),
        "identity": identity,
        "offset": int(offset),
        "size": size,
        "modified_at": _modified_at(stat_result),
        "reset": reset,
        "truncated": truncated,
    }


def read_lazy_progress(log_dir):
    try:
        _, path = _safe_log_file(log_dir, "lazy_scanner.log")
    except (ValueError, OSError):
        return None

    stat_result = path.stat()
    size = int(stat_result.st_size)
    with path.open("rb") as handle:
        handle.seek(max(0, size - DEFAULT_MAX_BYTES))
        if handle.tell() > 0:
            handle.readline()
        text = handle.read(DEFAULT_MAX_BYTES).decode("utf-8", errors="replace")

    lines = text.splitlines()
    progress = None
    db_type = None
    for line in reversed(lines):
        if progress is None:
            match = _LAZY_PROGRESS_PATTERN.search(line)
            if match:
                done = int(match.group("done"))
                total = int(match.group("total"))
                progress = {
                    "done": done,
                    "total": total,
                    "percent": round(done * 100 / total, 1) if total else 0,
                    "filename": match.group("filename").strip(),
                }
        if db_type is None:
            match = _LAZY_DB_PATTERN.search(line)
            if match:
                db_type = match.group("db_type")
        if progress is not None and db_type is not None:
            break
    if progress is None:
        return None
    progress["db_type"] = db_type
    progress["modified_at"] = _modified_at(stat_result)
    progress["modified_timestamp"] = float(stat_result.st_mtime)
    return progress
