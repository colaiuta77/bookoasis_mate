# 스캔 응답 유실과 VFS 실패가 중복 요청으로 이어지지 않는지 검증합니다.
import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from bookoasis_client import BookOasisClient
from gdrive_scan import GDriveScanProcessor, event_scan_targets


class ScanFailureTest(unittest.TestCase):
    def test_webhook_timeout_and_gateway_error_require_manual_confirmation(self):
        client = BookOasisClient("http://bookoasis:5930")
        for error in (TimeoutError("read timed out"), HTTPError("http://bookoasis", 504, "Gateway Timeout", {}, io.BytesIO(b""))):
            with self.subTest(error=type(error).__name__), patch("bookoasis_client.urlopen", side_effect=error):
                result = client.request_scan_path("token", 1, "series")
            self.assertTrue(result.get("outcome_unknown"))
            self.assertFalse(result["retryable"])

    def test_admin_timeout_requires_manual_confirmation(self):
        client = BookOasisClient("http://bookoasis:5930", opener=Mock())
        client._authenticated = True
        client._opener.open.side_effect = TimeoutError("read timed out")
        result = client.scan_library_path(1, "series")
        self.assertTrue(result.get("outcome_unknown"))
        self.assertFalse(result["retryable"])

    def test_processor_preserves_unknown_outcome_and_vfs_reason(self):
        library = {"db_type": "general", "id": 1, "name": "books", "root": "/books"}
        settings = {"gdrive_scan_path_mappings": "", "gdrive_scan_vfs_rules": "/books|/books|http://rclone:5572"}
        event = {"id": 1, "action": "create", "item_type": "file", "path": "/books/series/book.epub"}
        rc = Mock()
        rc.refresh.return_value = {"result": {"series": "OK"}}
        with patch.object(GDriveScanProcessor, "_load_libraries", return_value=[library]):
            processor = GDriveScanProcessor(settings, Mock(), path_scan_callback=lambda *args: {
                "success": False, "message": "timeout", "outcome_unknown": True, "retryable": False
            }, rc_client=rc)
        result = processor.process_batch([event])[1]
        self.assertTrue(result.get("outcome_unknown"))
        self.assertFalse(result["retryable"])
        rc.forget.side_effect = RuntimeError("rclone RC HTTP 오류 503")
        event.update(action="rename", removed_path="/books/series/old.epub")
        result = processor.process_batch([event])[1]
        self.assertIn("503", result["message"])
        self.assertEqual("forget", result["vfs"][0]["operation"])
        self.assertEqual("/books/series/old.epub", result["vfs"][0]["path"])

    def test_multi_root_library_uses_library_scan_instead_of_ambiguous_relative_path(self):
        libraries = [{"db_type": "general", "id": 1, "name": "books", "root": root} for root in ("/a", "/b")]
        targets = event_scan_targets({"action": "create", "item_type": "file", "path": "/b/series/book.epub"}, libraries)
        self.assertEqual("", targets[0]["path"])


if __name__ == "__main__":
    unittest.main()
