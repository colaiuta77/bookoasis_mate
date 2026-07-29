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
