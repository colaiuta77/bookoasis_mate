# 라이브러리 통계 결과와 원본 DB 지문을 FlaskFarm 전용 DB에 저장합니다.
import json
from datetime import datetime

from .setup import *


class ModelLibraryStatisticsSnapshot(ModelBase):
    P = P
    __tablename__ = "library_statistics_snapshot"
    __table_args__ = {"mysql_collate": "utf8_general_ci"}
    __bind_key__ = P.package_name

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    db_type = db.Column(db.String)
    library_id = db.Column(db.String)
    engine = db.Column(db.String)
    database_ref = db.Column(db.Text)
    source_fingerprint = db.Column(db.String)
    source_rows = db.Column(db.Integer)
    result_json = db.Column(db.Text)

    @classmethod
    def available(cls):
        return F is not None and getattr(F, "app", None) is not None

    @classmethod
    def store(cls, result, source):
        db_type = str(result.get("db_type") or "general")
        library_id = str(result.get("library_id") or "")
        with F.app.app_context():
            F.db.session.query(cls).filter(cls.db_type == db_type).filter(
                cls.library_id == library_id
            ).delete(synchronize_session=False)
            entity = cls()
            entity.created_at = datetime.now()
            entity.db_type = db_type
            entity.library_id = library_id
            entity.engine = str(source.get("engine") or result.get("engine") or "")
            entity.database_ref = str(source.get("database") or result.get("database") or "")
            entity.source_fingerprint = str(source.get("fingerprint") or "")
            entity.source_rows = int(source.get("source_rows") or 0)
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
            "db_type": self.db_type,
            "library_id": self.library_id or "",
            "engine": self.engine or "",
            "database": self.database_ref or "",
            "source_fingerprint": self.source_fingerprint or "",
            "source_rows": int(self.source_rows or 0),
            "result": result,
        }
