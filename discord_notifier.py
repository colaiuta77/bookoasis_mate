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

    def _content(self, events, results, statuses):
        actions = Counter(str(event.get("action") or "unknown") for event in events)
        state_counts = Counter(str(statuses.get(int(event["id"])) or "failed") for event in events)
        libraries = set()
        scan_modes = set()
        for result in results.values():
            for library in result.get("libraries") or []:
                name = str(library.get("name") or "").strip()
                if name:
                    libraries.add(name)
            for scan in result.get("scans") or []:
                mode = str(scan.get("mode") or "").strip()
                if mode:
                    scan_modes.add(mode)

        action_text = " · ".join(
            f"{self.ACTION_LABELS.get(action, action)} {count}"
            for action, count in sorted(actions.items())
        )
        state_text = " · ".join(
            f"{self.STATUS_LABELS.get(status, status)} {count}"
            for status, count in sorted(state_counts.items())
        )
        samples = []
        for event in events:
            name = self._basename(event.get("path") or event.get("removed_path"))
            if name and name not in samples:
                samples.append(name)
            if len(samples) >= 5:
                break

        lines = [
            "**BookOasis Google Drive 변경 처리**",
            f"총 {len(events)}건 · {action_text or '변경 정보 없음'}",
            state_text or "처리 상태 없음",
            "대상 보관함: " + (", ".join(sorted(libraries)) or "미분류"),
            "스캔 방식: " + (", ".join(sorted(scan_modes)) or "스캔 없음"),
        ]
        if samples:
            lines.append("경로 표본: " + ", ".join(samples))
        return "\n".join(lines)[:1900]

    def send_batch(self, events, results, statuses):
        if not self.webhook_url:
            return {"sent": False, "disabled": True}
        payload = {"content": self._content(events, results, statuses)}
        try:
            self.sender(self.webhook_url, payload, self.timeout)
        except DiscordWebhookError:
            raise
        except Exception as error:
            raise DiscordWebhookError(
                f"Discord 알림 전송에 실패했습니다. ({type(error).__name__})"
            ) from None
        return {"sent": True, "disabled": False}
