# 커스텀 폰트 업로드의 형식 검증과 안전한 저장을 확인합니다.
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from font_manager import CustomFontManager


def font_storage(filename, payload):
    return SimpleNamespace(filename=filename, stream=io.BytesIO(payload))


class CustomFontManagerTest(unittest.TestCase):
    def test_uploads_valid_font_and_lists_it(self):
        with tempfile.TemporaryDirectory() as root:
            manager = CustomFontManager(root)

            result = manager.upload(
                [font_storage("나눔폰트.woff2", b"wOF2" + b"font-data")]
            )

            self.assertEqual(["나눔폰트.woff2"], [item["name"] for item in result["uploaded"]])
            self.assertTrue((Path(root) / "나눔폰트.woff2").is_file())
            self.assertEqual(1, manager.list_fonts()["count"])

    def test_rejects_mismatched_signature_and_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            manager = CustomFontManager(root)
            first = manager.upload([font_storage("font.ttf", b"\x00\x01\x00\x00data")])
            invalid = manager.upload([font_storage("fake.ttf", b"NOT-font")])
            duplicate = manager.upload([font_storage("font.ttf", b"\x00\x01\x00\x00new")])

            self.assertEqual(1, len(first["uploaded"]))
            self.assertEqual(1, len(invalid["rejected"]))
            self.assertEqual(1, len(duplicate["rejected"]))
            self.assertFalse((Path(root) / "fake.ttf").exists())
            self.assertEqual(b"\x00\x01\x00\x00data", (Path(root) / "font.ttf").read_bytes())

    def test_requires_existing_configured_directory(self):
        with self.assertRaises(ValueError):
            CustomFontManager("").list_fonts()
