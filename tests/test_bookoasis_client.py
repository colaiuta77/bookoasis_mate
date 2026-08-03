# BookOasis 상태 API 클라이언트의 URL 제한과 정상 응답 처리를 검증합니다.
import io
import json
import unittest
from http.client import IncompleteRead
from unittest.mock import Mock, patch

from bookoasis_client import BookOasisClient


class _Response:
    def __init__(self, payload, status=200, headers=None):
        self.buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.buffer.read()


class BookOasisClientTest(unittest.TestCase):
    def test_audiobook_is_allowed_for_library_operations_only(self):
        client = BookOasisClient("http://bookoasis:5930")
        client._admin_request = lambda path, **kwargs: {
            "path": path,
            "query": kwargs.get("query"),
            "form": kwargs.get("form"),
        }

        schedules = client.library_schedules("audiobook")
        scan = client.scan_library(7, "audiobook")
        cover_scan = client.scan_library_covers(7, "audiobook")
        book_scan = client.scan_book(7, "audiobook")

        self.assertEqual({"type": "audiobook"}, schedules["query"])
        self.assertEqual("audiobook", scan["form"]["type"])
        self.assertFalse(cover_scan["success"])
        self.assertFalse(book_scan["success"])

    def test_defaults_api_timeout_to_thirty_seconds(self):
        client = BookOasisClient("http://bookoasis:5930")

        self.assertEqual(30, client.timeout)

    def test_rejects_non_http_url(self):
        result = BookOasisClient("file:///etc/passwd").health()

        self.assertFalse(result["success"])
        self.assertIn("http", result["message"])

    @patch("bookoasis_client.urlopen", return_value=_Response({"status": "healthy", "service": "BookOasis"}))
    def test_health_endpoint(self, mocked_urlopen):
        result = BookOasisClient("http://bookoasis:5930").health()

        self.assertTrue(result["success"])
        self.assertEqual("http://bookoasis:5930/health", result["url"])
        self.assertEqual("http://bookoasis:5930/health", mocked_urlopen.call_args.args[0].full_url)

    @patch("bookoasis_client.urlopen", return_value=_Response({"success": True, "already_queued": False}))
    def test_requests_library_rescan_without_exposing_token_in_result(self, mocked_urlopen):
        result = BookOasisClient("http://bookoasis:5930").request_scan("secret-token", 12, "adult")

        request = mocked_urlopen.call_args.args[0]
        self.assertTrue(result["success"])
        self.assertNotIn("secret-token", str(result))
        self.assertEqual("http://bookoasis:5930/api/webhook/scan", request.full_url)
        self.assertIn(b"library_id=12", request.data)
        self.assertIn(b"type=adult", request.data)

    @patch(
        "bookoasis_client.urlopen",
        return_value=_Response({}, headers={"Content-Type": "image/webp", "Content-Length": "1234"}),
    )
    def test_inspects_cover_headers(self, mocked_urlopen):
        result = BookOasisClient("http://bookoasis:5930").inspect_cover("1/cover.webp")

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual("ok", result["status"])
        self.assertEqual(1234, result["content_length"])
        self.assertEqual("HEAD", request.method)
        self.assertEqual("http://bookoasis:5930/covers/1/cover.webp", request.full_url)

    def test_admin_session_scans_single_book_without_exposing_password(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"success": True, "role": "admin"}),
            _Response({"success": True, "message": "완료", "cover_image": "1/new.webp"}),
        ]
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="secret-password",
            opener=opener,
        )

        result = client.scan_book(12, "adult")

        login_request = opener.open.call_args_list[0].args[0]
        scan_request = opener.open.call_args_list[1].args[0]
        self.assertTrue(result["success"])
        self.assertNotIn("secret-password", str(result))
        self.assertEqual("http://bookoasis:5930/login", login_request.full_url)
        self.assertIn(b'"username": "admin"', login_request.data)
        self.assertEqual("http://bookoasis:5930/api/media/books/12/scan", scan_request.full_url)
        self.assertIn(b"type=adult", scan_request.data)

    def test_admin_request_converts_incomplete_response_to_retryable_failure(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"success": True, "role": "admin"}),
            IncompleteRead(b"", 128),
        ]
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="secret-password",
            opener=opener,
        )

        result = client.scan_book(12, "general")

        self.assertFalse(result["success"])
        self.assertTrue(result["retryable"])
        self.assertIn("응답", result["message"])

    def test_admin_session_rejects_default_password_account(self):
        opener = Mock()
        opener.open.return_value = _Response({"success": True, "role": "admin", "is_default_password": 1})
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="admin",
            opener=opener,
        )

        result = client.login_admin()

        self.assertFalse(result["success"])
        self.assertIn("변경", result["message"])

    def test_admin_session_searches_and_applies_plugin_metadata(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"success": True, "role": "admin"}),
            _Response({"success": True, "plugins": [{"id": "aladin", "name": "알라딘"}]}),
            _Response({"success": True, "results": [{"title": "어린왕자"}]}),
            _Response({"success": True, "message": "적용 완료"}),
        ]
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="secret-password",
            opener=opener,
        )

        plugins = client.metadata_plugins()
        searched = client.search_metadata("어린 왕자", "aladin", "general")
        applied = client.apply_metadata(7, {"title": "어린왕자"}, "aladin", "general")

        search_request = opener.open.call_args_list[2].args[0]
        apply_request = opener.open.call_args_list[3].args[0]
        self.assertEqual("aladin", plugins["plugins"][0]["id"])
        self.assertEqual("어린왕자", searched["results"][0]["title"])
        self.assertTrue(applied["success"])
        self.assertIn("query=%EC%96%B4%EB%A6%B0+%EC%99%95%EC%9E%90", search_request.full_url)
        self.assertIn("source=aladin", search_request.full_url)
        self.assertEqual("http://bookoasis:5930/api/media/books/7/apply-metadata", apply_request.full_url)
        self.assertEqual("application/json", apply_request.headers["Content-type"])
        self.assertEqual("general", json.loads(apply_request.data)["type"])

    def test_admin_session_reads_live_scanner_queue_without_cache(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"success": True, "role": "admin"}),
            _Response({
                "success": True,
                "queue": {
                    "running": {"type": "lazy_scan", "key": "lazy_scan", "stage": "RAM 환수 재기동"},
                    "pending": [],
                },
            }),
        ]
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="secret-password",
            opener=opener,
        )

        result = client.queue_status()

        queue_request = opener.open.call_args_list[1].args[0]
        self.assertTrue(result["success"])
        self.assertEqual("lazy_scan", result["queue"]["running"]["type"])
        self.assertIn("http://bookoasis:5930/api/media/system/queue?_ts=", queue_request.full_url)

    def test_admin_session_controls_library_scans_and_pending_queue(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"success": True, "role": "admin"}),
            _Response({"success": True, "libraries": [{"id": 3, "name": "만화"}]}),
            _Response({"success": True, "message": "스캔 등록"}),
            _Response({"success": True, "message": "전체 등록"}),
            _Response({"success": True, "message": "취소 요청"}),
            _Response({"success": True, "message": "표지 등록"}),
            _Response({"success": True, "message": "2건 삭제"}),
            _Response({"success": True, "message": "작업 취소"}),
        ]
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="secret-password",
            opener=opener,
        )

        libraries = client.library_schedules("adult")
        scanned = client.scan_library(3, "adult", force=True)
        scanned_all = client.scan_all_libraries("adult")
        cancelled = client.cancel_library_scan(3, "adult")
        covers = client.scan_library_covers(3, "adult")
        cleared = client.clear_queue()
        pending_cancelled = client.cancel_queue_task("library_scan_adult_3")

        requests = [call.args[0] for call in opener.open.call_args_list[1:]]
        self.assertEqual(3, libraries["libraries"][0]["id"])
        self.assertTrue(scanned["success"])
        self.assertTrue(scanned_all["success"])
        self.assertTrue(cancelled["success"])
        self.assertTrue(covers["success"])
        self.assertTrue(cleared["success"])
        self.assertTrue(pending_cancelled["success"])
        self.assertIn("type=adult", requests[0].full_url)
        self.assertEqual(
            "http://bookoasis:5930/api/media/libraries/3/scan",
            requests[1].full_url,
        )
        self.assertIn(b"force=true", requests[1].data)
        self.assertEqual(
            "http://bookoasis:5930/api/media/libraries/scan-all",
            requests[2].full_url,
        )
        self.assertEqual(
            "http://bookoasis:5930/api/media/libraries/3/cancel-scan",
            requests[3].full_url,
        )
        self.assertEqual(
            "http://bookoasis:5930/api/media/libraries/3/scan-covers",
            requests[4].full_url,
        )
        self.assertEqual(
            "http://bookoasis:5930/api/media/system/queue/clear",
            requests[5].full_url,
        )
        self.assertIn(b"task_id=library_scan_adult_3", requests[6].data)

    def test_admin_session_reads_permissions_and_metadata_diagnostics(self):
        opener = Mock()
        opener.open.side_effect = [
            _Response({"success": True, "role": "admin"}),
            _Response({"success": True, "users": [{"id": 1, "username": "reader"}]}),
            _Response({
                "success": True,
                "plugins": [{
                    "id": "aladin",
                    "enabled": True,
                    "is_searchable": True,
                    "config": {"ALADIN": "secret"},
                }],
            }),
        ]
        client = BookOasisClient(
            "http://bookoasis:5930",
            username="admin",
            password="secret-password",
            opener=opener,
        )

        permissions = client.permissions()
        plugins = client.metadata_plugins_manage()

        self.assertEqual("reader", permissions["users"][0]["username"])
        self.assertEqual("aladin", plugins["plugins"][0]["id"])
        self.assertEqual(
            "http://bookoasis:5930/api/admin/permissions",
            opener.open.call_args_list[1].args[0].full_url,
        )
        self.assertEqual(
            "http://bookoasis:5930/api/media/metadata/plugins/manage",
            opener.open.call_args_list[2].args[0].full_url,
        )


if __name__ == "__main__":
    unittest.main()
