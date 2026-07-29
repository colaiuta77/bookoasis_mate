# gd-poller 래퍼가 CommandDispatcher 인자를 FlaskFarm API 폼으로 전달하는지 검증합니다.
import importlib.util
import unittest
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "gd_poller_ff_bridge.py"
)
SPEC = importlib.util.spec_from_file_location("gd_poller_ff_bridge", SCRIPT)
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return b'{"ret":"success","accepted":true,"id":7,"ready_at":"2026-07-28T12:00:00"}'


class GdPollerBridgeTest(unittest.TestCase):
    def test_bridge_posts_all_dispatcher_arguments(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["data"] = parse_qs(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _Response()

        with patch.object(BRIDGE, "urlopen", side_effect=fake_urlopen):
            result = BRIDGE.post_event(
                "http://ff:9999/bookoasis_mate/api/gdrive_scan/event",
                "secret",
                "move",
                "file",
                "/GDRIVE/new.cbz",
                "/GDRIVE/old.cbz",
                timeout=12,
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(12, captured["timeout"])
        self.assertEqual(["secret"], captured["data"]["apikey"])
        self.assertEqual(["move"], captured["data"]["action"])
        self.assertEqual(["file"], captured["data"]["item_type"])
        self.assertEqual(["/GDRIVE/new.cbz"], captured["data"]["path"])
        self.assertEqual(["/GDRIVE/old.cbz"], captured["data"]["removed_path"])

    def test_main_requires_api_key(self):
        exit_code = BRIDGE.main(
            [
                "--apikey",
                "",
                "create",
                "file",
                "/GDRIVE/A.cbz",
            ]
        )

        self.assertEqual(2, exit_code)


if __name__ == "__main__":
    unittest.main()
