# BookOasis 원격 설정과 Mate 로컬 설정의 호환 상태를 판정합니다.
def evaluate_integration(settings, local_engine, remote_engine, remote_cover):
    engine = str(remote_engine.get("engine") or "").lower()
    db_status = "unknown"
    if remote_engine.get("success") and engine in {"sqlite", "mariadb"}:
        db_status = "match" if engine == local_engine else "mismatch"
    db = {"status": db_status, "local": local_engine, "remote": engine,
          "message": {"unknown": "원격 DB 엔진 확인 불가. 웹훅 토큰·서버 연결을 확인하세요.", "match": "DB 엔진 일치. 동일 DB 서버·인증정보까지 검증한 것은 아닙니다.", "mismatch": "BookOasis와 Mate DB 엔진이 다릅니다. 이관을 차단합니다."}[db_status]}
    root = str(remote_cover.get("root") or "").rstrip("/")
    expected = str(settings.get("cover_storage_remote_root") or "").rstrip("/")
    local = str(settings.get("cover_root_path") or "").rstrip("/")
    status = "unknown"
    if remote_cover.get("success"):
        if remote_cover.get("migration_status") not in {"idle", "running", "done", "error"}:
            status = "unknown"
        elif remote_cover.get("migration_status") == "running":
            status = "moving"
        elif expected and expected != (root or "<default>"):
            status = "mismatch"
        elif not root:
            status = "default"
        elif expected == root:
            status = "mapped"
        else:
            status = "mapping_required"
    messages = {
        "unknown": "커버 설정·이동 상태 확인 불가. 서버 중지 또는 구버전이면 수동으로 경로를 확인하세요.",
        "moving": "BookOasis 커버 이동 중입니다. 커버 검사·정리·이관을 중단합니다.",
        "mismatch": "확인했던 원격 커버 경로가 변경되었습니다. 마운트 대응을 다시 확인하세요.",
        "mapping_required": "원격 커버 경로와 Mate 경로의 마운트 대응 확인이 필요합니다. 설정의 원격 커버 경로 확인값을 입력하세요.",
        "default": "BookOasis 기본 커버 경로 사용. Mate의 마운트가 같은 저장소인지 확인하세요.",
        "mapped": "커버 경로 대응 확인값 일치. 실제 마운트 동일성이나 서버 폴백까지 보장하지 않습니다.",
    }
    return {"db_engine": db, "cover_storage": {"status": status, "remote_root": root or "<default>", "local_root": local, "message": messages[status]}}
