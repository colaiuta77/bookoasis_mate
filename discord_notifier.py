# Google Drive 변경 이벤트를 Discord 웹훅 배치 요약으로 전송합니다.
import json
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class DiscordWebhookError(RuntimeError):
    pass


class DiscordWebhookNotifier:
    USER_AGENT = "BookOasisMate/1.0"
    MAX_EMBEDS = 10
    MAX_EMBED_CHARACTERS = 5800
    MAX_FIELD_VALUE = 1024
    SIDECAR_NAMES = {
        "comicinfo.xml",
        "kavita.yaml",
        "metadata.json",
    }
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

    def __init__(self, webhook_url, timeout=5, sender=None):
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
                        f"Discord 알림 전송이 HTTP {status}로 실패했습니다."
                    )
        except HTTPError as error:
            raise DiscordWebhookError(
                f"Discord 알림 전송이 HTTP {int(error.code)}로 실패했습니다."
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
        path = str(event.get("path") or event.get("removed_path") or "")
        normalized = path.replace("\\", "/").rstrip("/")
        basename = cls._basename(normalized)
        if basename.lower() in cls.SIDECAR_NAMES:
            parent = normalized.rsplit("/", 1)[0]
            basename = cls._basename(parent)
        elif str(event.get("item_type") or "").lower() != "directory":
            stem, separator, suffix = basename.rpartition(".")
            if separator and stem and suffix:
                basename = stem
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
                "value": self.STATUS_LABELS.get(status, status or "확인 불가"),
                "inline": True,
            }
        )
        scan_modes = []
        for scan in result.get("scans") or []:
            mode = str(scan.get("mode") or "").strip()
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

        return {
            "title": self._book_title(event),
            "color": self.ACTION_COLORS.get(action, self.DEFAULT_COLOR),
            "fields": fields,
            "footer": {"text": "BookOasis Mate · Google Drive 변경 감지"},
        }

    def _content(self, events, statuses, omitted=0):
        actions = Counter(str(event.get("action") or "unknown") for event in events)
        state_counts = Counter(
            str(statuses.get(int(event["id"])) or "failed") for event in events
        )

        action_text = " · ".join(
            f"{self.ACTION_LABELS.get(action, action)} {count}"
            for action, count in sorted(actions.items())
        )
        state_text = " · ".join(
            f"{self.STATUS_LABELS.get(status, status)} {count}"
            for status, count in sorted(state_counts.items())
        )
        lines = [
            "**BookOasis Google Drive 변경 처리**",
            f"총 {len(events)}건 · {action_text or '변경 정보 없음'}",
            state_text or "처리 상태 없음",
        ]
        if omitted:
            lines.append(f"카드 {len(events) - omitted}건 표시 · 외 {omitted}건 생략")
        return "\n".join(lines)[:1900]

    def _payload(self, events, results, statuses):
        embeds = []
        character_count = 0
        for event in events:
            if len(embeds) >= self.MAX_EMBEDS:
                break
            event_id = int(event["id"])
            result = results.get(event_id) or {}
            status = str(statuses.get(event_id) or "failed")
            embed = self._event_embed(event, result, status)
            embed_size = self._embed_text_size(embed)
            if character_count + embed_size > self.MAX_EMBED_CHARACTERS:
                break
            embeds.append(embed)
            character_count += embed_size

        omitted = max(0, len(events) - len(embeds))
        return {
            "content": self._content(events, statuses, omitted),
            "embeds": embeds,
            "allowed_mentions": {"parse": []},
        }

    def send_batch(self, events, results, statuses):
        if not self.webhook_url:
            return {"sent": False, "disabled": True}
        payload = self._payload(events, results, statuses)
        try:
            self.sender(self.webhook_url, payload, self.timeout)
        except DiscordWebhookError:
            raise
        except Exception as error:
            raise DiscordWebhookError(
                f"Discord 알림 전송에 실패했습니다. ({type(error).__name__})"
            ) from None
        return {"sent": True, "disabled": False}
