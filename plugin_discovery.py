# GitHub Topic에서 BookOasis 플러그인 후보를 안전하게 발견하고 캐시합니다.
import ast
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GitHubPluginDiscovery:
    REQUEST_ATTEMPTS = 3
    RETRYABLE_HTTP_STATUS = {408, 500, 502, 503, 504}
    RETRY_BASE_DELAY = 0.5

    def __init__(self, fetch_json=None, fetch_bytes=None):
        self._fetch_json = fetch_json or self._request_json
        self._fetch_bytes = fetch_bytes or self._request_bytes

    @classmethod
    def _request_bytes(cls, url, limit):
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "BookOasis-Mate-Plugin-Discovery/1",
            },
        )
        for attempt in range(cls.REQUEST_ATTEMPTS):
            try:
                with urlopen(request, timeout=12) as response:
                    payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise ValueError("GitHub 응답이 허용 크기를 초과했습니다.")
                return payload
            except HTTPError as error:
                if (
                    error.code not in cls.RETRYABLE_HTTP_STATUS
                    or attempt + 1 >= cls.REQUEST_ATTEMPTS
                ):
                    raise
            except (URLError, TimeoutError):
                if attempt + 1 >= cls.REQUEST_ATTEMPTS:
                    raise
            time.sleep(cls.RETRY_BASE_DELAY * (2**attempt))
        raise RuntimeError("GitHub 요청 재시도 상태가 올바르지 않습니다.")

    @classmethod
    def _request_json(cls, url):
        return json.loads(cls._request_bytes(url, 2 * 1024 * 1024).decode("utf-8-sig"))

    @staticmethod
    def _provider_name(source, plugin_id):
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            values = {}
            for statement in node.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    continue
                target = statement.targets[0]
                value = statement.value
                if isinstance(target, ast.Name) and target.id in {"id", "name"} and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    values[target.id] = value.value.strip()
            if values.get("id") == plugin_id:
                return values.get("name") or plugin_id
        raise ValueError("플러그인 ID 선언을 확인할 수 없습니다.")

    def _validate_repository(self, repository_url, branch, plugin_id):
        owner_repo = repository_url.removeprefix("https://github.com/").strip("/")
        base = f"https://raw.githubusercontent.com/{owner_repo}/{quote(branch, safe='/')}"
        version_payload = self._fetch_bytes(f"{base}/VERSION", 65536)
        version_data = json.loads(version_payload.decode("utf-8-sig"))
        version = str(version_data.get("plugin version") or version_data.get("version") or "") if isinstance(version_data, dict) else str(version_data or "")
        source = self._fetch_bytes(f"{base}/{plugin_id}.py", 524288).decode("utf-8-sig")
        return version.strip(), self._provider_name(source, plugin_id)

    def discover(self, topics):
        seen = set()
        items = []
        for topic in topics:
            topic = str(topic or "").strip().lower()
            if not topic:
                continue
            url = f"https://api.github.com/search/repositories?q=topic%3A{quote(topic, safe='-')}&per_page=100"
            payload = self._fetch_json(url)
            for repository in payload.get("items", []):
                if repository.get("fork") or repository.get("archived") or repository.get("disabled"):
                    continue
                repository_url = str(repository.get("html_url") or "").rstrip("/")
                plugin_id = str(repository.get("name") or "").replace("-", "_")
                key = repository_url.lower()
                if not repository_url.startswith("https://github.com/") or key in seen:
                    continue
                seen.add(key)
                branch = str(repository.get("default_branch") or "main")
                validation_status = "candidate"
                validation_error = ""
                version = ""
                name = plugin_id
                try:
                    version, name = self._validate_repository(repository_url, branch, plugin_id)
                    validation_status = "validated"
                except Exception as error:
                    validation_error = str(error)[:240]
                items.append({
                    "id": plugin_id,
                    "name": name,
                    "description": str(repository.get("description") or "GitHub Topic에서 발견한 BookOasis 플러그인 후보입니다."),
                    "repository": repository_url,
                    "repository_url": repository_url,
                    "source": "github",
                    "ref": branch,
                    "catalog_version": version,
                    "latest_version": version,
                    "validation_status": validation_status,
                    "validation_error": validation_error,
                    "stars": int(repository.get("stargazers_count") or 0),
                    "updated_at": str(repository.get("updated_at") or ""),
                    "discovered": True,
                    "custom": True,
                    "trusted": False,
                })
        items.sort(key=lambda item: (-item["stars"], item["id"]))
        return {"topics": list(topics), "items": items}

    @staticmethod
    def read_cache(path):
        path = Path(path)
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    @staticmethod
    def write_cache(path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)
