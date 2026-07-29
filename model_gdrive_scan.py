# gd-poller 변경 이벤트와 BookOasis 스캔 처리 결과를 FlaskFarm DB에 영속 저장합니다.
import json
from datetime import datetime, timedelta

from .setup import *


class ModelGDriveScanEvent(ModelBase):
    P = P
    __tablename__ = "gdrive_scan_event"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now)
    ready_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    action = db.Column(db.String)
    item_type = db.Column(db.String)
    path = db.Column(db.Text)
    removed_path = db.Column(db.Text)
    status = db.Column(db.String)
    attempts = db.Column(db.Integer, default=0)
    db_type = db.Column(db.String)
    library_id = db.Column(db.Integer)
    library_name = db.Column(db.String)
    mapped_path = db.Column(db.Text)
    result_json = db.Column(db.Text)
    error = db.Column(db.Text)
    TERMINAL_STATUSES = ("completed", "failed")

    @staticmethod
    def _next_ready_at(now, buffer_seconds):
        seconds = max(0, min(int(buffer_seconds or 0), 3600))
        if seconds == 0:
            return now
        remainder = seconds - (int(now.timestamp()) % seconds)
        return now + timedelta(seconds=remainder)

    @classmethod
    def enqueue(cls, event, buffer_seconds=60):
        now = datetime.now()
        entity = cls()
        entity.created_at = now
        entity.updated_at = now
        entity.ready_at = cls._next_ready_at(now, buffer_seconds)
        entity.action = event["action"]
        entity.item_type = event["item_type"]
        entity.path = event["path"]
        entity.removed_path = event.get("removed_path") or ""
        entity.status = "queued"
        entity.attempts = 0
        entity.result_json = "{}"
        entity.error = ""
        with F.app.app_context():
            F.db.session.add(entity)
            F.db.session.commit()
            return entity.to_dict()

    @classmethod
    def recover_processing(cls):
        now = datetime.now()
        with F.app.app_context():
            count = (
                F.db.session.query(cls)
                .filter(cls.status == "processing")
                .update(
                    {
                        cls.status: "retry",
                        cls.ready_at: now,
                        cls.updated_at: now,
                        cls.error: "FlaskFarm 재시작 후 작업을 복구했습니다.",
                    },
                    synchronize_session=False,
                )
            )
            F.db.session.commit()
            return int(count or 0)

    @classmethod
    def claim_ready(cls, limit=500):
        limit = max(1, min(int(limit or 500), 2000))
        now = datetime.now()
        claimed = []
        with F.app.app_context():
            candidates = (
                F.db.session.query(cls)
                .filter(cls.status.in_(("queued", "retry")))
                .filter(cls.ready_at <= now)
                .order_by(cls.ready_at.asc(), cls.id.asc())
                .limit(limit)
                .all()
            )
            for entity in candidates:
                snapshot = entity.to_dict()
                updated = (
                    F.db.session.query(cls)
                    .filter(cls.id == entity.id)
                    .filter(cls.status.in_(("queued", "retry")))
                    .update(
                        {
                            cls.status: "processing",
                            cls.attempts: int(entity.attempts or 0) + 1,
                            cls.updated_at: now,
                        },
                        synchronize_session=False,
                    )
                )
                if updated == 1:
                    snapshot["attempts"] = int(entity.attempts or 0) + 1
                    snapshot["status"] = "processing"
                    claimed.append(snapshot)
            F.db.session.commit()
        return claimed

    @classmethod
    def finish(cls, event_id, result):
        now = datetime.now()
        libraries = result.get("libraries") or []
        first_library = libraries[0] if libraries else {}
        with F.app.app_context():
            (
                F.db.session.query(cls)
                .filter(cls.id == int(event_id))
                .update(
                    {
                        cls.status: "completed",
                        cls.updated_at: now,
                        cls.completed_at: now,
                        cls.db_type: first_library.get("db_type"),
                        cls.library_id: first_library.get("id"),
                        cls.library_name: first_library.get("name"),
                        cls.mapped_path: result.get("mapped_path") or "",
                        cls.result_json: json.dumps(result, ensure_ascii=False),
                        cls.error: "",
                    },
                    synchronize_session=False,
                )
            )
            F.db.session.commit()

    @classmethod
    def fail_or_retry(cls, event, message, max_attempts=3):
        attempts = int(event.get("attempts") or 0)
        max_attempts = max(1, min(int(max_attempts or 3), 20))
        terminal = attempts >= max_attempts
        now = datetime.now()
        delay = min(300, max(5, 5 * (2 ** max(0, attempts - 1))))
        values = {
            cls.status: "failed" if terminal else "retry",
            cls.updated_at: now,
            cls.completed_at: now if terminal else None,
            cls.ready_at: now if terminal else now + timedelta(seconds=delay),
            cls.error: str(message or "변경 이벤트 처리에 실패했습니다.")[:4000],
        }
        with F.app.app_context():
            (
                F.db.session.query(cls)
                .filter(cls.id == int(event["id"]))
                .update(values, synchronize_session=False)
            )
            F.db.session.commit()
        return "failed" if terminal else "retry"

    @classmethod
    def retry(cls, event_id):
        now = datetime.now()
        with F.app.app_context():
            count = (
                F.db.session.query(cls)
                .filter(cls.id == int(event_id))
                .filter(cls.status == "failed")
                .update(
                    {
                        cls.status: "retry",
                        cls.ready_at: now,
                        cls.completed_at: None,
                        cls.updated_at: now,
                        cls.error: "",
                    },
                    synchronize_session=False,
                )
            )
            F.db.session.commit()
            return count == 1

    @classmethod
    def list_page(
        cls,
        page=1,
        page_size=50,
        status="",
        action="",
        db_type="",
        library_id="",
        search="",
        order="desc",
    ):
        page = max(1, int(page or 1))
        page_size = max(10, min(int(page_size or 50), 500))
        status = str(status or "").strip()
        action = str(action or "").strip()
        db_type = str(db_type or "").strip()
        library_id = str(library_id or "").strip()
        search = str(search or "").strip()
        with F.app.app_context():
            query = F.db.session.query(cls)
            selected_library_id = library_id
            selected_db_type = ""
            if library_id not in {"", "unassigned"} and ":" in library_id:
                selected_db_type, selected_library_id = library_id.split(":", 1)
            if status:
                query = query.filter(cls.status == status)
            if action:
                query = query.filter(cls.action == action)
            if selected_db_type or db_type:
                query = query.filter(cls.db_type == (selected_db_type or db_type))
            if library_id == "unassigned":
                query = query.filter(cls.library_id.is_(None))
            elif library_id:
                query = query.filter(cls.library_id == int(selected_library_id))
            if search:
                pattern = f"%{search}%"
                query = query.filter(
                    (cls.path.like(pattern))
                    | (cls.removed_path.like(pattern))
                    | (cls.library_name.like(pattern))
                    | (cls.error.like(pattern))
                )
            total = int(query.count())
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            ordering = cls.id.asc() if str(order).lower() == "asc" else cls.id.desc()
            rows = (
                query.order_by(ordering)
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            return {
                "items": [row.to_dict() for row in rows],
                "page": page,
                "page_size": page_size,
                "pages": pages,
                "total": total,
            }

    @classmethod
    def recent(cls, status="", search="", limit=200):
        return cls.list_page(
            page=1,
            page_size=limit,
            status=status,
            search=search,
        )["items"]

    @classmethod
    def filter_options(cls):
        with F.app.app_context():
            rows = (
                F.db.session.query(cls.db_type, cls.library_id, cls.library_name)
                .filter(cls.library_id.isnot(None))
                .distinct()
                .order_by(cls.db_type.asc(), cls.library_name.asc())
                .all()
            )
            return [
                {
                    "db_type": row[0] or "",
                    "library_id": int(row[1]),
                    "library_name": row[2] or str(row[1]),
                }
                for row in rows
            ]

    @classmethod
    def delete_terminal(cls, event_id):
        with F.app.app_context():
            count = (
                F.db.session.query(cls)
                .filter(cls.id == int(event_id))
                .filter(cls.status.in_(cls.TERMINAL_STATUSES))
                .delete(synchronize_session=False)
            )
            F.db.session.commit()
            return int(count or 0)

    @classmethod
    def cleanup_terminal(cls, retention_days=30, delete_all=False):
        retention_days = max(1, min(int(retention_days or 30), 3650))
        cutoff = datetime.now() - timedelta(days=retention_days)
        with F.app.app_context():
            query = F.db.session.query(cls).filter(
                cls.status.in_(cls.TERMINAL_STATUSES)
            )
            if not delete_all:
                query = query.filter(cls.updated_at < cutoff)
            count = query.delete(synchronize_session=False)
            F.db.session.commit()
            return int(count or 0)

    @classmethod
    def counts(cls):
        statuses = ("queued", "retry", "processing", "completed", "failed")
        with F.app.app_context():
            return {
                status: int(
                    F.db.session.query(cls).filter(cls.status == status).count()
                )
                for status in statuses
            }

    def to_dict(self):
        try:
            result = json.loads(self.result_json or "{}")
        except (TypeError, ValueError):
            result = {}
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(timespec="seconds")
            if self.created_at
            else None,
            "updated_at": self.updated_at.isoformat(timespec="seconds")
            if self.updated_at
            else None,
            "ready_at": self.ready_at.isoformat(timespec="seconds")
            if self.ready_at
            else None,
            "completed_at": self.completed_at.isoformat(timespec="seconds")
            if self.completed_at
            else None,
            "action": self.action,
            "item_type": self.item_type,
            "path": self.path,
            "removed_path": self.removed_path,
            "status": self.status,
            "attempts": int(self.attempts or 0),
            "db_type": self.db_type,
            "library_id": self.library_id,
            "library_name": self.library_name,
            "mapped_path": self.mapped_path,
            "result": result,
            "error": self.error,
        }
