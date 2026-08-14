# 상태 요약 결과와 원본 DB 지문을 FlaskFarm 전용 DB에 저장합니다.
import json
from datetime import datetime

from .setup import *


class ModelSummarySnapshot(ModelBase):
    P = P
    __tablename__ = "summary_snapshot"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    settings_fingerprint = db.Column(db.String)
    source_fingerprint = db.Column(db.String)
    result_json = db.Column(db.Text)

    @classmethod
    def available(cls):
        return F is not None and getattr(F, "app", None) is not None

    @classmethod
    def store(cls, result, settings_fingerprint, source):
        with F.app.app_context():
            F.db.session.query(cls).delete(synchronize_session=False)
            entity = cls()
            entity.created_at = datetime.now()
            entity.settings_fingerprint = str(settings_fingerprint or "")
            entity.source_fingerprint = str((source or {}).get("fingerprint") or "")
            entity.result_json = json.dumps(result, ensure_ascii=False)
            F.db.session.add(entity)
            F.db.session.commit()
            return entity.to_dict()

    @classmethod
    def latest(cls):
        with F.app.app_context():
            entity = F.db.session.query(cls).order_by(cls.id.desc()).first()
            return entity.to_dict() if entity is not None else None

    @classmethod
    def delete_all(cls):
        with F.app.app_context():
            count = F.db.session.query(cls).delete(synchronize_session=False)
            F.db.session.commit()
            return int(count or 0)

    def to_dict(self):
        try:
            result = json.loads(self.result_json or "{}")
        except (TypeError, ValueError):
            result = {}
        return {
            "id": self.id,
            "created_at": (
                self.created_at.isoformat(timespec="seconds")
                if self.created_at
                else ""
            ),
            "settings_fingerprint": self.settings_fingerprint or "",
            "source_fingerprint": self.source_fingerprint or "",
            "result": result,
        }
