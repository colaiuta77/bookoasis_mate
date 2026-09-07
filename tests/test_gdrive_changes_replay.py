# 실제 SQLite 상태 저장으로 변경 오탐과 장애 후 재생 중복을 검증합니다.
import importlib.util
import sys
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker

from gdrive_changes import GoogleDriveApiError, GoogleDriveChangesClient, GoogleDriveChangesWatcher, build_change_event


class ChangesReplayTest(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        self.session = scoped_session(sessionmaker(bind=self.engine))
        base = declarative_base()
        setup = types.ModuleType("replay_test.setup")
        setup.ModelBase = base
        setup.db = sa
        setup.P = types.SimpleNamespace(package_name="bookoasis_mate")
        setup.F = types.SimpleNamespace(
            app=types.SimpleNamespace(app_context=nullcontext),
            db=types.SimpleNamespace(session=self.session),
        )
        spec = importlib.util.spec_from_file_location(
            "replay_test.model_gdrive_scan", Path(__file__).resolve().parents[1] / "model_gdrive_scan.py"
        )
        self.models = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"replay_test.setup": setup}):
            spec.loader.exec_module(self.models)
        base.metadata.create_all(self.engine)
        self.items = self.models.ModelGDriveItemState
        self.events = self.models.ModelGDriveScanEvent
        self.states = self.models.ModelGDriveScanState
        self.client = GoogleDriveChangesClient("rclone", "test.conf", "drive", "root", "", "/books")
        self.watcher = GoogleDriveChangesWatcher(self.client, self.states, self.items, self.events, ".cbz")
        self.states.save_cursor("drive", "root", "page-1")
        self.file = {"id": "book", "name": "book.cbz", "parents": ["root"],
                     "mimeType": "application/zip", "modifiedTime": "2024-01-01T00:00:00Z",
                     "size": "123", "md5Checksum": "aaa"}

    def tearDown(self):
        self.session.remove()
        self.engine.dispose()

    def poll(self, file=None):
        with patch.object(self.client, "_get", return_value={
            "changes": [{"fileId": "book", "file": file or self.file}], "newStartPageToken": "page-2"
        }):
            return self.watcher.poll_once()

    def test_same_metadata_is_not_edit_but_checksum_change_is(self):
        self.assertEqual(1, self.poll())
        self.assertEqual(0, self.poll())
        self.assertEqual(1, self.poll(dict(self.file, md5Checksum="bbb")))
        self.assertEqual(["create", "edit"], [row.action for row in self.session.query(self.events).all()])

    def test_partial_page_rate_limit_replay_does_not_duplicate_first_file(self):
        data = {"changes": [{"fileId": "book", "file": self.file},
                            {"fileId": "other", "file": dict(self.file, id="other", parents=["missing-parent"])}],
                "nextPageToken": "page-2"}
        with patch.object(self.client, "_get", side_effect=[data, GoogleDriveApiError(403, "userRateLimitExceeded", "limit")]):
            with self.assertRaises(GoogleDriveApiError):
                self.watcher.poll_once()
        self.assertEqual("page-1", self.states.get("drive", "root")["page_token"])
        self.assertEqual(0, self.poll())
        self.assertEqual(1, self.session.query(self.events).count())

    def test_poll_stops_at_page_boundary_and_persists_next_token(self):
        pages = [{"changes": [{"fileId": "book", "file": self.file}], "nextPageToken": "page-2"},
                 {"changes": [], "newStartPageToken": "page-3"}]
        with patch.object(self.client, "_get", side_effect=pages):
            self.assertEqual(1, self.watcher.poll_once())
        self.assertEqual("page-2", self.states.get("drive", "root")["page_token"])

    def test_legacy_state_is_preserved_and_upgraded_without_reset(self):
        self.items.upsert(self.client.item_scope, {"file_id": "book", "path": "/books/book.cbz"})
        self.assertEqual(1, self.poll())
        self.assertEqual(0, self.poll())
        self.assertEqual("page-2", self.states.get("drive", "root")["page_token"])

    def test_rename_and_delete_are_not_suppressed_by_same_content(self):
        self.poll()
        self.assertEqual(1, self.poll(dict(self.file, name="renamed.cbz")))
        with patch.object(self.client, "_get", return_value={"changes": [{"fileId": "book", "removed": True}], "newStartPageToken": "end"}):
            self.assertEqual(1, self.watcher.poll_once())
            self.assertEqual(0, self.watcher.poll_once())
        self.assertEqual(["create", "rename", "delete"], [row.action for row in self.session.query(self.events).all()])

    def test_folder_move_only_relocates_its_own_children(self):
        scope = self.client.item_scope
        folder = {"file_id": "folder", "path": "/books/a_%", "is_directory": True}
        self.items.upsert(scope, folder)
        self.items.upsert(scope, {"file_id": "child", "path": "/books/a_%/child.cbz"})
        self.items.upsert(scope, {"file_id": "other", "path": "/books/abc/child.cbz"})
        current = dict(folder, path="/books/moved")
        self.items.record_change(scope, "folder", folder, current,
                                 build_change_event(folder, current), self.events, 0)
        self.assertEqual("/books/moved/child.cbz", self.items.get(scope, "child")["path"])
        self.assertEqual("/books/abc/child.cbz", self.items.get(scope, "other")["path"])
        self.items.record_change(scope, "folder", current, None,
                                 build_change_event(current, None), self.events, 0)
        self.assertIsNone(self.items.get(scope, "child"))
        self.assertIsNotNone(self.items.get(scope, "other"))

    def test_unchanged_folder_does_not_trigger_scan(self):
        previous = {"path": "/books/folder", "is_directory": True}
        self.assertIsNone(build_change_event(previous, dict(previous)))

    def test_item_write_failure_rolls_back_event_too(self):
        def fail_item_write(session, *args):
            if any(isinstance(row, self.items) for row in session.new | session.dirty):
                raise RuntimeError("simulated item write failure")
        session = self.session()
        sa.event.listen(session, "before_flush", fail_item_write)
        try:
            with self.assertRaisesRegex(RuntimeError, "item write failure"):
                self.poll()
        finally:
            sa.event.remove(session, "before_flush", fail_item_write)
            session.rollback()
        self.assertEqual(0, self.session.query(self.events).count())

    def test_existing_database_schema_upgrade_keeps_rows_and_is_repeatable(self):
        self.items.__table__.drop(self.engine)
        with self.engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE gdrive_item_state (id INTEGER PRIMARY KEY, remote VARCHAR, file_id VARCHAR, parent_id VARCHAR, path TEXT, mime_type VARCHAR, is_directory INTEGER, updated_at DATETIME)"))
            connection.execute(sa.text("INSERT INTO gdrive_item_state (remote,file_id,path) VALUES ('drive:root','book','/books/book.cbz')"))
        self.items.ensure_schema()
        self.items.ensure_schema()
        self.assertEqual("/books/book.cbz", self.items.get("drive:root", "book")["path"])
        self.assertEqual(1, self.poll())
        self.assertEqual(0, self.poll())

    def test_uncertain_scan_is_not_retried_and_keeps_mapped_library(self):
        event = self.events.enqueue({"action": "create", "item_type": "file", "path": "/books/book.cbz"}, 0)
        result = {"success": False, "outcome_unknown": True, "retryable": False,
                  "mapped_path": "/books/book.cbz", "libraries": [{"id": 7, "db_type": "general", "name": "Books"}],
                  "scans": [{"message": "timeout"}]}
        status = self.events.fail_or_retry(event, "결과 확인 필요", result=result)
        self.assertEqual("failed", status)
        self.assertEqual([], self.events.claim_ready())
        saved = self.events.failed(event["id"])
        self.assertEqual(7, saved["library_id"])
        self.assertTrue(saved["result"]["outcome_unknown"])

    def test_clear_pending_preserves_terminal_history_and_checkpoint(self):
        for status in ("queued", "retry", "completed", "failed"):
            row = self.events._new_entity({"action": "edit", "item_type": "file", "path": "/books/book.cbz"})
            row.status = status
            self.session.add(row)
        self.session.commit()
        self.items.upsert("drive:root", {"file_id": "book", "path": "/books/book.cbz"})
        self.assertEqual(2, self.events.clear_pending())
        self.assertEqual(["completed", "failed"], [row.status for row in self.session.query(self.events).order_by(self.events.id)])
        self.assertEqual("page-1", self.states.get("drive", "root")["page_token"])
        self.assertIsNotNone(self.items.get("drive:root", "book"))

    def test_cancelled_last_attempt_returns_to_retry_without_spending_attempt(self):
        row = self.events._new_entity({"action": "edit", "item_type": "file", "path": "/books/book.cbz"})
        row.status, row.attempts = "processing", 3
        self.session.add(row)
        self.session.commit()
        event = row.to_dict()
        self.assertEqual("retry", self.events.fail_or_retry(event, "stop", max_attempts=3, result={"cancelled": True}))
        self.session.expire_all()
        saved = self.session.query(self.events).filter_by(id=event["id"]).one()
        self.assertEqual(2, saved.attempts)
        self.assertEqual(1, self.events.clear_pending())

    def test_stop_during_collection_preserves_cursor_and_replays_without_duplicates(self):
        self.client.should_stop = lambda: False
        original = self.items.record_change
        def record(*args, **kwargs):
            original(*args, **kwargs)
            self.client.should_stop = lambda: True
        with patch.object(self.items, "record_change", side_effect=record):
            self.assertEqual(1, self.poll())
        self.assertEqual("page-1", self.states.get("drive", "root")["page_token"])
        self.client.should_stop = lambda: False
        self.assertEqual(0, self.poll())
        self.assertEqual("page-2", self.states.get("drive", "root")["page_token"])
        self.assertEqual(1, self.session.query(self.events).count())

    def test_clear_pending_refuses_processing_without_deleting_anything(self):
        for status in ("queued", "processing", "retry"):
            row = self.events._new_entity({"action": "edit", "item_type": "file", "path": "/books/book.cbz"})
            row.status = status
            self.session.add(row)
        self.session.commit()
        with self.assertRaisesRegex(ValueError, "처리 중"):
            self.events.clear_pending()
        self.assertEqual(3, self.session.query(self.events).count())


if __name__ == "__main__":
    unittest.main()
