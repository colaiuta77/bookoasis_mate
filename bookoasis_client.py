# BookOasis 공개 상태 API와 관리자 세션 API에 안전하게 연결합니다.
import json
import posixpath
import time
from http.cookiejar import CookieJar
from http.client import HTTPException, IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen


class BookOasisClient:
    """BookOasis 공개 API와 관리자 세션 API를 호출합니다."""

    def __init__(self, base_url, timeout=30, username="", password="", opener=None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.timeout = max(1, min(int(timeout or 30), 30))
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._opener = opener or build_opener(HTTPCookieProcessor(CookieJar()))
        self._authenticated = False

    def _valid_base_url(self):
        parsed = urlparse(self.base_url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _http_error_message(error):
        try:
            payload = json.loads(error.read().decode("utf-8"))
            return payload.get("error") or payload.get("message") or f"HTTP 오류 {error.code}"
        except (AttributeError, ValueError, OSError):
            return f"HTTP 오류 {error.code}"

    @staticmethod
    def _response_payload(response):
        return json.loads(response.read().decode("utf-8"))

    def _admin_error(self, message, http_status=None, retryable=False):
        data = {"success": False, "message": str(message or "BookOasis 관리자 API 요청에 실패했습니다.")}
        if http_status is not None:
            data["http_status"] = int(http_status)
        if retryable:
            data["retryable"] = True
        return data

    def login_admin(self, force=False):
        if self._authenticated and not force:
            return {"success": True, "message": "BookOasis 관리자 세션을 사용합니다."}
        if not self._valid_base_url():
            return self._admin_error("BookOasis URL이 올바르지 않습니다.")
        if not self.username or not self.password:
            return self._admin_error("BookOasis 관리자 계정이 설정되지 않았습니다.")

        request = Request(
            urljoin(f"{self.base_url}/", "login"),
            data=json.dumps({"username": self.username, "password": self.password}).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = self._response_payload(response)
            if not payload.get("success"):
                return self._admin_error(payload.get("error") or "BookOasis 관리자 로그인에 실패했습니다.")
            if payload.get("role") not in (None, "admin"):
                return self._admin_error("BookOasis 관리자 권한이 필요합니다.")
            if payload.get("is_default_password") == 1:
                return self._admin_error("BookOasis 기본 관리자 비밀번호를 먼저 변경해 주세요.")
            self._authenticated = True
            return {"success": True, "message": "BookOasis 관리자 로그인에 성공했습니다."}
        except HTTPError as error:
            self._authenticated = False
            return self._admin_error(self._http_error_message(error), error.code)
        except (URLError, ValueError, OSError) as error:
            self._authenticated = False
            return self._admin_error(f"BookOasis 관리자 로그인 실패: {getattr(error, 'reason', error)}")

    def _admin_request(self, path, method="GET", form=None, payload=None, query=None, retry=True, timeout=None):
        login = self.login_admin()
        if not login.get("success"):
            return login

        url = urljoin(f"{self.base_url}/", str(path or "").lstrip("/"))
        if query:
            url = f"{url}?{urlencode(query)}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            request_timeout = self.timeout if timeout is None else max(1, min(int(timeout), 600))
            with self._opener.open(request, timeout=request_timeout) as response:
                return self._response_payload(response)
        except HTTPError as error:
            if error.code == 401 and retry:
                self._authenticated = False
                login = self.login_admin(force=True)
                if login.get("success"):
                    return self._admin_request(path, method=method, form=form, payload=payload, query=query, retry=False, timeout=timeout)
            return self._admin_error(
                self._http_error_message(error),
                error.code,
                retryable=error.code in {408, 429, 500, 502, 503, 504},
            )
        except (IncompleteRead, RemoteDisconnected, HTTPException) as error:
            return self._admin_error(
                f"BookOasis 관리자 API 응답 수신 실패: {error}",
                retryable=True,
            )
        except (URLError, ValueError, OSError) as error:
            return self._admin_error(
                f"BookOasis 관리자 API 요청 실패: {getattr(error, 'reason', error)}",
                retryable=True,
            )

    @staticmethod
    def _valid_library_db_type(db_type):
        return db_type if db_type in {"general", "adult", "audiobook", "video"} else None

    @staticmethod
    def _valid_book_db_type(db_type):
        return db_type if db_type in {"general", "adult"} else None

    def _valid_positive_id(self, value, label):
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None, self._admin_error(f"{label}가 올바르지 않습니다.")
        if value <= 0:
            return None, self._admin_error(f"{label}가 올바르지 않습니다.")
        return value, None

    def scan_book(self, book_id, db_type="general"):
        book_id, error = self._valid_positive_id(book_id, "도서 ID")
        if error:
            return error
        db_type = self._valid_book_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        return self._admin_request(
            f"api/media/books/{book_id}/scan",
            method="POST",
            form={"type": db_type},
        )

    def metadata_plugins(self):
        return self._admin_request("api/media/metadata/plugins")

    def metadata_plugins_manage(self):
        return self._admin_request("api/media/metadata/plugins/manage")

    def plugin_load_status(self, token=""):
        token = str(token or "").strip()
        if not token:
            return self._admin_request("api/media/plugins/load-status")
        request = Request(
            urljoin(f"{self.base_url}/", "api/webhook/plugins/status"),
            headers={"Accept": "application/json", "X-Webhook-Token": token},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return self._response_payload(response)
        except HTTPError as error:
            return self._admin_error(self._http_error_message(error), error.code)
        except (URLError, ValueError, OSError) as error:
            return self._admin_error(
                f"BookOasis 플러그인 상태 조회 실패: {getattr(error, 'reason', error)}",
                retryable=True,
            )

    def toggle_plugin(self, plugin_id, enabled, db_type="general"):
        plugin_id = str(plugin_id or "").strip()
        if not plugin_id:
            return self._admin_error("플러그인 ID가 비어 있습니다.")
        return self._admin_request(
            "api/media/metadata/plugins/toggle",
            method="POST",
            form={"type": db_type, "plugin_id": plugin_id, "enabled": "1" if enabled else "0"},
        )

    def save_plugin_config(self, plugin_id, config, db_type="general"):
        plugin_id = str(plugin_id or "").strip()
        if not plugin_id or not isinstance(config, dict):
            return self._admin_error("플러그인 설정 요청이 올바르지 않습니다.")
        return self._admin_request(
            "api/media/metadata/plugins/save-config",
            method="POST",
            payload={"type": db_type, "plugin_id": plugin_id, "config": config},
        )

    def sample_update_plugin(self, plugin_id):
        plugin_id = str(plugin_id or "").strip()
        if not plugin_id:
            return self._admin_error("플러그인 ID가 비어 있습니다.")
        return self._admin_request(
            "api/media/metadata/plugins/sample-update",
            method="POST",
            payload={"plugin_id": plugin_id},
        )

    def permissions(self):
        return self._admin_request("api/admin/permissions")

    def library_schedules(self, db_type="general"):
        db_type = self._valid_library_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        return self._admin_request(
            "api/media/libraries/schedules",
            query={"type": db_type},
        )

    def scan_library(self, library_id, db_type="general", force=False):
        library_id, error = self._valid_positive_id(library_id, "보관함 ID")
        if error:
            return error
        db_type = self._valid_library_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        return self._admin_request(
            f"api/media/libraries/{library_id}/scan",
            method="POST",
            form={
                "type": db_type,
                "force": "true" if force else "false",
            },
        )

    def scan_library_path(
        self,
        library_id,
        relative_path,
        db_type="general",
        force=False,
        timeout=None,
    ):
        library_id, error = self._valid_positive_id(library_id, "보관함 ID")
        if error:
            return error
        db_type = self._valid_library_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        relative_path = str(relative_path or "").strip().replace("\\", "/")
        parts = [part for part in relative_path.split("/") if part]
        if (
            not relative_path
            or relative_path.startswith("/")
            or ".." in parts
            or posixpath.normpath(relative_path) in {"", ".", ".."}
        ):
            return self._admin_error("보관함 기준 상대 디렉터리 경로가 올바르지 않습니다.")
        relative_path = posixpath.normpath(relative_path)
        return self._admin_request(
            f"api/media/libraries/{library_id}/scan-path",
            method="POST",
            form={
                "type": db_type,
                "path": relative_path,
                "force": "true" if force else "false",
            },
            timeout=timeout,
        )

    def scan_all_libraries(self, db_type="general", force=False):
        db_type = self._valid_library_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        return self._admin_request(
            "api/media/libraries/scan-all",
            method="POST",
            form={
                "type": db_type,
                "force": "true" if force else "false",
            },
        )

    def cancel_library_scan(self, library_id, db_type="general"):
        library_id, error = self._valid_positive_id(library_id, "보관함 ID")
        if error:
            return error
        db_type = self._valid_library_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        return self._admin_request(
            f"api/media/libraries/{library_id}/cancel-scan",
            method="POST",
            form={"type": db_type},
        )

    def scan_library_covers(self, library_id, db_type="general"):
        library_id, error = self._valid_positive_id(library_id, "보관함 ID")
        if error:
            return error
        db_type = self._valid_book_db_type(db_type)
        if not db_type:
            return self._admin_error("표지 전용 스캔은 일반·성인 도서 DB에서만 지원합니다.")
        return self._admin_request(
            f"api/media/libraries/{library_id}/scan-covers",
            method="POST",
            form={"type": db_type},
        )

    def queue_status(self):
        return self._admin_request(
            "api/media/system/queue",
            query={"_ts": str(time.time_ns())},
        )

    def clear_queue(self):
        return self._admin_request(
            "api/media/system/queue/clear",
            method="POST",
        )

    def cancel_queue_task(self, task_key):
        task_key = str(task_key or "").strip()
        if not task_key:
            return self._admin_error("대기열 작업 키가 비어 있습니다.")
        return self._admin_request(
            "api/media/system/queue/cancel",
            method="POST",
            form={"task_id": task_key},
        )

    def search_metadata(self, query, source=None, db_type="general"):
        db_type = self._valid_book_db_type(db_type)
        query = str(query or "").strip()
        source = str(source or "").strip()
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        if not query:
            return self._admin_error("메타데이터 검색어가 비어 있습니다.")
        params = {"type": db_type, "query": query}
        if source:
            params["source"] = source
        return self._admin_request("api/media/books/search-metadata", query=params)

    def apply_metadata(self, book_id, item_data, source=None, db_type="general"):
        book_id, error = self._valid_positive_id(book_id, "도서 ID")
        if error:
            return error
        db_type = self._valid_book_db_type(db_type)
        if not db_type:
            return self._admin_error("DB 유형이 올바르지 않습니다.")
        if not isinstance(item_data, dict) or not item_data:
            return self._admin_error("적용할 메타데이터가 비어 있습니다.")
        return self._admin_request(
            f"api/media/books/{book_id}/apply-metadata",
            method="POST",
            payload={"type": db_type, "source": str(source or "").strip() or None, "item_data": item_data},
        )

    def health(self):
        if not self.base_url:
            return {"success": False, "message": "BookOasis URL이 비어 있습니다."}
        if not self._valid_base_url():
            return {"success": False, "message": "BookOasis URL은 http 또는 https 주소여야 합니다."}

        url = urljoin(f"{self.base_url}/", "health")
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            healthy = payload.get("status") == "healthy"
            return {
                "success": healthy,
                "url": url,
                "status": payload.get("status", "unknown"),
                "service": payload.get("service", "BookOasis"),
                "message": "BookOasis 상태 API 연결에 성공했습니다." if healthy else "BookOasis 상태가 healthy가 아닙니다.",
            }
        except HTTPError as error:
            return {"success": False, "url": url, "message": f"BookOasis HTTP 오류 {error.code}"}
        except URLError as error:
            return {"success": False, "url": url, "message": f"BookOasis 연결 실패: {error.reason}"}
        except (ValueError, OSError) as error:
            return {"success": False, "url": url, "message": f"BookOasis 응답 확인 실패: {error}"}

    def request_scan(self, token, library_id, db_type="general", force=False):
        if not self._valid_base_url():
            return {"success": False, "message": "BookOasis URL이 올바르지 않습니다."}
        token = str(token or "").strip()
        if not token:
            return {"success": False, "message": "WEBHOOK_TOKEN이 설정되지 않았습니다."}
        try:
            library_id = int(library_id)
        except (TypeError, ValueError):
            return {"success": False, "message": "보관함 ID가 올바르지 않습니다."}
        if db_type not in {"general", "adult", "audiobook", "video"}:
            return {"success": False, "message": "DB 유형이 올바르지 않습니다."}

        url = urljoin(f"{self.base_url}/", "api/webhook/scan")
        data = urlencode({
            "token": token,
            "library_id": library_id,
            "type": db_type,
            "force": "1" if force else "0",
        }).encode("utf-8")
        request = Request(url, data=data, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return {
                "success": bool(payload.get("success")),
                "already_queued": bool(payload.get("already_queued")),
                "message": payload.get("message") or "재스캔 요청을 처리했습니다.",
                "library_id": library_id,
                "db_type": db_type,
            }
        except HTTPError as error:
            return {"success": False, "message": self._http_error_message(error), "library_id": library_id, "db_type": db_type}
        except (URLError, ValueError, OSError) as error:
            return {"success": False, "message": f"재스캔 요청 실패: {getattr(error, 'reason', error)}", "library_id": library_id, "db_type": db_type}

    def request_scan_path(self, token, library_id, relative_path, db_type="general", force=False, timeout=None):
        if not self._valid_base_url():
            return {"success": False, "message": "BookOasis URL이 올바르지 않습니다."}
        token = str(token or "").strip()
        if not token:
            return {"success": False, "message": "WEBHOOK_TOKEN이 설정되지 않았습니다."}
        library_id, error = self._valid_positive_id(library_id, "보관함 ID")
        if error:
            return error
        db_type = self._valid_library_db_type(db_type)
        if not db_type:
            return {"success": False, "message": "DB 유형이 올바르지 않습니다."}
        relative_path = str(relative_path or "").strip().replace("\\", "/")
        parts = [part for part in relative_path.split("/") if part]
        if not relative_path or relative_path.startswith("/") or ".." in parts:
            return {"success": False, "message": "보관함 기준 상대 디렉터리 경로가 올바르지 않습니다."}
        relative_path = posixpath.normpath(relative_path)
        data = urlencode({
            "token": token,
            "library_id": library_id,
            "type": db_type,
            "force": "1" if force else "0",
            "path": relative_path,
        }).encode("utf-8")
        request = Request(
            urljoin(f"{self.base_url}/", "api/webhook/scan"),
            data=data,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        request_timeout = self.timeout if timeout is None else max(1, min(int(timeout), 600))
        try:
            with urlopen(request, timeout=request_timeout) as response:
                payload = self._response_payload(response)
            mode = "full_webhook_compat" if "already_queued" in payload else "path_webhook"
            return {
                "success": bool(payload.get("success")),
                "already_queued": bool(payload.get("already_queued")),
                "message": payload.get("message") or "경로 스캔 요청을 처리했습니다.",
                "library_id": library_id,
                "db_type": db_type,
                "mode": mode,
            }
        except HTTPError as error:
            return {"success": False, "message": self._http_error_message(error), "http_status": error.code, "library_id": library_id, "db_type": db_type, "mode": "path_webhook"}
        except (URLError, ValueError, OSError) as error:
            return {"success": False, "message": f"경로 스캔 요청 실패: {getattr(error, 'reason', error)}", "retryable": True, "library_id": library_id, "db_type": db_type, "mode": "path_webhook"}

    def inspect_cover(self, relative_path):
        relative = str(relative_path or "").split("?", 1)[0].replace("\\", "/").strip().lstrip("/")
        if relative.lower().startswith("covers/"):
            relative = relative[7:]
        if not relative:
            return {"status": "missing_reference", "content_type": "", "content_length": 0}
        if not self._valid_base_url():
            return {"status": "invalid_url", "content_type": "", "content_length": 0}
        url = f"{self.base_url}/covers/{quote(relative, safe='/')}"
        request = Request(url, method="HEAD", headers={"Accept": "image/*"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                try:
                    content_length = int(response.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    content_length = 0
            status = "ok" if content_type.startswith("image/") else "not_image"
            return {"status": status, "content_type": content_type, "content_length": content_length, "url": url}
        except HTTPError as error:
            return {"status": "http_error", "http_status": error.code, "content_type": "", "content_length": 0, "url": url}
        except (URLError, OSError) as error:
            return {"status": "connection_error", "error": str(getattr(error, "reason", error)), "content_type": "", "content_length": 0, "url": url}
