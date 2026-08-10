# BookOasis 설치 루트의 .env 파일을 안전하게 읽고 백업·저장합니다.
import os
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path


ENV_MAX_BYTES = 1024 * 1024


class EnvFileError(ValueError):
    pass


def _resolve_env_path(root_value):
    raw_root = str(root_value or "").strip()
    if not raw_root:
        raise EnvFileError("BookOasis 기본 경로를 먼저 설정해 주세요.")
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise EnvFileError("BookOasis 기본 경로는 절대 경로여야 합니다.")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise EnvFileError("BookOasis 기본 경로를 찾을 수 없습니다.") from error
    if not resolved_root.is_dir():
        raise EnvFileError("BookOasis 기본 경로가 디렉터리가 아닙니다.")
    if resolved_root.parent == resolved_root:
        raise EnvFileError("파일시스템 루트는 BookOasis 기본 경로로 사용할 수 없습니다.")
    env_path = resolved_root / ".env"
    if env_path.is_symlink():
        raise EnvFileError("보안을 위해 심볼릭 링크 .env 파일은 편집할 수 없습니다.")
    if env_path.exists() and not env_path.is_file():
        raise EnvFileError("BookOasis .env 경로가 일반 파일이 아닙니다.")
    return resolved_root, env_path


def read_env_file(root_value, max_bytes=ENV_MAX_BYTES):
    _, env_path = _resolve_env_path(root_value)
    if not env_path.exists():
        return {
            "exists": False,
            "path": str(env_path),
            "content": "",
            "size": 0,
            "modified_at": "",
        }
    file_size = env_path.stat().st_size
    if file_size > max_bytes:
        raise EnvFileError(".env 파일이 허용 크기 1MB를 초과했습니다.")
    try:
        content = env_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise EnvFileError(".env 파일은 UTF-8 인코딩이어야 합니다.") from error
    modified_at = datetime.fromtimestamp(env_path.stat().st_mtime).isoformat(timespec="seconds")
    return {
        "exists": True,
        "path": str(env_path),
        "content": content,
        "size": file_size,
        "modified_at": modified_at,
    }


def save_env_file(root_value, content, max_bytes=ENV_MAX_BYTES):
    root, env_path = _resolve_env_path(root_value)
    if not isinstance(content, str):
        raise EnvFileError("저장할 .env 내용이 올바르지 않습니다.")
    if "\x00" in content:
        raise EnvFileError(".env 내용에 NUL 문자를 포함할 수 없습니다.")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise EnvFileError(".env 파일은 1MB 이하만 저장할 수 있습니다.")

    backup_path = None
    original_stat = None
    if env_path.exists():
        original_stat = env_path.stat()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = root / f".env.bookoasis_mate_{stamp}.bak"
        shutil.copy2(env_path, backup_path)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=root,
            prefix=".env.bookoasis_mate_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(
            temp_path,
            stat.S_IMODE(original_stat.st_mode) if original_stat is not None else 0o600,
        )
        if original_stat is not None and hasattr(os, "chown"):
            try:
                os.chown(temp_path, original_stat.st_uid, original_stat.st_gid)
            except PermissionError as error:
                raise EnvFileError("기존 .env 파일 소유권을 유지할 권한이 없습니다.") from error
        os.replace(temp_path, env_path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass

    return {
        "path": str(env_path),
        "backup_path": str(backup_path) if backup_path else "",
        "size": len(encoded),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
