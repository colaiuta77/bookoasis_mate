# BookOasis Mate 자체 버전과 GitHub 변경 내역을 안전하게 조회합니다.
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY_URL = "https://github.com/colaiuta77/bookoasis_mate"
RAW_BASE_URL = "https://raw.githubusercontent.com/colaiuta77/bookoasis_mate/main"
REMOTE_INFO_URL = f"{RAW_BASE_URL}/info.yaml"
REMOTE_README_URL = f"{RAW_BASE_URL}/README.md"
REQUEST_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS = {408, 500, 502, 503, 504}
RETRY_BASE_DELAY = 0.4
REQUEST_TIMEOUT = 6
INFO_MAX_BYTES = 64 * 1024
README_MAX_BYTES = 1024 * 1024
CACHE_SECONDS = 600

_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _version_tuple(value):
    match = re.match(r"^\s*[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value or ""))
    if not match:
        return ()
    return tuple(int(item or 0) for item in match.groups())


def compare_versions(left, right):
    """left < right면 -1, 같으면 0, left > right면 1을 반환합니다."""
    left_tuple = _version_tuple(left)
    right_tuple = _version_tuple(right)
    if not left_tuple or not right_tuple:
        return 0
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


def _parse_yaml_scalar(text, key):
    pattern = rf"^\s*{re.escape(key)}\s*:\s*[\"']?([^\"'\r\n#]+)"
    match = re.search(pattern, str(text or ""), re.MULTILINE)
    return match.group(1).strip() if match else ""


def read_local_version(base_dir=None):
    root = Path(base_dir) if base_dir else Path(__file__).resolve().parent
    try:
        text = (root / "info.yaml").read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    return _parse_yaml_scalar(text, "version")


def _read_limited(url, limit):
    request = Request(
        url,
        headers={
            "Accept": "text/plain, */*",
            "User-Agent": "BookOasis-Mate-Release-Info/1",
        },
    )
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("GitHub 응답이 허용 크기를 초과했습니다.")
            return payload.decode("utf-8-sig")
        except HTTPError as error:
            if error.code not in RETRYABLE_HTTP_STATUS or attempt + 1 >= REQUEST_ATTEMPTS:
                raise
        except (URLError, TimeoutError):
            if attempt + 1 >= REQUEST_ATTEMPTS:
                raise
        time.sleep(RETRY_BASE_DELAY * (2**attempt))
    raise RuntimeError("GitHub 요청 재시도 상태가 올바르지 않습니다.")


def _cached(key, loader, force=False):
    now = time.monotonic()
    if not force:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and now - cached[0] < CACHE_SECONDS:
                return cached[1]
    value = loader()
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), value)
    return value


def release_status(force=False):
    local = read_local_version()

    def load_remote():
        return _parse_yaml_scalar(_read_limited(REMOTE_INFO_URL, INFO_MAX_BYTES), "version")

    remote = _cached("remote_version", load_remote, force=force)
    return {
        "current_version": local,
        "latest_version": remote,
        "update_available": bool(local and remote and compare_versions(local, remote) < 0),
        "repository_url": REPOSITORY_URL,
    }


_RELEASE_HEADER = re.compile(r"^-\s+[vV]([^\s(]+)(?:\s+\(([^)]+)\))?\s*$")
_BULLET = re.compile(r"^\s+-\s+(.+?)\s*$")


def parse_changelog(readme):
    """README의 ## Changelog와 다음 ## 섹션 사이를 구조화합니다."""
    lines = str(readme or "").splitlines()
    in_changelog = False
    releases = []
    current = None
    for raw in lines:
        line = raw.rstrip()
        if not in_changelog:
            if re.match(r"^##\s+Changelog\s*$", line, re.IGNORECASE):
                in_changelog = True
            continue
        if re.match(r"^##\s+", line):
            break
        header = _RELEASE_HEADER.match(line.strip())
        if header:
            current = {
                "version": header.group(1).strip(),
                "date": (header.group(2) or "").strip(),
                "items": [],
            }
            releases.append(current)
            continue
        bullet = _BULLET.match(line)
        if bullet and current is not None:
            text = bullet.group(1).strip()
            if text:
                current["items"].append(text)
    return releases


def fetch_changelog(force=False):
    readme = _cached(
        "readme",
        lambda: _read_limited(REMOTE_README_URL, README_MAX_BYTES),
        force=force,
    )
    releases = parse_changelog(readme)
    if not releases:
        raise ValueError("GitHub README에서 Changelog를 찾지 못했습니다.")
    return {
        "releases": releases,
        "repository_url": REPOSITORY_URL,
        "source_url": f"{REPOSITORY_URL}#changelog",
    }
