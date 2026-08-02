# gd-poller 이벤트의 경로 매핑, 보관함 선택, VFS 처리와 스캔 병합을 검증합니다.
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gdrive_scan import (
    GDriveScanProcessor,
    event_vfs_operations,
    map_path,
    parse_path_mappings,
    parse_vfs_rules,
    validate_event,
)


class _FakeRcClient:
    def __init__(self):
        self.calls = []

    def refresh(self, rule, path):
        self.calls.append(("refresh", path, "directory"))
        return {"result": {path: "OK"}}

    def forget(self, rule, path, item_type):
        self.calls.append(("forget", path, item_type))
        return {"forgotten": [path]}


class GDriveScanTest(unittest.TestCase):
    def test_invalid_vfs_rule_does_not_expose_credentials(self):
        with self.assertRaises(ValueError) as context:
            parse_vfs_rules(
                "/mnt/gds/GDRIVE|/GDRIVE|ftp://private-user:private-password@127.0.0.1"
            )

        message = str(context.exception)
        self.assertIn("VFS RC 주소", message)
        self.assertNotIn("private-user", message)
        self.assertNotIn("private-password", message)

    def _database(self, root, physical_path="/mnt/gds/GDRIVE/READING/만화"):
        db_path = Path(root) / "media_general.db"
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE libraries (
                id INTEGER PRIMARY KEY,
                name TEXT,
                physical_path TEXT
            );
            INSERT INTO libraries(id, name, physical_path)
            VALUES (25, '만화', ?);
            """
            .replace("?", f"'{physical_path}'")
        )
        connection.commit()
        connection.close()
        return db_path

    def test_longest_path_mapping_wins(self):
        mappings = parse_path_mappings(
            "/GDRIVE => /mnt/gds/GDRIVE\n"
            "/GDRIVE/READING/만화 => /special/comics"
        )
        self.assertEqual(
            "/special/comics/A.cbz",
            map_path("/GDRIVE/READING/만화/A.cbz", mappings),
        )

    def test_unsupported_file_is_ignored_but_directory_is_relevant(self):
        file_event = validate_event(
            "create", "file", "/GDRIVE/README.md", extensions=".cbz,.epub"
        )
        directory_event = validate_event(
            "create", "directory", "/GDRIVE/READING", extensions=".cbz,.epub"
        )

        self.assertFalse(file_event["relevant"])
        self.assertTrue(directory_event["relevant"])

    def test_bookoasis_ignore_control_file_is_relevant_without_extension(self):
        created = validate_event(
            "create",
            "file",
            "/GDRIVE/READING/만화/.bookoasisignore",
            extensions=".cbz,.epub",
        )
        deleted = validate_event(
            "delete",
            "file",
            "/GDRIVE/READING/만화/.bookoasisignore",
            removed_path="/GDRIVE/READING/만화/.bookoasisignore",
            extensions=".cbz,.epub",
        )

        self.assertTrue(created["relevant"])
        self.assertTrue(deleted["relevant"])

    def test_move_event_refreshes_old_and_new_parents(self):
        operations = event_vfs_operations(
            {
                "action": "move",
                "item_type": "file",
                "mapped_path": "/mnt/gds/GDRIVE/new/A.cbz",
                "mapped_removed_path": "/mnt/gds/GDRIVE/old/A.cbz",
            }
        )

        self.assertEqual(
            [
                ("forget", "/mnt/gds/GDRIVE/old/A.cbz", "file"),
                ("refresh", "/mnt/gds/GDRIVE/old", "directory"),
                ("refresh", "/mnt/gds/GDRIVE/new", "directory"),
            ],
            operations,
        )

    def test_batch_deduplicates_vfs_and_library_scan(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = self._database(tempdir)
            rc_client = _FakeRcClient()
            scans = []

            def scan_callback(db_type, library_id, library_name):
                scans.append((db_type, library_id, library_name))
                return {"success": True, "message": "queued"}

            processor = GDriveScanProcessor(
                {
                    "general_db_path": str(db_path),
                    "adult_enabled": False,
                    "gdrive_scan_path_mappings": "/GDRIVE => /mnt/gds/GDRIVE",
                    "gdrive_scan_vfs_rules": (
                        "/mnt/gds/GDRIVE|/GDRIVE|http://127.0.0.1:5572"
                    ),
                },
                scan_callback=scan_callback,
                rc_client=rc_client,
            )
            results = processor.process_batch(
                [
                    {
                        "id": 1,
                        "action": "create",
                        "item_type": "file",
                        "path": "/GDRIVE/READING/만화/A.cbz",
                        "removed_path": "",
                    },
                    {
                        "id": 2,
                        "action": "edit",
                        "item_type": "file",
                        "path": "/GDRIVE/READING/만화/B.cbz",
                        "removed_path": "",
                    },
                ]
            )

        self.assertTrue(results[1]["success"])
        self.assertTrue(results[2]["success"])
        self.assertEqual([("general", 25, "만화")], scans)
        self.assertEqual(
            [("refresh", "/mnt/gds/GDRIVE/READING/만화", "directory")],
            rc_client.calls,
        )

    def test_event_without_matching_library_fails_before_scan(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = self._database(tempdir)
            scans = []
            processor = GDriveScanProcessor(
                {
                    "general_db_path": str(db_path),
                    "adult_enabled": False,
                    "gdrive_scan_path_mappings": "/GDRIVE => /mnt/gds/GDRIVE",
                    "gdrive_scan_vfs_rules": "",
                },
                scan_callback=lambda *args: scans.append(args),
                rc_client=_FakeRcClient(),
            )
            result = processor.process_batch(
                [
                    {
                        "id": 3,
                        "action": "create",
                        "item_type": "file",
                        "path": "/GDRIVE/READING/소설/A.epub",
                        "removed_path": "",
                    }
                ]
            )

        self.assertFalse(result[3]["success"])
        self.assertIn("보관함", result[3]["message"])
        self.assertEqual([], scans)

    def test_missing_vfs_rule_blocks_bookoasis_scan(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = self._database(tempdir)
            scans = []
            processor = GDriveScanProcessor(
                {
                    "general_db_path": str(db_path),
                    "adult_enabled": False,
                    "gdrive_scan_path_mappings": "/GDRIVE => /mnt/gds/GDRIVE",
                    "gdrive_scan_vfs_rules": "",
                },
                scan_callback=lambda *args: scans.append(args),
                rc_client=_FakeRcClient(),
            )
            result = processor.process_batch(
                [
                    {
                        "id": 4,
                        "action": "create",
                        "item_type": "file",
                        "path": "/GDRIVE/READING/만화/A.cbz",
                        "removed_path": "",
                    }
                ]
            )

        self.assertFalse(result[4]["success"])
        self.assertIn("VFS 규칙", result[4]["message"])
        self.assertEqual([], scans)


if __name__ == "__main__":
    unittest.main()
