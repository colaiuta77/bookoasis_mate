# BookOasis 커스텀 폰트 파일을 검증하고 마운트 경로에 안전하게 저장합니다.
import os
import tempfile
from datetime import datetime
from pathlib import Path


class CustomFontManager:
    ALLOWED_EXTENSIONS = {".ttf", ".otf", ".woff", ".woff2"}
    SIGNATURES = {
        ".ttf": (b"\x00\x01\x00\x00", b"true", b"typ1"),
        ".otf": (b"OTTO",),
        ".woff": (b"wOFF",),
        ".woff2": (b"wOF2",),
    }
    MAX_FILE_SIZE = 30 * 1024 * 1024
    MAX_FILES = 20

    def __init__(self, root_path):
        self.root_path = str(root_path or "").strip()

    def _root(self):
        if not self.root_path:
            raise ValueError("커스텀 폰트 디렉터리를 먼저 설정해 주세요.")
        root = Path(self.root_path).expanduser()
        if not root.is_dir():
            raise FileNotFoundError("커스텀 폰트 디렉터리를 찾을 수 없습니다.")
        if not os.access(str(root), os.W_OK):
            raise PermissionError("커스텀 폰트 디렉터리에 쓰기 권한이 없습니다.")
        return root.resolve()

    @staticmethod
    def _safe_filename(filename):
        name = Path(str(filename or "")).name.strip()
        if not name or name in {".", ".."} or "\x00" in name or len(name) > 160:
            raise ValueError("폰트 파일명이 올바르지 않습니다.")
        if any(ord(character) < 32 for character in name):
            raise ValueError("폰트 파일명에 제어 문자를 사용할 수 없습니다.")
        return name

    @classmethod
    def _validate_signature(cls, extension, header):
        if not any(header.startswith(signature) for signature in cls.SIGNATURES[extension]):
            raise ValueError("확장자와 실제 폰트 파일 형식이 일치하지 않습니다.")

    def list_fonts(self):
        root = self._root()
        items = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in self.ALLOWED_EXTENSIONS:
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size": int(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
        return {"root": str(root), "items": items, "count": len(items)}

    def upload(self, files):
        root = self._root()
        candidates = [item for item in (files or []) if item and item.filename]
        if not candidates:
            raise ValueError("업로드할 폰트 파일을 선택해 주세요.")
        if len(candidates) > self.MAX_FILES:
            raise ValueError(f"한 번에 최대 {self.MAX_FILES}개 파일만 업로드할 수 있습니다.")

        uploaded = []
        rejected = []
        for storage in candidates:
            temp_path = None
            try:
                name = self._safe_filename(storage.filename)
                extension = Path(name).suffix.lower()
                if extension not in self.ALLOWED_EXTENSIONS:
                    raise ValueError("TTF, OTF, WOFF, WOFF2 파일만 업로드할 수 있습니다.")
                destination = (root / name).resolve()
                destination.relative_to(root)
                if destination.exists():
                    raise FileExistsError("같은 이름의 폰트 파일이 이미 있습니다.")

                with tempfile.NamedTemporaryFile(
                    prefix=".bookoasis-font-",
                    suffix=".tmp",
                    dir=str(root),
                    delete=False,
                ) as output:
                    temp_path = Path(output.name)
                    total = 0
                    header = b""
                    while True:
                        chunk = storage.stream.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.MAX_FILE_SIZE:
                            raise ValueError("폰트 파일은 30MB를 초과할 수 없습니다.")
                        if len(header) < 4:
                            header += chunk[: 4 - len(header)]
                        output.write(chunk)
                if total == 0:
                    raise ValueError("빈 폰트 파일은 업로드할 수 없습니다.")
                self._validate_signature(extension, header)
                os.replace(str(temp_path), str(destination))
                temp_path = None
                uploaded.append({"name": name, "size": total})
            except (OSError, ValueError) as error:
                rejected.append(
                    {
                        "name": str(getattr(storage, "filename", "") or ""),
                        "reason": str(error),
                    }
                )
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

        message = f"커스텀 폰트 {len(uploaded)}개를 업로드했습니다."
        if rejected:
            message += f" 거부 {len(rejected)}개는 결과에서 사유를 확인하세요."
        if not uploaded:
            message = f"업로드된 커스텀 폰트가 없습니다. 거부 {len(rejected)}개입니다."
        return {
            "root": str(root),
            "uploaded": uploaded,
            "rejected": rejected,
            "message": message,
        }
