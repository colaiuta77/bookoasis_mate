# BookOasis Docker Compose 파일을 안전하게 읽고 백업·저장합니다.
import os
import shutil
import stat
import tempfile
from datetime import datetime
from pathlib import Path

COMPOSE_MAX_BYTES = 1024 * 1024
COMPOSE_BACKUP_KEEP = 5
BASE_COMPOSE_NAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.build.yml",
    "docker-compose.build.yaml",
    "docker-compose.ghcr.yml",
    "docker-compose.ghcr.yaml",
    "docker-compose.mariadb.yml",
    "docker-compose.mariadb.yaml",
    "docker-compose.mariadb.ghcr.yml",
    "docker-compose.mariadb.ghcr.yaml",
)
OVERRIDE_COMPOSE_NAMES = (
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.mariadb.yml",
    "docker-compose.override.mariadb.yaml",
)
COMPOSE_NAMES = {
    "compose": BASE_COMPOSE_NAMES,
    "override": OVERRIDE_COMPOSE_NAMES,
}


class ComposeFileError(ValueError):
    pass


def _resolve_root(root_value, host_mount="/host"):
    raw_root = str(root_value or "").strip()
    if not raw_root:
        raise ComposeFileError("BookOasis Docker 경로를 먼저 설정해 주세요.")
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise ComposeFileError("BookOasis Docker 경로는 절대 경로여야 합니다.")

    resolved_root = None
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        mount = Path(host_mount)
        try:
            if mount.is_dir():
                candidate = mount / root.relative_to(Path("/"))
                resolved_root = candidate.resolve(strict=True)
        except (OSError, ValueError):
            resolved_root = None
    if resolved_root is None:
        raise ComposeFileError("BookOasis Docker 경로를 찾을 수 없습니다.")
    if not resolved_root.is_dir():
        raise ComposeFileError("BookOasis Docker 경로가 디렉터리가 아닙니다.")
    if resolved_root.parent == resolved_root:
        raise ComposeFileError("파일시스템 루트는 BookOasis Docker 경로로 사용할 수 없습니다.")
    return resolved_root


def _kind_names(kind):
    key = str(kind or "").strip().lower()
    names = COMPOSE_NAMES.get(key)
    if names is None:
        raise ComposeFileError("Compose 파일 종류가 올바르지 않습니다.")
    return key, names


def _normalize_selection(kind, selected):
    key, names = _kind_names(kind)
    value = str(selected or "auto").strip()
    if not value or value.lower() == "auto":
        return key, names, "auto"
    if key == "override" and value.lower() == "none":
        return key, names, "none"
    if value not in names:
        raise ComposeFileError(f"지원하지 않는 Compose 파일입니다: {value}")
    return key, names, value


def _scan_existing(root, names):
    found = []
    for name in names:
        candidate = root / name
        if candidate.is_symlink():
            raise ComposeFileError(f"보안을 위해 심볼릭 링크 {name} 파일은 편집할 수 없습니다.")
        if candidate.exists():
            if not candidate.is_file():
                raise ComposeFileError(f"{name} 경로가 일반 파일이 아닙니다.")
            found.append(candidate)
    return found


def list_compose_files(root_value):
    root = _resolve_root(root_value)
    base_existing = [item.name for item in _scan_existing(root, BASE_COMPOSE_NAMES)]
    override_existing = [item.name for item in _scan_existing(root, OVERRIDE_COMPOSE_NAMES)]
    return {
        "root": str(root),
        "base_candidates": list(BASE_COMPOSE_NAMES),
        "override_candidates": list(OVERRIDE_COMPOSE_NAMES),
        "base_existing": base_existing,
        "override_existing": override_existing,
    }


def _select_path(root, kind, selected=None):
    key, names, selection = _normalize_selection(kind, selected)
    found = _scan_existing(root, names)
    if selection == "none":
        return None, False, selection
    if selection != "auto":
        target = root / selection
        exists = target in found
        return target, exists, selection
    if len(found) > 1:
        raise ComposeFileError(
            "Compose 파일이 여러 개 있습니다. 설정에서 사용할 파일을 직접 선택해 주세요."
        )
    if found:
        return found[0], True, found[0].name
    if key == "override":
        return root / names[0], False, names[0]
    return None, False, "auto"


