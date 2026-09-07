# 요청 경계 중지와 이미 완료된 스캔 결과 보존을 검증합니다.
import threading
import unittest
from unittest.mock import Mock, patch

from gdrive_scan import GDriveScanProcessor
from bookoasis_client import BookOasisClient
from gdrive_changes import GoogleDriveChangesClient


class StopTest(unittest.TestCase):
    def test_stop_after_login_prevents_admin_scan(self):
        client = BookOasisClient("http://bookoasis:5930")
        stop = threading.Event()
        client.should_stop = stop.is_set
        def login():
            stop.set()
            return {"success": True}
        with patch.object(client, "login_admin", side_effect=login), patch.object(client._opener, "open") as request:
            result = client.scan_library_path(1, "series")
        request.assert_not_called()
        self.assertTrue(result["cancelled"])

    def test_stopped_drive_client_does_not_request_next_page(self):
        request = Mock()
        client = GoogleDriveChangesClient("rclone", "test.conf", "drive", "root", "", "/books", opener=request)
        client.should_stop = lambda: True
        with self.assertRaises(InterruptedError):
            client._get("changes")
        request.assert_not_called()

    def test_stop_after_scan_preserves_completed_and_defers_remaining(self):
        stop = threading.Event()
        calls = []
        def scan(*args):
            calls.append(args)
            stop.set()
            return {"success": True}
        library = {"db_type": "general", "id": 1, "name": "Books", "root": "/books"}
        with patch.object(GDriveScanProcessor, "_load_libraries", return_value=[library]):
            processor = GDriveScanProcessor({}, scan, path_scan_callback=scan, rc_client=Mock())
        processor.should_stop = stop.is_set
        events = [{"id": n, "action": "create", "item_type": "file", "path": f"/books/{n}/a.epub"} for n in (1, 2)]
        with patch("gdrive_scan.find_vfs_rule", return_value={}):
            results = processor.process_batch(events)
        self.assertEqual(1, len(calls))
        self.assertTrue(results[1]["success"])
        self.assertTrue(results[2]["cancelled"])

    def test_stop_after_vfs_does_not_submit_scan(self):
        stop = threading.Event()
        rc = Mock()
        rc.forget.side_effect = lambda *args: stop.set()
        scan = Mock()
        library = {"db_type": "general", "id": 1, "name": "Books", "root": "/books"}
        with patch.object(GDriveScanProcessor, "_load_libraries", return_value=[library]):
            processor = GDriveScanProcessor({}, scan, rc_client=rc)
        processor.should_stop = stop.is_set
        with patch("gdrive_scan.find_vfs_rule", return_value={}):
            results = processor.process_batch([{"id": 1, "action": "delete", "item_type": "file", "path": "/books/a.epub"}])
        scan.assert_not_called()
        self.assertTrue(results[1]["cancelled"])
