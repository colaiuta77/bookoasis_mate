# Activity 페이지 재시도와 SQLite 원자적 중복 제거 및 경로 변환을 검증합니다.
import json
import unittest
from unittest.mock import patch

from gdrive_activity import GoogleDriveActivityClient
from gdrive_changes import GoogleDriveChangesWatcher, GoogleDriveApiError, parse_builtin_roots
import test_gdrive_changes_replay as replay


class ActivityTest(unittest.TestCase):
    def setUp(self):
        replay.ChangesReplayTest.setUp(self)
        self.client = GoogleDriveActivityClient("rclone", "test.conf", "drive", "root", "", "/books")
        self.watcher = GoogleDriveChangesWatcher(self.client, self.states, self.items, self.events, ".cbz")
        self.cursor = json.dumps({"start": "2026-09-07T01:00:00.000000+00:00", "floor": "2026-09-07T01:00:00.000000+00:00",
                                  "end": "2026-09-07T01:10:00.000000+00:00"})
        self.states.save_cursor(self.client.state_remote, "root", self.cursor)

    def tearDown(self):
        replay.ChangesReplayTest.tearDown(self)

    def activity(self, kind="edit", detail=None, file_id="book"):
        return {"actions": [{"detail": {kind: detail or {}}, "timestamp": "2026-09-07T01:08:00Z",
                              "target": {"driveItem": {"name": "items/" + file_id, "title": "book.cbz", "driveFile": {}}}}]}

    def file_lookup(self, file_id):
        return {"id": "root", "mimeType": "application/vnd.google-apps.folder"} if file_id == "root" else self.file

    def poll(self, payload):
        with patch.object(self.client, "_query", return_value=payload), patch.object(self.client, "file", side_effect=self.file_lookup):
            return self.watcher.poll_once()

    def test_overlap_replay_and_fixed_page_window(self):
        payload = {"activities": [self.activity()], "nextPageToken": "page-two"}
        self.assertEqual(1, self.poll(payload))
        saved = json.loads(self.states.get(self.client.state_remote, "root")["page_token"])
        self.assertEqual("page-two", saved["page"])
        self.assertEqual(json.loads(self.cursor)["end"], saved["end"])
        self.assertEqual(0, self.poll({"activities": [self.activity()]}))
        self.assertEqual(1, self.session.query(self.events).count())
        self.assertEqual("2026-09-07T01:05:00.000000+00:00", json.loads(self.states.get(self.client.state_remote, "root")["page_token"])["start"])

    def test_shared_delete_targets_are_recorded_once(self):
        targets = []
        for file_id in ("first", "second"):
            self.items.upsert(self.client.item_scope, {"file_id": file_id, "path": "/books/" + file_id + ".cbz"})
            targets.append({"driveItem": {"name": "items/" + file_id, "driveFile": {}}})
        payload = {"activities": [{"actions": [{"detail": {"delete": {"type": "TRASH"}}}],
                                   "targets": targets, "timestamp": "2026-09-07T01:08:00Z"}]}
        with patch.object(self.client, "_query", return_value=payload), patch.object(
            self.client, "file", side_effect=GoogleDriveApiError(404, "notFound", "gone")
        ):
            self.assertEqual(2, self.watcher.poll_once())
            self.assertEqual(0, self.watcher.poll_once())
        rows = self.session.query(self.events).all()
        self.assertEqual({"/books/first.cbz", "/books/second.cbz"}, {row.path for row in rows})
        self.assertEqual(["delete", "delete"], [row.action for row in rows])

    def test_explicit_target_does_not_expand_to_shared_targets(self):
        activity = self.activity()
        activity["targets"] = [{"driveItem": {"name": "items/other"}}]
        self.assertEqual(1, self.poll({"activities": [activity]}))
        self.assertIsNone(self.items.get(self.client.item_scope, "other"))

    def test_missing_all_targets_preserves_checkpoint(self):
        payload = {"activities": [{"actions": [{"detail": {"delete": {}}}],
                                   "timestamp": "2026-09-07T01:08:00Z"}]}
        with self.assertRaisesRegex(RuntimeError, "체크포인트"):
            self.poll(payload)
        self.assertEqual(self.cursor, self.states.get(self.client.state_remote, "root")["page_token"])

    def test_partial_failure_keeps_cursor_and_deduplicates(self):
        payload = {"activities": [self.activity(), self.activity(file_id="other")]}
        def lookup(file_id):
            if file_id == "other":
                raise GoogleDriveApiError(429, "RESOURCE_EXHAUSTED", "quota")
            return self.file_lookup(file_id)
        with patch.object(self.client, "_query", return_value=payload), patch.object(self.client, "file", side_effect=lookup):
            with self.assertRaises(GoogleDriveApiError):
                self.watcher.poll_once()
        self.assertEqual(self.cursor, self.states.get(self.client.state_remote, "root")["page_token"])
        self.assertEqual(0, self.poll({"activities": [self.activity()]}))
        self.assertEqual(1, self.session.query(self.events).count())

    def test_delete_replay_uses_receipt_after_item_removed(self):
        self.items.upsert(self.client.item_scope, {"file_id": "book", "path": "/books/book.cbz"})
        with patch.object(self.client, "_query", return_value={"activities": [self.activity("delete")]}), patch.object(
            self.client, "file", side_effect=GoogleDriveApiError(404, "notFound", "gone")
        ):
            self.assertEqual(1, self.watcher.poll_once())
            self.assertEqual(0, self.watcher.poll_once())
        self.assertIsNone(self.items.get(self.client.item_scope, "book"))
        self.assertEqual("delete", self.session.query(self.events).one().action)

    def test_missing_delete_path_does_not_advance_cursor(self):
        with patch.object(self.client, "_query", return_value={"activities": [self.activity("delete")]}), patch.object(
            self.client, "file", side_effect=GoogleDriveApiError(404, "notFound", "gone")
        ):
            with self.assertRaisesRegex(RuntimeError, "체크포인트"):
                self.watcher.poll_once()
        self.assertEqual(self.cursor, self.states.get(self.client.state_remote, "root")["page_token"])

    def test_rename_restores_old_title_without_cached_file(self):
        self.assertEqual(1, self.poll({"activities": [self.activity("rename", {"oldTitle": "old.cbz", "newTitle": "book.cbz"})]}))
        row = self.session.query(self.events).one()
        self.assertEqual("rename", row.action)
        self.assertEqual("/books/old.cbz", row.removed_path)

    def test_scopes_are_required_and_actual_activity_api_is_probed(self):
        with patch.object(self.client, "file", side_effect=self.file_lookup), patch.object(self.client, "_run_json", return_value={"drive": {"scope": "drive.readonly"}}):
            with self.assertRaisesRegex(ValueError, "drive.activity.readonly"):
                self.client.validate_root()
        with patch.object(self.client, "file", side_effect=self.file_lookup), patch.object(self.client, "_run_json", return_value={"drive": {"scope": "drive.readonly,drive.activity.readonly"}}), patch.object(self.client, "_query", return_value={}) as query:
            self.client.validate_root()
            self.assertEqual("items/root", query.call_args.args[0]["ancestorName"])

    def test_mode_defaults_and_rejects_invalid_or_overlapping_roots(self):
        row = {"remote": "drive", "root_id": "root", "local_root": "/books"}
        self.assertEqual("changes", parse_builtin_roots([row])[0]["detection_mode"])
        with self.assertRaises(ValueError):
            parse_builtin_roots([dict(row, detection_mode="invalid")])
        with self.assertRaises(ValueError):
            parse_builtin_roots([row, dict(row, detection_mode="activity")])

    def test_reset_preserves_changes_cursor(self):
        self.poll({"activities": [self.activity()]})
        self.watcher.reset()
        self.assertEqual("page-1", self.states.get("drive", "root")["page_token"])
        self.assertEqual(0, self.session.query(self.items).filter(self.items.remote == self.client.item_scope + ":receipts").count())

    def test_expired_page_replays_same_time_window(self):
        cursor = json.loads(self.cursor)
        cursor["page"] = "expired"
        with patch.object(self.client, "_query", side_effect=[GoogleDriveApiError(400, "INVALID_ARGUMENT", "page"), {}]) as query:
            output = list(self.client.list_changes(json.dumps(cursor)))
        self.assertEqual(2, query.call_count)
        self.assertNotIn("pageToken", query.call_args.args[0])
        self.assertIn(cursor["end"], query.call_args.args[0]["filter"])
        self.assertEqual(1, len(output))

    def test_event_failure_rolls_back_receipt_and_file(self):
        with patch.object(self.client, "_query", return_value={"activities": [self.activity()]}), patch.object(self.client, "file", side_effect=self.file_lookup), patch.object(self.events, "_new_entity", side_effect=RuntimeError("save failed")):
            with self.assertRaisesRegex(RuntimeError, "save failed"):
                self.watcher.poll_once()
        self.assertEqual(0, self.session.query(self.items).count())
        self.assertEqual(1, self.poll({"activities": [self.activity()]}))

    def test_move_out_preserves_removed_parent_path(self):
        action = self.activity("move", {"removedParents": [{"driveItem": {"name": "items/root"}}]})
        def lookup(file_id):
            if file_id == "book":
                return dict(self.file, parents=["outside"])
            return {"id": file_id, "name": "outside", "parents": []}
        with patch.object(self.client, "_query", return_value={"activities": [action]}), patch.object(self.client, "file", side_effect=lookup):
            self.assertEqual(1, self.watcher.poll_once())
        row = self.session.query(self.events).one()
        self.assertEqual("delete", row.action)
        self.assertEqual("/books/book.cbz", row.removed_path)


if __name__ == "__main__":
    unittest.main()