def _secure_backup_copy(source, destination):
    source = Path(source)
    destination = Path(destination)
    try:
        if os.name == "nt":
            shutil.copy2(source, destination)
            return
        descriptor = os.open(
            str(destination),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output_file:
                descriptor = None
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.chmod(destination, 0o600)
    except OSError as error:
        try:
            destination.unlink()
        except OSError:
            pass
        raise ComposeFileError("Compose 백업 파일을 안전하게 생성하지 못했습니다.") from error


def _prune_backups(root, target_name, keep=COMPOSE_BACKUP_KEEP):
    keep = max(1, int(keep or COMPOSE_BACKUP_KEEP))
    backups = []
    for candidate in Path(root).glob(f"{target_name}.bookoasis_mate_*.bak"):
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            info = candidate.stat()
        except OSError:
            continue
        backups.append((info.st_mtime_ns, candidate.name, candidate))
    backups.sort(reverse=True)
    for unused_mtime, unused_name, candidate in backups[keep:]:
        try:
            candidate.unlink()
        except OSError:
            pass


def read_compose_file(root_value, kind, max_bytes=COMPOSE_MAX_BYTES, selected=None):
    root = _resolve_root(root_value)
    key, names = _kind_names(kind)
    target, exists, selection = _select_path(root, key, selected=selected)
    if not exists:
        return {
            "kind": key,
            "exists": False,
            "path": str(target) if target is not None else "",
            "content": "",
            "size": 0,
            "modified_at": "",
            "candidates": list(names),
            "selected": selection,
            "can_create": key == "override" and target is not None,
        }

    file_size = target.stat().st_size
    if file_size > max_bytes:
        raise ComposeFileError("Compose 파일이 허용 크기 1MB를 초과했습니다.")
    try:
        content = target.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise ComposeFileError("Compose 파일은 UTF-8 인코딩이어야 합니다.") from error
    return {
        "kind": key,
        "exists": True,
        "path": str(target),
        "content": content,
        "size": file_size,
        "modified_at": datetime.fromtimestamp(target.stat().st_mtime).isoformat(timespec="seconds"),
        "candidates": list(names),
        "selected": selection,
        "can_create": key == "override",
    }


def save_compose_file(root_value, kind, content, max_bytes=COMPOSE_MAX_BYTES, selected=None):
    root = _resolve_root(root_value)
    key, unused_names = _kind_names(kind)
    target, exists, selection = _select_path(root, key, selected=selected)
    if target is None:
        raise ComposeFileError("Compose Override를 사용 안 함으로 선택한 상태에서는 파일을 저장할 수 없습니다.")
    if key == "compose" and not exists:
        raise ComposeFileError(
            "선택한 기본 Compose 파일을 찾을 수 없습니다. Docker 경로와 파일 선택을 확인해 주세요."
        )
    if not isinstance(content, str):
        raise ComposeFileError("저장할 Compose 내용이 올바르지 않습니다.")
    if "\x00" in content:
        raise ComposeFileError("Compose 내용에 NUL 문자를 포함할 수 없습니다.")
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ComposeFileError("Compose 파일은 1MB 이하만 저장할 수 있습니다.")

    backup_path = None
    original_stat = None
    if exists:
        original_stat = target.stat()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = root / f"{target.name}.bookoasis_mate_{stamp}.bak"
        _secure_backup_copy(target, backup_path)

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=root,
            prefix=f".{target.name}.bookoasis_mate_",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(
            temp_path,
            stat.S_IMODE(original_stat.st_mode) if original_stat is not None else 0o644,
        )
        if original_stat is not None and hasattr(os, "chown"):
            try:
                os.chown(temp_path, original_stat.st_uid, original_stat.st_gid)
            except PermissionError as error:
                raise ComposeFileError("기존 Compose 파일 소유권을 유지할 권한이 없습니다.") from error
        os.replace(temp_path, target)
        temp_path = None
        if backup_path is not None:
            _prune_backups(root, target.name)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass

    return {
        "kind": key,
        "path": str(target),
        "backup_path": str(backup_path) if backup_path else "",
        "size": len(encoded),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "created": not exists,
        "selected": selection,
    }
