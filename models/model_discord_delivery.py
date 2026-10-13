# Discord 발송 대기열과 목적지 제한 및 감지 상태를 영속 보존합니다.
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta

from ..setup import *


@contextmanager
def delivery_session():
    with F.app.app_context():
        try:
            yield F.db.session
            F.db.session.commit()
        except Exception:
            F.db.session.rollback()
            raise


class ModelDiscordDestination(ModelBase):
    __tablename__ = 'discord_destination'
    __bind_key__ = P.package_name
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    P = P
    id = db.Column(db.String(64), primary_key=True)
    cooldown_until = db.Column(db.DateTime)
    blocked = db.Column(db.Integer, default=0)
    error = db.Column(db.Text, default='')


class ModelDiscordHealth(ModelBase):
    __tablename__ = 'discord_health'
    __bind_key__ = P.package_name
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    P = P
    id = db.Column(db.String(64), primary_key=True)
    healthy = db.Column(db.Integer)


class ModelDiscordDelivery(ModelBase):
    __tablename__ = 'discord_delivery'
    __bind_key__ = P.package_name
    __table_args__ = {'mysql_collate': 'utf8_general_ci'}
    P = P
    id = db.Column(db.Integer, primary_key=True)
    destination = db.Column(db.String(64), index=True)
    identity = db.Column(db.String(64), unique=True)
    event_id = db.Column(db.Integer)
    kind = db.Column(db.String, default='result')
    status = db.Column(db.String, index=True)
    snapshot = db.Column(db.Text)
    batch_id = db.Column(db.Integer, index=True)
    payload = db.Column(db.Text)
    created_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime)
    due_at = db.Column(db.DateTime)
    attempts = db.Column(db.Integer, default=0)
    message_id = db.Column(db.String)
    error = db.Column(db.Text, default='')

    @staticmethod
    def fingerprint(value):
        return hashlib.sha256(str(value).encode('utf-8')).hexdigest() if value else ''

    @classmethod
    def enqueue_result(cls, session, event, result, status, destination, now):
        generation = (event.get('result') or {}).get('processing_started_at')
        if not destination or not generation:
            return
        identity = cls.fingerprint(f"{destination}:{event['id']}:{generation}:{status}")
        if session.query(cls).filter(cls.identity == identity).first():
            return
        row = session.query(cls).filter(cls.destination == destination, cls.event_id == event['id'],
            cls.status == 'pending', cls.batch_id.is_(None)).first()
        if row is None:
            row = cls(destination=destination, event_id=event['id'], kind='result',
                      status='pending', created_at=now, attempts=0)
            session.add(row)
        clean_event = {key: str(event.get(key) or '')[:2048]
                       for key in ('action', 'item_type', 'path', 'removed_path', 'created_at')}
        clean_event['id'] = event['id']
        drive = (event.get('result') or {}).get('drive') or {}
        clean_event['result'] = {'drive': {key: str(drive[key])[:1024] for key in
            ('name', 'file_id', 'mime_type', 'action_detail', 'occurred_at') if drive.get(key)}}
        clean_result = {
            'libraries': [{'name': str(item.get('name') or '')[:256]} for item in (result.get('libraries') or [])[:20]],
            'scans': [{'mode': str(item.get('mode') or '')[:64], 'request_accepted': bool(item.get('request_accepted')),
                       'response': {'cleanup_scope': (item.get('response') or {}).get('cleanup_scope') == 'library' and 'library' or ''}}
                      for item in (result.get('scans') or [])[:20]],
        }
        row.identity, row.updated_at, row.due_at = identity, now, now
        row.snapshot = json.dumps({'event': clean_event, 'result': clean_result, 'status': status}, ensure_ascii=False)

    @classmethod
    def claim(cls, now, quiet_seconds, max_wait_seconds, destination):
        if not destination:
            return None
        with delivery_session() as session:
            target = session.query(ModelDiscordDestination).filter_by(id=destination).first()
            if target and (target.blocked or (target.cooldown_until and target.cooldown_until > now)):
                return None
            query = session.query(cls).filter(cls.destination == destination)
            retry = query.filter(cls.status == 'retry', cls.due_at <= now).order_by(cls.id).first()
            if retry:
                rows = query.filter(cls.batch_id == retry.batch_id, cls.status == 'retry').all()
            else:
                rows = query.filter(cls.status == 'pending', cls.kind == 'health').order_by(cls.id).limit(1).all()
                if not rows:
                    rows = query.filter(cls.status == 'pending', cls.kind == 'result').order_by(cls.id).all()
                    if not rows:
                        return None
                    deadline = min(max(row.updated_at for row in rows) + timedelta(seconds=quiet_seconds),
                                   min(row.created_at for row in rows) + timedelta(seconds=max_wait_seconds))
                    if now < deadline:
                        return None
            batch_id = rows[0].batch_id or rows[0].id
            ids = [row.id for row in rows]
            changed = session.query(cls).filter(cls.id.in_(ids), cls.status.in_(('pending', 'retry'))).update(
                {cls.status: 'sending', cls.batch_id: batch_id}, synchronize_session=False)
            if changed != len(ids):
                raise RuntimeError('Discord 발송 대기열 소유권이 변경되었습니다.')
            return {'id': batch_id, 'kind': rows[0].kind, 'attempts': rows[0].attempts or 0,
                    'payload': json.loads(rows[0].payload) if rows[0].payload else None,
                    'items': [json.loads(row.snapshot) for row in rows]}

    @classmethod
    def save_payload(cls, delivery_id, payload):
        with delivery_session() as session:
            session.query(cls).filter(cls.id == delivery_id).update({cls.payload: json.dumps(payload, ensure_ascii=False)})

    @classmethod
    def complete(cls, delivery_id, message_id, now):
        with delivery_session() as session:
            session.query(cls).filter(cls.batch_id == delivery_id, cls.status == 'sending').update(
                {cls.status: 'sent', cls.message_id: message_id, cls.updated_at: now, cls.error: ''})

    @classmethod
    def reschedule(cls, delivery_id, due_at, error, count_failure):
        with delivery_session() as session:
            session.query(cls).filter(cls.batch_id == delivery_id, cls.status == 'sending').update({
                cls.status: 'retry', cls.due_at: due_at, cls.error: error[:500],
                cls.attempts: cls.attempts + int(count_failure)})

    @classmethod
    def fail(cls, delivery_id, error, now):
        with delivery_session() as session:
            session.query(cls).filter(cls.batch_id == delivery_id, cls.status == 'sending').update(
                {cls.status: 'failed', cls.updated_at: now, cls.error: error[:500]})

    @classmethod
    def recover(cls, now):
        with delivery_session() as session:
            session.query(cls).filter(cls.status == 'sending').update({cls.status: 'retry', cls.due_at: now})

    @classmethod
    def set_destination_limit(cls, destination, until=None, blocked=False, error=''):
        with delivery_session() as session:
            row = session.query(ModelDiscordDestination).filter_by(id=destination).first()
            if row is None:
                row = ModelDiscordDestination(id=destination)
                session.add(row)
            row.cooldown_until, row.blocked, row.error = until, int(blocked), error[:500]

    @classmethod
    def cancel_pending(cls, destination, now):
        with delivery_session() as session:
            session.query(cls).filter(cls.destination == destination, cls.status.in_(('pending', 'retry', 'sending'))).update(
                {cls.status: 'cancelled', cls.updated_at: now})

    @classmethod
    def reconcile_destination(cls, destination, now):
        with delivery_session() as session:
            session.query(cls).filter(cls.destination != destination, cls.status.in_(('pending', 'retry', 'sending'))).update(
                {cls.status: 'cancelled', cls.updated_at: now})

    @classmethod
    def cleanup(cls, now):
        with delivery_session() as session:
            session.query(cls).filter(cls.status.in_(('sent', 'cancelled', 'failed')),
                cls.updated_at < now - timedelta(days=7)).delete(synchronize_session=False)

    @classmethod
    def queue_status(cls, destination):
        with delivery_session() as session:
            query = session.query(cls).filter(cls.destination == destination)
            target = session.query(ModelDiscordDestination).filter_by(id=destination).first()
            recent = query.filter(cls.error != '').order_by(cls.id.desc()).first()
            return {'pending': query.filter(cls.status.in_(('pending', 'retry', 'sending'))).count(),
                    'failed': query.filter(cls.status == 'failed').count(),
                    'blocked': bool(target and target.blocked),
                    'last_error': (target.error if target and target.error else recent.error if recent else '')}
