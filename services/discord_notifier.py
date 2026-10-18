# Google Drive 변경 이벤트를 Discord 웹훅 배치 요약으로 전송합니다.
import json
import math
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class DiscordWebhookError(RuntimeError):
    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class DiscordWebhookNotifier:
    USER_AGENT = "BookOasisMate/1.0"
    MAX_EMBEDS = 10
    MAX_EMBED_CHARACTERS = 5800
    MAX_FIELD_VALUE = 1024
    ALLOWED_HOSTS = {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }
    ACTION_LABELS = {
        "create": "생성",
        "move": "이동",
        "rename": "이름변경",
        "delete": "삭제",
        "edit": "수정",
        "restore": "복원",
    }
    STATUS_LABELS = {
        "completed": "완료",
        "retry": "재시도",
        "failed": "실패",
    }
    ACTION_COLORS = {
        "create": 0x57F287,
        "restore": 0x57F287,
        "move": 0x3498DB,
        "rename": 0x3498DB,
        "edit": 0xFEE75C,
        "delete": 0xED4245,
    }
    DEFAULT_COLOR = 0x95A5A6

    def __init__(self, webhook_url, timeout=5, sender=None, source_name="Google Drive"):
        self.source_name = source_name
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout = max(1, min(int(timeout or 5), 30))
        self.sender = sender or self._send
        if self.webhook_url:
            self._validate_url(self.webhook_url)

    @classmethod
    def _validate_url(cls, value):
        parsed = urlsplit(value)
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.hostname not in cls.ALLOWED_HOSTS
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or len(path_parts) < 4
            or path_parts[0:2] != ["api", "webhooks"]
            or not path_parts[2]
            or not path_parts[3]
        ):
            raise DiscordWebhookError("Discord 웹훅 주소 형식이 올바르지 않습니다.")

    @staticmethod
    def _send(url, payload, timeout):
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": DiscordWebhookNotifier.USER_AGENT,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 204) or 204)
                if status < 200 or status >= 300:
                    raise DiscordWebhookError(
                        f"Discord 알림 전송이 HTTP {status}로 실패했습니다.", status_code=status
                    )
                if urlsplit(url).query == 'wait=true':
                    try:
                        data = json.loads(response.read(65537))
                        message_id = str(data.get('id') or '')
                        if not message_id.isdigit():
                            raise ValueError('missing message id')
                        return {'message_id': message_id}
                    except (ValueError, TypeError, AttributeError):
                        raise DiscordWebhookError('Discord 메시지 생성 응답을 확인하지 못했습니다.') from None
        except HTTPError as error:
            delays = []
            try:
                body = json.loads(error.read(65537))
            except (ValueError, TypeError):
                body = {}
            for value in ((error.headers or {}).get('Retry-After'), body.get('retry_after') if isinstance(body, dict) else None):
                try:
                    seconds = float(value)
                    if math.isfinite(seconds) and 0 <= seconds <= 604800:
                        delays.append(seconds)
                except (ValueError, TypeError):
                    pass
            error.close()
            raise DiscordWebhookError(
                f"Discord 알림 전송이 HTTP {int(error.code)}로 실패했습니다.",
                status_code=int(error.code), retry_after=max(delays) if delays else None
            ) from None
        except URLError as error:
            reason = getattr(error, "reason", None)
            reason_type = type(reason).__name__ if reason is not None else "URLError"
            raise DiscordWebhookError(
                f"Discord 알림 연결에 실패했습니다. ({reason_type})"
            ) from None
        except TimeoutError:
            raise DiscordWebhookError(
                "Discord 알림 연결에 실패했습니다. (TimeoutError)"
            ) from None

    @staticmethod
    def _basename(value):
        normalized = str(value or "").replace("\\", "/").rstrip("/")
        return normalized.rsplit("/", 1)[-1] if normalized else ""

    @staticmethod
    def _truncate(value, limit):
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    @classmethod
    def _book_title(cls, event):
        drive = (event.get("result") or {}).get("drive") or {}
        path = str(event.get("path") or event.get("removed_path") or "")
        basename = str(drive.get("name") or cls._basename(path))
        return cls._truncate(basename or "제목 확인 불가", 256)

    @staticmethod
    def _received_at(event):
        value = str(event.get("created_at") or "").strip()
        return value.replace("T", " ", 1) if value else "확인 불가"

    @classmethod
    def _embed_text_size(cls, embed):
        size = len(str(embed.get("title") or ""))
        size += len(str(embed.get("description") or ""))
        footer = embed.get("footer") or {}
        size += len(str(footer.get("text") or ""))
        for field in embed.get("fields") or []:
            size += len(str(field.get("name") or ""))
            size += len(str(field.get("value") or ""))
        return size

    def _event_embed(self, event, result, status):
        action = str(event.get("action") or "unknown").lower()
        path = str(event.get("path") or event.get("removed_path") or "확인 불가")
        fields = [
            {
                "name": "액션",
                "value": self._truncate(action.upper(), self.MAX_FIELD_VALUE),
                "inline": True,
            },
            {
                "name": "수신 시간",
                "value": self._truncate(
                    self._received_at(event), self.MAX_FIELD_VALUE
                ),
                "inline": True,
            },
            {
                "name": "상세 경로",
                "value": self._truncate(path, self.MAX_FIELD_VALUE),
                "inline": False,
            },
        ]
        removed_path = str(event.get("removed_path") or "").strip()
        if action in {"move", "rename"} and removed_path:
            fields.append(
                {
                    "name": "이전 경로",
                    "value": self._truncate(removed_path, self.MAX_FIELD_VALUE),
                    "inline": False,
                }
            )

        drive = (event.get("result") or {}).get("drive") or {}
        # 제목 한도를 넘는 파일명도 필드에서 최대한 표시합니다.
        name = str(drive.get("name") or self._basename(path))
        if len(name) > 256:
            fields.append({"name": "파일명", "value": self._truncate(name, self.MAX_FIELD_VALUE), "inline": False})
        for label, key in (("세부 유형", "action_detail"), ("발생 시각", "occurred_at"), ("ID", "file_id"), ("MIME", "mime_type")):
            if drive.get(key):
                fields.append({"name": label, "value": self._truncate(drive[key], self.MAX_FIELD_VALUE), "inline": False})
        if drive.get("file_id"):
            file_id = quote(str(drive["file_id"]), safe="")
            is_folder = event.get("item_type") == "directory" or drive.get("mime_type") == "application/vnd.google-apps.folder"
            link = f"https://drive.google.com/drive/folders/{file_id}" if is_folder else f"https://drive.google.com/file/d/{file_id}/view"
            fields.append({"name": "Google Drive", "value": self._truncate(f"[Google Drive에서 열기]({link})", self.MAX_FIELD_VALUE), "inline": False})

        libraries = []
        for library in result.get("libraries") or []:
            name = str(library.get("name") or "").strip()
            if name and name not in libraries:
                libraries.append(name)
        if libraries:
            fields.append(
                {
                    "name": "보관함",
                    "value": self._truncate(", ".join(libraries), self.MAX_FIELD_VALUE),
                    "inline": True,
                }
            )

        fields.append(
            {
                "name": "처리 결과",
                "value": "요청 접수 · 실제 스캔 완료 미확인" if status == "completed" and any(
                    scan.get("request_accepted") for scan in result.get("scans") or []
                ) else self.STATUS_LABELS.get(status, status or "확인 불가"),
                "inline": True,
            }
        )
        scan_modes = []
        for scan in result.get("scans") or []:
            mode = str(scan.get("mode") or "").strip()
            if (scan.get("response") or {}).get("cleanup_scope") == "library":
                mode = "library"
            mode = {"library": "보관함 증분 스캔", "full_webhook_compat": "보관함 증분 스캔", "admin_scan_path": "개별 경로 스캔", "path_webhook": "경로 스캔 요청", "path": "경로 스캔 요청"}.get(mode, mode)
            if mode and mode not in scan_modes:
                scan_modes.append(mode)
        if scan_modes:
            fields.append(
                {
                    "name": "스캔 방식",
                    "value": self._truncate(", ".join(scan_modes), self.MAX_FIELD_VALUE),
                    "inline": True,
                }
            )

        reasons = list(dict.fromkeys(scan["fallback_reason"] for scan in result.get("scans") or [] if scan.get("fallback_reason")))
        if reasons:
            fields.append({"name": "전체 스캔 전환 사유", "value": self._truncate(" · ".join(reasons), self.MAX_FIELD_VALUE), "inline": False})
        return {
            "title": self._book_title(event),
            "color": self.ACTION_COLORS.get(action, self.DEFAULT_COLOR),
            "fields": fields,
            "footer": {"text": f"BookOasis Mate · {self.source_name} 변경 감지"},
        }

    def _content(self, events, statuses):
        actions = Counter(str(event.get("action") or "unknown") for event in events)
        state_counts = Counter(
            str(statuses.get(int(event["id"])) or "failed") for event in events
        )

        action_text = " · ".join(
            f"{self.ACTION_LABELS.get(action, action)} {count}건"
            for action, count in sorted(actions.items())
        )
        state_text = " · ".join(
            f"{self.STATUS_LABELS.get(status, status)} {count}건"
            for status, count in sorted(state_counts.items())
        )
        lines = [
            f"**BookOasis {self.source_name} 변경 처리**",
            "```text",
            f"전체  {len(events)}건",
            f"변경  {action_text or '변경 정보 없음'}",
            f"결과  {state_text or '처리 상태 없음'}",
            "```",
        ]
        return "\n".join(lines)[:1900]

    def _payload(self, events, results, statuses):
        return self.build_batch_payload(events, results, statuses)

    def send_batch(self, events, results, statuses):
        if not self.webhook_url:
            return {"sent": False, "disabled": True}
        payloads = self.build_batch_payloads(events, results, statuses)
        for index, payload in enumerate(payloads):
            self.send_payload(payload, position=(index + 1, len(payloads)))
        return {"sent": True, "disabled": False}

    def send_payload(self, payload, wait=False, position=None):
        if not self.webhook_url:
            return {'sent': False, 'disabled': True}
        if position is not None:
            suffix = f'\n메시지 {position[0]}/{position[1]}'
            payload = dict(payload, content=str(payload.get('content') or '')[:2000 - len(suffix)] + suffix)
        try:
            result = self.sender(self.webhook_url + ('?wait=true' if wait else ''), payload, self.timeout)
            if wait and (not isinstance(result, dict) or not result.get('message_id')):
                raise DiscordWebhookError('Discord 메시지 생성 응답을 확인하지 못했습니다.')
            return dict(result or {}, sent=True, disabled=False)
        except DiscordWebhookError:
            raise
        except Exception as error:
            raise DiscordWebhookError(f'Discord 알림 전송에 실패했습니다. ({type(error).__name__})') from None

    def build_batch_payload(self, events, results, statuses):
        payloads = self.build_batch_payloads(events, results, statuses)
        if len(payloads) > 1:
            raise DiscordWebhookError('여러 메시지가 필요합니다. build_batch_payloads로 모든 카드를 발송해 주세요.')
        return payloads[0] if payloads else {'content': self._content([], statuses), 'embeds': [], 'allowed_mentions': {'parse': []}}

    def build_batch_payloads(self, events, results, statuses):
        groups = {}
        for event in events:
            drive = (event.get('result') or {}).get('drive') or {}
            key = tuple(drive.get(name) for name in ('activity_group', 'file_id', 'occurred_at'))
            if all(key):
                groups.setdefault(key, []).append(event)
        hidden = set()
        for group in groups.values():
            primaries = [event for event in group if
                ((event.get('result') or {}).get('drive') or {}).get('activity_primary') is True]
            if len(primaries) == 1:
                hidden.update(int(event['id']) for event in group
                              if event is not primaries[0] and statuses.get(int(event['id'])) == 'completed')
        events = sorted((event for event in events if int(event['id']) not in hidden),
                        key=lambda event: statuses.get(int(event['id'])) != 'failed')
        payloads, batch, embeds, character_count = [], [], [], 0
        for event in events:
            event_id = int(event['id'])
            embed = self._event_embed(event, results.get(event_id) or {}, str(statuses.get(event_id) or 'failed'))
            # 단일 카드가 예산보다 커도 카드는 유지하고 긴 필드부터 줄입니다.
            excess = self._embed_text_size(embed) - self.MAX_EMBED_CHARACTERS
            for field in sorted(embed['fields'], key=lambda field: len(field['value']), reverse=True):
                if excess <= 0:
                    break
                before = len(field['value'])
                field['value'] = self._truncate(field['value'], max(1, before - excess))
                excess -= before - len(field['value'])
            size = self._embed_text_size(embed)
            if embeds and (len(embeds) >= self.MAX_EMBEDS or character_count + size > self.MAX_EMBED_CHARACTERS):
                payloads.append({'content': self._content(batch, statuses), 'embeds': embeds, 'allowed_mentions': {'parse': []}})
                batch, embeds, character_count = [], [], 0
            batch.append(event)
            embeds.append(embed)
            character_count += size
        if embeds:
            payloads.append({'content': self._content(batch, statuses), 'embeds': embeds, 'allowed_mentions': {'parse': []}})
        return payloads

    def build_health_payload(self, display_name, recovered, error_kind):
        state = '복구' if recovered else '오류'
        return {'content': f'**BookOasis Google Drive 감지 {state}**',
                'embeds': [{'title': self._truncate(display_name, 100),
                            'description': '실제 변경 수집에 다시 성공했습니다.' if recovered else self._truncate(error_kind, 100),
                            'color': 0x57F287 if recovered else 0xED4245}],
                'allowed_mentions': {'parse': []}}
