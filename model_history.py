# BookOasis Mate 검사 실행 이력을 FlaskFarm 전용 DB에 저장합니다.
import json
from datetime import datetime

from .setup import *


class ModelScanHistory(ModelBase):
    P = P
    __tablename__ = "scan_history"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    trigger = db.Column(db.String)
    status = db.Column(db.String)
    duration_ms = db.Column(db.Integer)
    total_books = db.Column(db.Integer)
    problem_books = db.Column(db.Integer)
    summary_json = db.Column(db.Text)

    @classmethod
    def create_from_report(cls, report, trigger, duration_ms):
        totals = report.get("totals", {})
        entity = cls()
        entity.created_at = datetime.now()
        entity.trigger = str(trigger or "manual")
        entity.status = str(report.get("status") or "unknown")
        entity.duration_ms = int(duration_ms or 0)
        entity.total_books = int(totals.get("total_books", 0) or 0)
        entity.problem_books = int(totals.get("problem_books", 0) or 0)
        entity.summary_json = json.dumps(report, ensure_ascii=False)
        return entity.save()

    @classmethod
    def recent(cls, limit=100):
        limit = max(1, min(int(limit or 100), 1000))
        with F.app.app_context():
            rows = F.db.session.query(cls).order_by(cls.id.desc()).limit(limit).all()
            return [row.to_dict() for row in rows]

    @classmethod
    def delete_all(cls, day=None):
        with F.app.app_context():
            count = F.db.session.query(cls).delete()
            F.db.session.commit()
            return count

    def to_dict(self):
        try:
            summary = json.loads(self.summary_json or "{}")
        except (TypeError, ValueError):
            summary = {}
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(timespec="seconds") if self.created_at else None,
            "trigger": self.trigger,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "total_books": self.total_books,
            "problem_books": self.problem_books,
            "summary": summary,
        }
