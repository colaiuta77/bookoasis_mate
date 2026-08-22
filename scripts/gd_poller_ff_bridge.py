# gd-poller CommandDispatcher 인자를 FlaskFarm BookOasis Mate 이벤트 API로 전달합니다.
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "http://127.0.0.1:9999/bookoasis_mate/api/gdrive_scan/event"
MAX_RESPONSE_BYTES = 1024 * 1024


def _read_json_response(response, max_bytes=MAX_RESPONSE_BYTES):
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"FlaskFarm 응답 크기가 허용 한도 {max_bytes}바이트를 초과했습니다.")
    return json.loads(payload.decode("utf-8") or "{}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="gd-poller 변경 이벤트를 BookOasis Mate에 전달합니다."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("BOOKOASIS_MATE_FF_URL", DEFAULT_URL),
        help="FlaskFarm BookOasis Mate 이벤트 API URL",
    )
    parser.add_argument(
        "--apikey",
        default=os.environ.get("BOOKOASIS_MATE_FF_APIKEY", ""),
        help="FlaskFarm 전역 API 키",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="HTTP 요청 제한 시간",
    )
    parser.add_argument("action", help="create/edit/move/rename/restore/delete")
    parser.add_argument("item_type", help="file 또는 directory")
    parser.add_argument("path", help="현재 경로")
    parser.add_argument("removed_path", nargs="?", default="", help="이전 경로")
    return parser


def post_event(url, apikey, action, item_type, path, removed_path="", timeout=10):
    payload = {
        "apikey": str(apikey or ""),
        "action": str(action or ""),
        "item_type": str(item_type or ""),
        "path": str(path or ""),
        "removed_path": str(removed_path or ""),
    }
    request = Request(
        str(url or "").strip(),
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(1, min(int(timeout or 10), 60))) as response:
            result = _read_json_response(response)
    except HTTPError as error:
        try:
            result = _read_json_response(error)
            message = result.get("msg") or result.get("message")
        except (ValueError, OSError):
            message = None
        raise RuntimeError(message or f"FlaskFarm HTTP 오류 {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"FlaskFarm 연결 실패: {error.reason}") from error
    except (ValueError, OSError) as error:
        raise RuntimeError(f"FlaskFarm 응답 처리 실패: {error}") from error
    if result.get("ret") not in {"success", True}:
        raise RuntimeError(
            result.get("msg") or result.get("message") or "FlaskFarm 이벤트 접수 실패"
        )
    return result


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.apikey:
        print("FlaskFarm API 키가 비어 있습니다.", file=sys.stderr)
        return 2
    try:
        result = post_event(
            args.url,
            args.apikey,
            args.action,
            args.item_type,
            args.path,
            args.removed_path,
            args.timeout,
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "accepted": bool(result.get("accepted")),
                "ignored": bool(result.get("ignored")),
                "id": result.get("id"),
                "ready_at": result.get("ready_at"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
