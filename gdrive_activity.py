# Drive Activity를 시간 구간별로 조회하고 영속 중복 제거용 이벤트로 변환합니다.
import hashlib
import json
import posixpath
from datetime import datetime, timedelta, timezone

try:
    from .gdrive_changes import GoogleDriveChangesClient, GoogleDriveApiError, google_drive_state_remote
except ImportError:
    from gdrive_changes import GoogleDriveChangesClient, GoogleDriveApiError, google_drive_state_remote


class GoogleDriveActivityClient(GoogleDriveChangesClient):
    detection_mode = "activity"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state_remote = google_drive_state_remote(self.remote, self.source_remote, "activity")
        self.item_scope = f"{self.state_remote}:{self.root_id}"

    @staticmethod
    def _time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    @staticmethod
    def _stamp(value):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")

    def _query(self, body):
        return self._get("", activity_body=body)

    def validate_root(self):
        root = super().validate_root()
        remote = self._run_json("config", "dump").get(self.source_remote) or {}
        scopes = {scope.rsplit("/", 1)[-1] for scope in str(remote.get("scope") or "").replace(",", " ").split()}
        if not (scopes & {"drive", "drive.readonly"} and scopes & {"drive.activity", "drive.activity.readonly"}):
            raise ValueError("Activity 방식에는 drive.readonly,drive.activity.readonly 권한이 필요합니다. rclone scope 설정 후 해당 권한으로 다시 인증해 주세요.")
        now = self._stamp(datetime.now(timezone.utc))
        self._query({"ancestorName": "items/" + self.root_id, "pageSize": 1,
                     "filter": f'time >= "{now}"'})
        return root

    def start_page_token(self):
        self.validate_root()
        now = self._stamp(datetime.now(timezone.utc))
        return json.dumps({"start": now, "floor": now})

    def list_changes(self, page_token):
        cursor = json.loads(page_token)
        start = self._time(cursor["start"])
        # 1분 지연 및 5분 중첩으로 늦게 공개된 활동을 다시 확인합니다.
        end = self._time(cursor["end"]) if cursor.get("end") else datetime.now(timezone.utc) - timedelta(seconds=60)
        if end <= start:
            return
        body = {"ancestorName": "items/" + self.root_id, "pageSize": 100,
                "consolidationStrategy": {"none": {}},
                "filter": f'time >= "{self._stamp(start)}" AND time < "{self._stamp(end)}"'}
        if cursor.get("page"):
            body["pageToken"] = cursor["page"]
        try:
            payload = self._query(body)
        except GoogleDriveApiError as error:
            if error.status_code != 400 or "pageToken" not in body:
                raise
            # 만료된 페이지 토큰은 같은 고정 구간의 첫 페이지부터 안전하게 재생합니다.
            body.pop("pageToken")
            payload = self._query(body)
        for activity in payload.get("activities") or []:
            for action in activity.get("actions") or []:
                detail = action.get("detail") or {}
                if not any(kind in detail for kind in ("create", "edit", "move", "rename", "delete", "restore")):
                    continue
                # 개별 대상이 생략된 action은 활동의 모든 공통 대상에 적용됩니다.
                targets = [action["target"]] if action.get("target") else activity.get("targets") or []
                if not targets:
                    raise RuntimeError("Activity 응답에 대상 정보가 없습니다. 체크포인트를 유지합니다.")
                when = action.get("timestamp") or (action.get("timeRange") or {}).get("endTime")
                when = when or activity.get("timestamp") or (activity.get("timeRange") or {}).get("endTime")
                for target in targets:
                    if "driveItem" not in target:
                        continue
                    file_id = str((target.get("driveItem") or {}).get("name") or "").removeprefix("items/")
                    if not file_id or not when:
                        raise RuntimeError("Activity 응답에 파일 ID 또는 활동 시각이 없습니다. 체크포인트를 유지합니다.")
                    stamp = self._stamp(self._time(when))
                    identity = json.dumps([file_id, when, detail, action.get("actor")], sort_keys=True, separators=(",", ":"))
                    receipt_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                    yield {"fileId": file_id, "activity_action": dict(action, target=target),
                           "receipt": {"file_id": receipt_id, "parent_id": stamp, "path": "activity"}}, ""
        next_page = payload.get("nextPageToken")
        if next_page:
            cursor.update(end=self._stamp(end), page=next_page)
        else:
            cursor = {"start": self._stamp(max(self._time(cursor["floor"]), end - timedelta(minutes=5))),
                      "floor": cursor["floor"]}
        yield None, json.dumps(cursor)

    def _path(self, file_data):
        parts, seen = [], set()
        node = file_data
        for _ in range(100):
            file_id = str(node.get("id") or "")
            if file_id == self.root_id:
                return posixpath.join(self.local_root, *reversed(parts))
            if not file_id or file_id in seen:
                return ""
            seen.add(file_id)
            name = str(node.get("name") or "")
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise ValueError("Activity 파일 이름을 안전한 로컬 경로로 변환할 수 없습니다.")
            parts.append(name)
            parents = node.get("parents") or []
            if not parents:
                return ""
            node = self.file(parents[0])
        raise RuntimeError("Activity 폴더 경로의 최대 탐색 깊이를 초과했습니다.")

    def activity_event(self, change, previous, item_model):
        action = change["activity_action"]
        detail = action.get("detail") or {}
        target = (action.get("target") or {}).get("driveItem") or {}
        file_id = change["fileId"]
        kind = next(key for key in ("delete", "move", "rename", "restore", "create", "edit") if key in detail)
        old_path = str((previous or {}).get("path") or "")
        data = None
        try:
            data = self.file(file_id)
        except GoogleDriveApiError as error:
            if error.status_code != 404:
                raise
        new_path = self._path(data) if data and not data.get("trashed") else ""
        item_type = "directory" if "driveFolder" in target or (data or {}).get("mimeType") == "application/vnd.google-apps.folder" else "file"
        if kind == "rename" and new_path:
            old_title = (detail["rename"] or {}).get("oldTitle")
            if old_title and old_title not in {".", ".."} and "/" not in old_title and "\\" not in old_title:
                old_path = posixpath.join(posixpath.dirname(new_path), old_title)
        if kind == "move":
            parents = (detail["move"] or {}).get("removedParents") or []
            if parents:
                parent_id = str((parents[0].get("driveItem") or {}).get("name") or "").removeprefix("items/")
                if parent_id:
                    old_path = self._path({"id": file_id, "name": target.get("title") or (data or {}).get("name"), "parents": [parent_id]})
        if not new_path and not old_path:
            raise RuntimeError("Activity 대상의 현재·이전 경로를 확인할 수 없습니다. 파일 접근 권한과 실제 감시 폴더를 확인해 주세요. 체크포인트를 유지합니다.")
        current = {"file_id": file_id, "path": new_path, "is_directory": item_type == "directory",
                   "parent_id": str(((data or {}).get("parents") or [""])[0])} if new_path else None
        if not new_path:
            kind = "delete"
        event = {"action": kind, "item_type": item_type, "path": new_path or old_path,
                 "removed_path": old_path if kind in {"move", "rename", "delete"} else ""}
        return current, event
