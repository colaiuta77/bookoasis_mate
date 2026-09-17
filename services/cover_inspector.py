# 마운트된 BookOasis 표지 파일을 점검하고 고아 파일을 안전하게 정리합니다.
import os
import struct
import time
from pathlib import Path, PurePosixPath


IMAGE_EXTENSIONS = {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}


def normalize_cover_path(value):
    text = str(value or "").split("?", 1)[0].replace("\\", "/").strip().lstrip("/")
    if text.lower().startswith("covers/"):
        text = text[7:]
    return str(PurePosixPath(text)) if text else ""


def resolve_cover_path(root, relative_path):
    if not str(root or "").strip():
        return None
    root_path = Path(root or "").expanduser()
    normalized = normalize_cover_path(relative_path)
    if not normalized or not root_path.is_dir():
        return None
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    resolved_root = root_path.resolve()
    candidate = resolved_root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        return None
    return candidate


def _jpeg_dimensions(data):
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return "jpeg", width, height
        offset += length
    return None


def image_info(path):
    with Path(path).open("rb") as handle:
        data = handle.read(1024 * 1024)
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", *struct.unpack(">II", data[16:24])
    if len(data) >= 10 and data[:6] in {b"GIF87a", b"GIF89a"}:
        width, height = struct.unpack("<HH", data[6:10])
        return "gif", width, height
    jpeg = _jpeg_dimensions(data)
    if jpeg:
        return jpeg
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return "webp", width, height
        if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return "webp", 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
        marker = data.find(b"\x9d\x01\x2a", 20, 40)
        if marker >= 0 and marker + 7 <= len(data):
            width = int.from_bytes(data[marker + 3:marker + 5], "little") & 0x3FFF
            height = int.from_bytes(data[marker + 5:marker + 7], "little") & 0x3FFF
            return "webp", width, height
    return None


def inspect_cover_file(
    root,
    relative_path,
    min_width=200,
    min_height=280,
    min_file_size=0,
    min_aspect_ratio=0,
):
    normalized = normalize_cover_path(relative_path)
    result = {
        "path": normalized,
        "status": "ok",
        "issues": [],
        "size": 0,
        "format": None,
        "width": None,
        "height": None,
        "aspect_ratio": None,
    }
    if not normalized:
        result["status"] = "missing_reference"
        return result
    candidate = resolve_cover_path(root, normalized)
    if candidate is None:
        result["status"] = "invalid_path"
        return result
    try:
        if not candidate.is_file():
            result["status"] = "missing_file"
            return result
        result["size"] = candidate.stat().st_size
        if result["size"] <= 0:
            result["status"] = "empty_file"
            return result
        is_small_file = min_file_size > 0 and result["size"] < min_file_size
        info = image_info(candidate)
        if info is None:
            if is_small_file:
                result["issues"] = ["small_file"]
                result["status"] = "small_file"
            else:
                result["status"] = "invalid_image"
            return result
        result["format"], result["width"], result["height"] = info
        if result["width"] < min_width or result["height"] < min_height:
            result["issues"].append("low_resolution")
        if is_small_file:
            result["issues"].append("small_file")
        long_side = max(result["width"], result["height"])
        short_side = min(result["width"], result["height"])
        result["aspect_ratio"] = round(short_side / long_side, 4) if long_side > 0 else 0
        if min_aspect_ratio > 0 and result["aspect_ratio"] < min_aspect_ratio:
            result["issues"].append("abnormal_aspect_ratio")
        if result["issues"]:
            result["status"] = result["issues"][0]
        return result
    except OSError as error:
        result["status"] = "read_error"
        result["error"] = str(error)
        return result


def cleanup_orphan_files(
    root,
    referenced_paths,
    library_ids,
    dry_run=True,
    result_limit=500,
    should_stop=None,
    on_progress=None,
):
    if not str(root or "").strip():
        raise FileNotFoundError("표지 디렉터리가 설정되지 않았습니다.")
    root_path = Path(root or "").expanduser()
    if not root_path.is_dir():
        raise FileNotFoundError("표지 디렉터리를 찾을 수 없습니다.")

    resolved_root = root_path.resolve()
    selected_ids = sorted({
        int(library_id)
        for library_id in (library_ids or [])
        if str(library_id or "").strip().isdigit() and int(library_id) > 0
    })
    if not selected_ids:
        raise ValueError("정리할 보관함을 찾을 수 없습니다.")

    referenced = {
        normalize_cover_path(path).lower()
        for path in referenced_paths
        if normalize_cover_path(path)
    }
    result_limit = max(1, min(int(result_limit or 500), 5000))
    result = {
        "dry_run": bool(dry_run),
        "scanned_count": 0,
        "target_count": 0,
        "target_size": 0,
        "deleted_count": 0,
        "deleted_size": 0,
        "error_count": 0,
        "items": [],
        "truncated": False,
        "stopped": False,
    }
    last_report_count = 0
    last_report_at = 0.0

    def stopped():
        return bool(should_stop and should_stop())

    def report(force=False):
        nonlocal last_report_at, last_report_count
        if not on_progress:
            return
        now = time.monotonic()
        scanned_delta = result["scanned_count"] - last_report_count
        if not force and scanned_delta < 250 and now - last_report_at < 0.5:
            return
        on_progress(result)
        last_report_count = result["scanned_count"]
        last_report_at = now

    def append_item(item):
        if len(result["items"]) >= result_limit:
            result["items"].pop(0)
            result["truncated"] = True
        result["items"].append(item)

    for library_id in selected_ids:
        library_root = resolved_root / str(library_id)
        if not library_root.is_dir() or library_root.is_symlink():
            continue
        try:
            library_root.resolve().relative_to(resolved_root)
        except ValueError:
            continue

        for current_root, directories, filenames in os.walk(str(library_root), followlinks=False):
            current_path = Path(current_root)
            directories[:] = [
                name for name in directories
                if not (current_path / name).is_symlink()
            ]
            for filename in filenames:
                if stopped():
                    result["stopped"] = True
                    report(force=True)
                    return result
                candidate = current_path / filename
                if candidate.suffix.lower() not in IMAGE_EXTENSIONS or candidate.is_symlink():
                    continue
                try:
                    resolved = candidate.resolve()
                    resolved.relative_to(resolved_root)
                    relative = candidate.relative_to(resolved_root).as_posix()
                except (OSError, ValueError):
                    continue

                result["scanned_count"] += 1
                if relative.lower() in referenced:
                    report()
                    continue

                try:
                    size = candidate.stat().st_size
                    result["target_count"] += 1
                    result["target_size"] += size
                    status = "planned"
                    if not dry_run:
                        candidate.unlink()
                        status = "deleted"
                        result["deleted_count"] += 1
                        result["deleted_size"] += size
                    append_item({"path": relative, "size": size, "status": status})
                except OSError as error:
                    result["error_count"] += 1
                    append_item({
                        "path": relative,
                        "size": 0,
                        "status": "error",
                        "error": str(error),
                    })
                report()

    report(force=True)
    return result
