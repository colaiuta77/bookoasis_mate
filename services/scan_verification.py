# BookOasis 도서 경로의 DB 반영 여부를 읽기 전용으로 확인합니다.
import posixpath
from contextlib import ExitStack, closing
from datetime import datetime, timedelta

from .bookoasis_db import BookOasisDatabaseAdapter
from .gdrive_scan import find_library, normalize_event_path


CHECK_TIMEOUT = 600


def start_verification(event, result, now=None):
    now = now or datetime.now()
    unsupported = {"state": "unsupported", "message": "수정·폴더·보조 파일·오디오/비디오는 경로만으로 반영을 판정할 수 없습니다."}
    action = event.get("action")
    if event.get("item_type") != "file" or action not in {"create", "restore", "delete", "move", "rename"}:
        return unsupported
    current = result.get("mapped_path") or ""
    previous = result.get("mapped_removed_path") or ""
    paths = [(previous or current, False)] if action == "delete" else [(current, True)]
    if action in {"move", "rename"}:
        if not previous or previous == current:
            return dict(unsupported, message="이전 경로를 확인할 수 없어 이름 변경·이동 반영을 자동 판정하지 않습니다.")
        paths.append((previous, False))
    targets = []
    for path, present in paths:
        try:
            path = normalize_event_path(path)
        except ValueError:
            return dict(unsupported, message="DB 확인 대상 경로가 올바르지 않습니다.")
        libraries = result.get("verification_libraries") or result.get("libraries") or []
        library = find_library(path, sorted(libraries, key=lambda row: len(row["root"]), reverse=True))
        if not library or library.get("db_type") not in {"general", "adult"}:
            return dict(unsupported, message="확인 대상 경로에 해당하는 일반·성인 보관함이 없습니다.")
        if posixpath.splitext(path)[1].lower() not in {".zip", ".cbz", ".epub", ".pdf", ".txt"}:
            return unsupported
        targets.append({"db_type": library["db_type"], "library_id": library["id"], "path": path, "present": present})
    return {"state": "pending", "message": "요청 접수 · DB 반영 확인 대기", "targets": targets,
            "started_at": now.isoformat(timespec="seconds"),
            "deadline": (now + timedelta(seconds=CHECK_TIMEOUT)).isoformat(timespec="seconds")}


def check_verification(verification, settings, now=None):
    if verification.get("state") != "pending":
        return verification
    now = now or datetime.now()
    message = "새 경로 등록 또는 이전 경로 정리를 기다립니다."
    # 확인용 연결만 짧게 제한하며 스캔·사용자 DB 설정은 변경하지 않습니다.
    matched = True
    try:
        adapter = BookOasisDatabaseAdapter(dict(settings, mariadb_connect_timeout=3, mariadb_read_timeout=3))
        with ExitStack() as stack:
            connections = {}
            for target in verification["targets"]:
                db_type = target["db_type"]
                if db_type not in connections:
                    database = adapter.target(db_type, db_type, settings.get(db_type + "_db_path"))
                    connection = stack.enter_context(closing(adapter.connect(database, readonly=True)))
                    if adapter.engine == "sqlite":
                        connection.execute("PRAGMA busy_timeout = 1000")
                    columns = connection.columns("books")
                    if not {"library_id", "file_path", "is_deleted"}.issubset(columns):
                        return dict(verification, state="unsupported", message="도서 DB 스키마가 경로·삭제 상태 확인을 지원하지 않습니다.", checked_at=now.isoformat(timespec="seconds"))
                    connections[db_type] = connection
                rows = connections[db_type].execute(
                    "SELECT file_path FROM books WHERE library_id = ? AND file_path = ? AND COALESCE(is_deleted, 0) = 0",
                    (target["library_id"], target["path"]),
                ).fetchall()
                # MariaDB/SQLite의 대소문자 무시 비교를 그대로 완료 판정에 쓰지 않습니다.
                present = any(row["file_path"] == target["path"] for row in rows)
                if present != target["present"]:
                    matched = False
    except Exception:
        matched = False
        message = "DB 조회 실패로 반영 여부를 확인하지 못했습니다. DB 연결·권한을 확인하세요."
    expired = now >= datetime.fromisoformat(verification["deadline"])
    state = "confirmed" if matched else "timeout" if expired else "pending"
    if matched:
        message = "DB 경로 반영 확인. 메타데이터·표지 갱신이나 전체 스캔 완료를 뜻하지 않습니다."
    elif expired:
        message = "확인 시간 초과. 스캔 실패로 단정하지 않습니다. " + message
    return dict(verification, state=state, message=message, checked_at=now.isoformat(timespec="seconds"))
