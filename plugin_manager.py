# BookOasis 네이티브 플러그인의 카탈로그 조회와 안전한 설치 작업을 관리합니다.
import ast
import copy
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

try:
    from .plugin_discovery import GitHubPluginDiscovery
except ImportError:
    from plugin_discovery import GitHubPluginDiscovery


class PluginManagerError(RuntimeError):
    pass


class PluginManagerStopped(PluginManagerError):
    pass


class BookOasisPluginManager:
    CATALOG = (
        {
            "id": "activity",
            "name": "Activity",
            "description": "사용자별 최근 열람 도서와 진행률을 전용 Activity 탭에 표시합니다.",
            "repository": "https://github.com/colaiuta77/activity",
            "ref": "main",
            "catalog_version": "1.3.0",
            "dependencies": [],
        },
        {
            "id": "activity_desk",
            "name": "Activity Desk",
            "description": "Activity 데이터를 공통 플러그인 데스크의 세로형 위젯으로 표시합니다.",
            "repository": "https://github.com/colaiuta77/activity_desk",
            "ref": "main",
            "catalog_version": "1.2.0",
            "dependencies": ["activity>=1.3.0"],
        },
        {
            "id": "achievements",
            "name": "Achievements",
            "description": "사용자 독서 기록을 집계해 단계별 독서 업적과 진행 상황을 표시합니다.",
            "repository": "https://github.com/colaiuta77/achievements",
            "ref": "main",
            "catalog_version": "1.1.0",
            "dependencies": [],
        },
        {
            "id": "naverkakaoridi",
            "name": "네카리 웹북 검색",
            "description": "네이버·카카오·리디·노벨피아·문피아의 웹툰과 웹소설 메타데이터를 통합 검색해 도서에 적용합니다.",
            "repository": "https://github.com/javara999/naverkakaoridi",
            "ref": "main",
            "catalog_version": "1.6.3",
            "dependencies": [],
        },
        {
            "id": "extract_isbn",
            "name": "ISBN 추출기",
            "description": "EPUB·PDF·TXT의 판권지 구간을 분석해 ISBN을 찾고, 필요한 경우 Gemini 또는 LiteLLM으로 보조 판독합니다.",
            "repository": "https://github.com/yume-script/extract_isbn",
            "ref": "main",
            "catalog_version": "1.3.2",
            "dependencies": [],
        },
        {
            "id": "unified_book",
            "name": "통합 도서 검색",
            "description": "알라딘·국립중앙도서관·Google 도서 정보를 병렬 검색하고 ISBN과 서지 메타데이터를 조합해 적용합니다.",
            "repository": "https://github.com/yume-script/unified_book",
            "ref": "main",
            "catalog_version": "1.6.3",
            "dependencies": [],
        },
        {
            "id": "ssn_rating",
            "name": "소설넷 평점",
            "description": "소설넷의 공개 작품 페이지에서 평균 평점과 최고 평점 리뷰를 가져와 BookOasis 평점과 작품 설명에 적용합니다.",
            "repository": "https://github.com/javara999/ssn_rating",
            "ref": "main",
            "catalog_version": "1.0.4",
            "dependencies": [],
            "ignored_archive_files": ["ssn_rating.zip"],
        },
        {
            "id": "pixiv_ranking",
            "name": "Pixiv 랭킹",
            "description": "Pixiv의 일간·주간·월간 등 랭킹을 콘텐츠 유형별 카드로 보여주는 카테고리 전용 탭입니다.",
            "repository": "https://github.com/yume-script/pixiv_ranking",
            "ref": "main",
            "catalog_version": "1.0.1",
            "dependencies": [],
        },
        {
            "id": "plugin_manager",
            "name": "BookOasis 플러그인 매니저",
            "description": "BookOasis 웹 UI에서 플러그인의 ZIP·Git 설치, 업데이트, 삭제, 활성화와 설정 관리를 지원합니다.",
            "repository": "https://github.com/madnite1/plugin_manager",
            "ref": "main",
            "catalog_version": "1.0.0",
            "dependencies": [],
        },
        {
            "id": "plugin_board",
            "name": "플러그인 목록장",
            "description": "BookOasis 플러그인 GitHub 저장소를 실시간 정보와 함께 카드로 보여주는 안내용 카테고리 탭입니다.",
            "repository": "https://github.com/yume-script/plugin_board",
            "ref": "main",
            "catalog_version": "1.0.0",
            "dependencies": [],
        },
        {
            "id": "my_reading_summary",
            "name": "내 독서 요약",
            "description": "개인별 독서 캘린더와 일간·월간 통계, 연속 독서일, 완독 현황을 전용 탭과 대시보드 위젯으로 표시합니다.",
            "repository": "https://github.com/grandfoxx/my_reading_summary",
            "ref": "master",
            "catalog_version": "1.1.0",
            "dependencies": [],
        },
    )
    PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
    GITHUB_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
    GITHUB_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,160}$")
    IGNORED_NAMES = {
        ".git",
        ".github",
        ".agents",
        ".codex",
        ".pytest_cache",
        "__pycache__",
        "docs",
        "test",
        "tests",
    }
    REJECTED_SUFFIXES = {
        ".7z",
        ".bat",
        ".cmd",
        ".dll",
        ".exe",
        ".gz",
        ".p12",
        ".pem",
        ".pfx",
        ".ps1",
        ".pth",
        ".sh",
        ".so",
        ".tar",
        ".tgz",
        ".zip",
    }
    ACTIVE_STATES = {"ready", "running", "stopping"}
    MAX_LOGS = 200
    DOWNLOAD_TIMEOUT = 30
    MAX_CUSTOM_CATALOG_ITEMS = 100
    PROTECTED_PLUGIN_IDS = {"bookoasis_mate"}

    def __init__(self, logger=None):
        self.logger = logger
        self._lock = threading.RLock()
        self._job = None
        self._stop_event = threading.Event()
        self._prepared = {}
        self._discovery = GitHubPluginDiscovery()
        self._discovery_job = {"status": "idle", "message": "", "error": "", "started_at": "", "finished_at": ""}
        self._installed_update_job = {
            "status": "idle",
            "message": "",
            "error": "",
            "started_at": "",
            "finished_at": "",
        }

    @staticmethod
    def _as_int(value, default, minimum, maximum):
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    @classmethod
    def normalized_settings(cls, values):
        raw = dict(values or {})
        root = str(raw.get("bookoasis_root_path") or "").strip()
        plugin_root = str(raw.get("plugin_manager_plugins_path") or "").strip()
        if not plugin_root and root:
            plugin_root = str(Path(root) / "plugins" / "metadata")
        work_dir = str(raw.get("plugin_manager_work_dir") or "").strip()
        if not work_dir and root:
            work_dir = str(Path(root) / "temp" / "bookoasis_mate_plugins")
        if not work_dir and plugin_root:
            work_dir = str(Path(plugin_root).parent / ".bookoasis_mate_plugins")
        return {
            "plugin_root": plugin_root,
            "work_dir": work_dir,
            "backup_keep": cls._as_int(
                raw.get("plugin_manager_backup_keep"), 5, 1, 30
            ),
            "max_archive_bytes": cls._as_int(
                raw.get("plugin_manager_max_archive_mb"), 100, 1, 2048
            )
            * 1024
            * 1024,
            "max_extracted_bytes": cls._as_int(
                raw.get("plugin_manager_max_extracted_mb"), 500, 10, 4096
            )
            * 1024
            * 1024,
            "max_files": cls._as_int(
                raw.get("plugin_manager_max_files"), 2000, 10, 20000
            ),
            "discovery_topics": [
                value.strip().lower()
                for value in re.split(r"[,\r\n]+", str(raw.get("plugin_manager_discovery_topics") or "bookoasis-plugin"))
                if value.strip()
            ][:5],
            "discovery_cache_hours": cls._as_int(
                raw.get("plugin_manager_discovery_cache_hours"), 6, 1, 168
            ),
        }

    @classmethod
    def _validate_plugin_id(cls, value):
        plugin_id = str(value or "").strip().lower().replace("-", "_")
        if not cls.PLUGIN_ID_PATTERN.fullmatch(plugin_id):
            raise PluginManagerError(
                "플러그인 ID는 영문 소문자로 시작하고 영문 소문자·숫자·밑줄만 사용할 수 있습니다."
            )
        return plugin_id

    @classmethod
    def parse_github_url(cls, value):
        parsed = urlparse(str(value or "").strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.query
            or parsed.fragment
        ):
            raise PluginManagerError(
                "공개 https://github.com/<소유자>/<저장소> 주소만 사용할 수 있습니다."
            )
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) != 2:
            raise PluginManagerError(
                "GitHub 저장소 루트 주소를 입력해 주세요."
            )
        owner, repository = parts
        repository = repository[:-4] if repository.lower().endswith(".git") else repository
        if not cls.GITHUB_COMPONENT_PATTERN.fullmatch(owner) or not cls.GITHUB_COMPONENT_PATTERN.fullmatch(repository):
            raise PluginManagerError("GitHub 소유자 또는 저장소 이름이 올바르지 않습니다.")
        return owner, repository

    @classmethod
    def _validate_ref(cls, value):
        ref = str(value or "main").strip() or "main"
        if not cls.GITHUB_REF_PATTERN.fullmatch(ref) or ".." in ref or ref.startswith("/"):
            raise PluginManagerError("GitHub 브랜치·태그 형식이 올바르지 않습니다.")
        return ref

    @staticmethod
    def _version_tuple(value):
        match = re.match(r"^\s*[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(value or ""))
        return tuple(int(item or 0) for item in match.groups()) if match else ()

    @classmethod
    def _read_version(cls, plugin_dir):
        version_path = Path(plugin_dir) / "VERSION"
        if version_path.is_file():
            try:
                data = json.loads(version_path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return str(data.get("plugin version") or data.get("version") or "").strip()
                return str(data or "").strip()
            except Exception:
                pass
        for source_path in sorted(Path(plugin_dir).glob("*.py")):
            try:
                source = source_path.read_text(encoding="utf-8-sig")
            except Exception:
                continue
            match = re.search(
                r"^\s*PLUGIN_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
                source,
                re.MULTILINE,
            )
            if match:
                return match.group(1).strip()
        return ""

    @staticmethod
    def _safe_resolve(value, label, create=False):
        if not str(value or "").strip():
            raise PluginManagerError(f"{label}을 설정해 주세요.")
        path = Path(str(value)).expanduser().resolve(strict=False)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if not path.exists() or not path.is_dir():
            raise PluginManagerError(f"{label}을 찾을 수 없습니다: {path}")
        return path

    def effective_paths(self, settings):
        normalized = self.normalized_settings(settings)
        return {
            "plugin_root": normalized["plugin_root"],
            "work_dir": normalized["work_dir"],
        }

    def _discovery_cache_path(self, settings, create=False):
        normalized = self.normalized_settings(settings)
        if not normalized["work_dir"]:
            if create:
                raise PluginManagerError("플러그인 작업 디렉터리를 설정해 주세요.")
            return None
        root = Path(normalized["work_dir"]).expanduser().resolve(strict=False)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root / "github_discovery_cache.json"

    def _installed_update_cache_path(self, settings, create=False):
        normalized = self.normalized_settings(settings)
        if not normalized["work_dir"]:
            if create:
                raise PluginManagerError("플러그인 작업 디렉터리를 설정해 주세요.")
            return None
        root = Path(normalized["work_dir"]).expanduser().resolve(strict=False)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root / "installed_update_cache.json"

    def _read_installed_update_cache(self, settings):
        path = self._installed_update_cache_path(settings)
        if path is None or not path.is_file() or path.is_symlink():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_installed_update_cache(self, settings, payload):
        path = self._installed_update_cache_path(settings, create=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _discovered_catalog_items(self, settings):
        path = self._discovery_cache_path(settings)
        payload = self._discovery.read_cache(path) if path else {}
        result = []
        for raw in payload.get("items", []):
            if not isinstance(raw, dict):
                continue
            try:
                owner, repository_name = self.parse_github_url(raw.get("repository"))
                plugin_id = self._validate_plugin_id(raw.get("id") or repository_name)
                ref = self._validate_ref(raw.get("ref"))
            except PluginManagerError:
                continue
            result.append(
                {
                    "id": plugin_id,
                    "name": str(raw.get("name") or plugin_id).strip(),
                    "description": str(
                        raw.get("description")
                        or "GitHub Topic에서 확인한 BookOasis 플러그인입니다."
                    ).strip(),
                    "repository": f"https://github.com/{owner}/{repository_name}",
                    "ref": ref,
                    "catalog_version": str(raw.get("catalog_version") or "").strip(),
                    "dependencies": [],
                    "discovered": True,
                    "verified": True,
                    "trusted": True,
                }
            )
        return result

    def _known_catalog_items(self, settings, include_discovery=True):
        by_id = {item["id"]: copy.deepcopy(item) for item in self.CATALOG}
        for item in self._load_custom_catalog(settings):
            by_id.setdefault(item["id"], copy.deepcopy(item))
        if include_discovery:
            for item in self._discovered_catalog_items(settings):
                by_id.setdefault(item["id"], item)
        return by_id

    def discovery(self, settings):
        path = self._discovery_cache_path(settings)
        payload = self._discovery.read_cache(path) if path else {}
        installed = self._installed_catalog(settings)
        items = []
        for raw in payload.get("items", []):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            item = copy.deepcopy(raw)
            local = installed.get(item["id"]) or {}
            item.update(local)
            item["installed"] = bool(local)
            item["installed_version"] = local.get("installed_version", "")
            items.append(item)
        fetched_epoch = float(payload.get("fetched_at_epoch") or 0)
        max_age = self.normalized_settings(settings)["discovery_cache_hours"] * 3600
        return {
            "items": items,
            "topics": payload.get("topics", []),
            "fetched_at": payload.get("fetched_at", ""),
            "stale": not fetched_epoch or time.time() - fetched_epoch > max_age,
        }

    def discovery_status(self):
        with self._lock:
            return copy.deepcopy(self._discovery_job)

    def start_discovery_refresh(self, settings):
        with self._lock:
            if self._discovery_job.get("status") == "running":
                return copy.deepcopy(self._discovery_job)
            self._discovery_job = {
                "status": "running",
                "message": "GitHub Topic에서 플러그인 후보를 찾고 있습니다.",
                "error": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
            }
        snapshot = dict(settings or {})
        threading.Thread(
            target=self._run_discovery_refresh,
            args=(snapshot,),
            name="bookoasis-plugin-discovery",
            daemon=True,
        ).start()
        return self.discovery_status()

    def _run_discovery_refresh(self, settings):
        try:
            normalized = self.normalized_settings(settings)
            payload = self._discovery.discover(normalized["discovery_topics"])
            now = time.time()
            payload.update({
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "fetched_at_epoch": now,
            })
            self._discovery.write_cache(self._discovery_cache_path(settings, create=True), payload)
            with self._lock:
                self._discovery_job.update({
                    "status": "completed",
                    "message": f"GitHub 플러그인 후보 {len(payload.get('items', []))}개를 확인했습니다.",
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception as error:
            with self._lock:
                self._discovery_job.update({
                    "status": "failed",
                    "message": "GitHub Topic 발견 작업에 실패했습니다.",
                    "error": str(error)[:500],
                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })

    def _custom_catalog_path(self, settings, create=False):
        normalized = self.normalized_settings(settings)
        if not normalized["work_dir"]:
            if create:
                raise PluginManagerError("플러그인 작업 디렉터리를 설정해 주세요.")
            return None
        if create:
            work_root = self._safe_resolve(
                normalized["work_dir"], "플러그인 작업 디렉터리", create=True
            )
        else:
            work_root = Path(normalized["work_dir"]).expanduser().resolve(strict=False)
            if work_root.exists() and not work_root.is_dir():
                raise PluginManagerError("플러그인 작업 디렉터리가 디렉터리가 아닙니다.")
        return work_root / "custom_catalog.json"

    @classmethod
    def _normalize_custom_catalog_item(cls, value):
        if not isinstance(value, dict):
            raise PluginManagerError("사용자 카탈로그 항목 형식이 올바르지 않습니다.")
        owner, repository_name = cls.parse_github_url(value.get("repository"))
        repository = f"https://github.com/{owner}/{repository_name}"
        plugin_id = cls._validate_plugin_id(
            value.get("id") or repository_name.lower().replace("-", "_")
        )
        ref = cls._validate_ref(value.get("ref"))
        name = str(value.get("name") or repository_name).strip()
        description = str(
            value.get("description")
            or "사용자가 등록한 공개 GitHub BookOasis 플러그인입니다."
        ).strip()
        if not name or len(name) > 80:
            raise PluginManagerError("표시명은 1~80자로 입력해 주세요.")
        if len(description) > 500:
            raise PluginManagerError("플러그인 소개는 500자 이하로 입력해 주세요.")
        verified_value = value.get("verified", False)
        verified = verified_value is True or str(verified_value).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return {
            "id": plugin_id,
            "name": name,
            "description": description,
            "repository": repository,
            "ref": ref,
            "catalog_version": "",
            "dependencies": [],
            "custom": True,
            "verified": verified,
        }

    def _load_custom_catalog(self, settings):
        path = self._custom_catalog_path(settings)
        if path is None:
            return []
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise PluginManagerError("사용자 카탈로그 파일 형식이 올바르지 않습니다.")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PluginManagerError(
                f"사용자 카탈로그 파일을 읽을 수 없습니다: {error}"
            ) from error
        if not isinstance(payload, list):
            raise PluginManagerError("사용자 카탈로그 파일은 JSON 배열이어야 합니다.")
        if len(payload) > self.MAX_CUSTOM_CATALOG_ITEMS:
            raise PluginManagerError("사용자 카탈로그 항목 수가 허용 범위를 초과했습니다.")
        result = []
        seen_ids = set()
        for raw in payload:
            item = self._normalize_custom_catalog_item(raw)
            if item["id"] in seen_ids:
                raise PluginManagerError(
                    f"사용자 카탈로그에 중복 플러그인 ID가 있습니다: {item['id']}"
                )
            seen_ids.add(item["id"])
            result.append(item)
        return result

    def _write_custom_catalog(self, settings, items):
        path = self._custom_catalog_path(settings, create=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(items, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def save_custom_catalog_item(
        self, repository, ref, plugin_id, name, description, verified, settings
    ):
        item = self._normalize_custom_catalog_item(
            {
                "repository": repository,
                "ref": ref,
                "id": plugin_id,
                "name": name,
                "description": description,
                "verified": verified,
            }
        )
        builtin_ids = {value["id"] for value in self.CATALOG}
        if item["id"] in builtin_ids:
            raise PluginManagerError("기본 카탈로그와 같은 플러그인 ID는 등록할 수 없습니다.")
        builtin_repositories = {
            value["repository"].lower() for value in self.CATALOG
        }
        if item["repository"].lower() in builtin_repositories:
            raise PluginManagerError("기본 카탈로그에 이미 등록된 GitHub 저장소입니다.")
        with self._lock:
            items = self._load_custom_catalog(settings)
            if len(items) >= self.MAX_CUSTOM_CATALOG_ITEMS:
                raise PluginManagerError(
                    f"사용자 카탈로그는 최대 {self.MAX_CUSTOM_CATALOG_ITEMS}개까지 등록할 수 있습니다."
                )
            for existing in items:
                if existing["id"] == item["id"]:
                    raise PluginManagerError("같은 플러그인 ID가 이미 등록되어 있습니다.")
                if existing["repository"].lower() == item["repository"].lower():
                    raise PluginManagerError("같은 GitHub 저장소가 이미 등록되어 있습니다.")
            items.append(item)
            self._write_custom_catalog(settings, items)
        return copy.deepcopy(item)

    def delete_custom_catalog_item(self, plugin_id, settings):
        plugin_id = self._validate_plugin_id(plugin_id)
        with self._lock:
            items = self._load_custom_catalog(settings)
            remaining = [item for item in items if item["id"] != plugin_id]
            if len(remaining) == len(items):
                raise PluginManagerError("삭제할 사용자 카탈로그 항목을 찾을 수 없습니다.")
            self._write_custom_catalog(settings, remaining)
        return {"id": plugin_id, "deleted": True}

    def _installed_catalog(self, settings):
        normalized = self.normalized_settings(settings)
        root_value = normalized["plugin_root"]
        result = {}
        if not root_value:
            return result
        root = Path(root_value).expanduser().resolve(strict=False)
        if not root.is_dir():
            return result
        for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            plugin_id = child.name.lower()
            if (
                not child.is_dir()
                or child.is_symlink()
                or not self.PLUGIN_ID_PATTERN.fullmatch(plugin_id)
            ):
                continue
            init_path = child / "__init__.py"
            if not init_path.is_file():
                continue
            result[plugin_id] = {
                "id": plugin_id,
                "installed": True,
                "installed_version": self._read_version(child),
                "path": str(child),
                "managed": any(
                    "update_manifest" in source.read_text(encoding="utf-8-sig", errors="ignore")
                    for source in child.glob("*.py")
                ),
            }
        return result

    def installed(self, settings):
        catalog_by_id = self._known_catalog_items(settings)
        update_items = self._read_installed_update_cache(settings).get("items") or {}
        result = []
        for plugin_id, installed in self._installed_catalog(settings).items():
            item = dict(catalog_by_id.get(plugin_id) or {})
            item.update(installed)
            item.setdefault("name", plugin_id)
            item.setdefault("description", "카탈로그에 등록되지 않은 로컬 플러그인입니다.")
            item.setdefault("repository", "")
            remote = update_items.get(plugin_id) or {}
            if remote.get("name"):
                item["name"] = remote["name"]
            latest = str(remote.get("version") or "").strip()
            item["latest_version"] = latest
            item["version_error"] = str(remote.get("error") or "")
            item["update_available"] = bool(
                latest
                and installed.get("installed_version")
                and self._version_tuple(installed["installed_version"])
                < self._version_tuple(latest)
            )
            result.append(item)
        return result

    def installed_update_status(self, settings):
        cache = self._read_installed_update_cache(settings)
        fetched_epoch = float(cache.get("fetched_at_epoch") or 0)
        max_age = self.normalized_settings(settings)["discovery_cache_hours"] * 3600
        with self._lock:
            status = copy.deepcopy(self._installed_update_job)
        status["fetched_at"] = str(cache.get("fetched_at") or "")
        status["stale"] = not fetched_epoch or time.time() - fetched_epoch > max_age
        if status["status"] == "idle" and fetched_epoch:
            status["status"] = "completed"
        return status

    def start_installed_update_refresh(self, settings, force=False):
        current = self.installed_update_status(settings)
        with self._lock:
            if self._installed_update_job.get("status") == "running":
                return copy.deepcopy(current)
            if not force and not current["stale"]:
                return current
            self._installed_update_job = {
                "status": "running",
                "message": "설치 플러그인의 최신 버전을 확인하고 있습니다.",
                "error": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
            }
            snapshot = copy.deepcopy(self._installed_update_job)
        threading.Thread(
            target=self._run_installed_update_refresh,
            args=(dict(settings or {}),),
            name="bookoasis-installed-plugin-update-check",
            daemon=True,
        ).start()
        snapshot["stale"] = current["stale"]
        snapshot["fetched_at"] = current.get("fetched_at", "")
        return snapshot

    def _run_installed_update_refresh(self, settings):
        errors = {}
        remote_items = {}
        try:
            installed_ids = set(self._installed_catalog(settings))
            catalog_by_id = self._known_catalog_items(settings)
            targets = [
                item
                for plugin_id, item in catalog_by_id.items()
                if plugin_id in installed_ids and item.get("repository")
            ]
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(self._fetch_remote_metadata, item): item["id"]
                    for item in targets
                }
                for future, plugin_id in futures.items():
                    try:
                        remote_items[plugin_id] = future.result()
                    except Exception as error:
                        errors[plugin_id] = str(error)[:240]
                        remote_items[plugin_id] = {"version": "", "name": ""}
            for plugin_id, error in errors.items():
                remote_items[plugin_id]["error"] = error
            now = time.time()
            payload = {
                "items": remote_items,
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "fetched_at_epoch": now,
            }
            self._write_installed_update_cache(settings, payload)
            with self._lock:
                self._installed_update_job.update(
                    {
                        "status": "completed",
                        "message": (
                            f"설치 플러그인 {len(targets)}개의 최신 버전을 확인했습니다."
                            + (f" · 실패 {len(errors)}개" if errors else "")
                        ),
                        "error": "",
                        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        except Exception as error:
            with self._lock:
                self._installed_update_job.update(
                    {
                        "status": "failed",
                        "message": "설치 플러그인 업데이트 확인에 실패했습니다.",
                        "error": str(error)[:500],
                        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

    @staticmethod
    def _dependency_id(value):
        match = re.fullmatch(
            r"([a-z][a-z0-9_]{1,63})(?:>=(\d+(?:\.\d+){0,2}))?",
            str(value or "").strip(),
        )
        return match.group(1) if match else ""

    def _installed_dependents(self, plugin_id, settings):
        installed_ids = set(self._installed_catalog(settings))
        catalog_items = [copy.deepcopy(item) for item in self.CATALOG]
        catalog_items.extend(self._load_custom_catalog(settings))
        dependents = []
        for item in catalog_items:
            if item.get("id") not in installed_ids:
                continue
            dependency_ids = {
                self._dependency_id(value) for value in item.get("dependencies") or []
            }
            if plugin_id in dependency_ids:
                dependents.append(str(item.get("id") or ""))
        return sorted(value for value in dependents if value)

    def delete_installed_plugin(self, plugin_id, settings):
        plugin_id = self._validate_plugin_id(plugin_id)
        if plugin_id in self.PROTECTED_PLUGIN_IDS:
            raise PluginManagerError("BookOasis Mate 자체 플러그인은 이 화면에서 삭제할 수 없습니다.")
        normalized = self.normalized_settings(settings)
        with self._lock:
            if self._job and self._job.get("status") in self.ACTIVE_STATES:
                raise PluginManagerError("다른 플러그인 작업이 진행 중입니다.")
            root_input = Path(normalized["plugin_root"]).expanduser()
            if root_input.is_symlink():
                raise PluginManagerError("심볼릭 링크 plugins/metadata 경로에서는 삭제할 수 없습니다.")
            plugin_root = self._safe_resolve(
                normalized["plugin_root"], "BookOasis plugins/metadata 경로"
            )
            work_root = self._safe_resolve(
                normalized["work_dir"], "플러그인 작업 디렉터리", create=True
            )
            try:
                work_root.relative_to(plugin_root)
                raise PluginManagerError(
                    "플러그인 작업 디렉터리는 plugins/metadata 경로 밖에 설정해 주세요."
                )
            except ValueError:
                pass
            target = plugin_root / plugin_id
            if target.is_symlink():
                raise PluginManagerError("심볼릭 링크 플러그인은 삭제할 수 없습니다.")
            if not target.exists() or not target.is_dir() or not (target / "__init__.py").is_file():
                raise PluginManagerError("삭제할 설치 플러그인을 찾을 수 없습니다.")
            resolved_target = target.resolve()
            if resolved_target.parent != plugin_root:
                raise PluginManagerError("plugins/metadata 바로 아래 플러그인만 삭제할 수 있습니다.")
            dependents = self._installed_dependents(plugin_id, settings)
            if dependents:
                raise PluginManagerError(
                    "이 플러그인을 사용하는 설치 플러그인을 먼저 삭제해 주세요: "
                    + ", ".join(dependents)
                )
            version = self._read_version(target)
            job_id = uuid.uuid4().hex
            backup = self._backup_existing(
                target,
                plugin_id,
                work_root,
                normalized["backup_keep"],
                job_id,
            )
            shutil.rmtree(target)
            if target.exists():
                raise PluginManagerError("플러그인 폴더 삭제를 완료하지 못했습니다.")
            finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
            result = {
                "plugin_id": plugin_id,
                "version": version,
                "deleted": True,
                "backup": backup,
                "restart_recommended": True,
            }
            self._record_history(
                normalized,
                {
                    "id": job_id,
                    "operation": "delete",
                    "source": "installed",
                    "status": "completed",
                    "message": "설치 플러그인을 백업한 뒤 삭제했습니다.",
                    "result": result,
                    "started_at": finished_at,
                    "finished_at": finished_at,
                },
            )
            return result

    @staticmethod
    def _fetch_remote_version(item):
        owner, repository = BookOasisPluginManager.parse_github_url(item["repository"])
        ref = BookOasisPluginManager._validate_ref(item.get("ref"))
        url = f"https://raw.githubusercontent.com/{owner}/{repository}/{quote(ref, safe='/')}/VERSION"
        request = Request(url, headers={"User-Agent": "BookOasis-Mate-Plugin-Manager/1"})
        with urlopen(request, timeout=8) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise PluginManagerError("원격 VERSION 파일이 허용 크기를 초과했습니다.")
        data = json.loads(payload.decode("utf-8-sig"))
        if isinstance(data, dict):
            return str(data.get("plugin version") or data.get("version") or "").strip()
        return str(data or "").strip()

    @staticmethod
    def _extract_provider_name(source, plugin_id):
        try:
            tree = ast.parse(str(source or ""))
        except (SyntaxError, TypeError, ValueError):
            return ""
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            values = {}
            for statement in node.body:
                if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                    continue
                target = statement.targets[0]
                value = statement.value
                if (
                    isinstance(target, ast.Name)
                    and target.id in {"id", "name"}
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    values[target.id] = value.value.strip()
            if values.get("id") != plugin_id:
                continue
            name = values.get("name", "")
            if not name or len(name) > 200 or any(ord(char) < 32 for char in name):
                return ""
            return name
        return ""

    @classmethod
    def _fetch_remote_metadata(cls, item):
        version = cls._fetch_remote_version(item)
        name = ""
        try:
            owner, repository = cls.parse_github_url(item["repository"])
            ref = cls._validate_ref(item.get("ref"))
            plugin_id = cls._validate_plugin_id(item.get("id"))
            source_url = (
                f"https://raw.githubusercontent.com/{owner}/{repository}/"
                f"{quote(ref, safe='/')}/{plugin_id}.py"
            )
            request = Request(
                source_url,
                headers={"User-Agent": "BookOasis-Mate-Plugin-Manager/1"},
            )
            with urlopen(request, timeout=8) as response:
                payload = response.read(524289)
            if len(payload) <= 524288:
                name = cls._extract_provider_name(
                    payload.decode("utf-8-sig"),
                    plugin_id,
                )
        except Exception:
            name = ""
        return {"version": version, "name": name}

    def catalog(self, settings, refresh_remote=False):
        installed = self._installed_catalog(settings)
        catalog_items = [copy.deepcopy(item) for item in self.CATALOG]
        catalog_items.extend(self._load_custom_catalog(settings))
        remote_metadata = {}
        remote_errors = {}
        if refresh_remote:
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = {
                    executor.submit(self._fetch_remote_metadata, item): item["id"]
                    for item in catalog_items
                }
                for future, plugin_id in futures.items():
                    try:
                        remote_metadata[plugin_id] = future.result()
                    except Exception as error:
                        remote_errors[plugin_id] = str(error)[:240]
        result = []
        for original in catalog_items:
            item = copy.deepcopy(original)
            local = installed.get(item["id"]) or {}
            item.update(local)
            item["installed"] = bool(local)
            item["installed_version"] = local.get("installed_version", "")
            remote = remote_metadata.get(item["id"]) or {}
            item["latest_version"] = remote.get("version") or item["catalog_version"]
            if remote.get("name"):
                item["name"] = remote["name"]
            item["version_error"] = remote_errors.get(item["id"], "")
            item["trusted"] = (
                bool(item.get("verified"))
                if item.get("custom")
                else True
            )
            item["update_available"] = bool(
                item["installed_version"]
                and self._version_tuple(item["installed_version"])
                < self._version_tuple(item["latest_version"])
            )
            result.append(item)
        return result

    def status(self):
        with self._lock:
            return copy.deepcopy(self._job) if self._job else self._empty_status()

    @staticmethod
    def _empty_status():
        return {
            "id": "",
            "operation": "",
            "status": "idle",
            "status_label": "대기",
            "message": "실행 중인 플러그인 작업이 없습니다.",
            "percent": 0,
            "logs": [],
            "result": None,
            "error": "",
            "started_at": "",
            "finished_at": "",
            "can_install": False,
        }

    def _set_job(self, job_id, **values):
        with self._lock:
            if self._job and self._job.get("id") == job_id:
                self._job.update(values)

    def _append_log(self, job_id, message):
        line = str(message or "").strip()
        if not line:
            return
        with self._lock:
            if not self._job or self._job.get("id") != job_id:
                return
            self._job.setdefault("logs", []).append(
                {"time": time.strftime("%H:%M:%S"), "message": line[:1000]}
            )
            self._job["logs"] = self._job["logs"][-self.MAX_LOGS :]

    def _check_stop(self):
        if self._stop_event.is_set():
            raise PluginManagerStopped("사용자가 작업 중지를 요청했습니다.")

    def stop(self):
        with self._lock:
            if not self._job or self._job.get("status") not in self.ACTIVE_STATES:
                return {"requested": False, "message": "실행 중인 작업이 없습니다."}
            self._job["status"] = "stopping"
            self._job["status_label"] = "중지 요청"
            self._job["message"] = "안전한 단계에서 작업을 중지하고 있습니다."
            self._stop_event.set()
            return {"requested": True, "message": "작업 중지를 요청했습니다."}

    def _begin_job(self, operation, source, runner, args):
        with self._lock:
            if self._job and self._job.get("status") in self.ACTIVE_STATES:
                raise PluginManagerError("다른 플러그인 작업이 진행 중입니다.")
            job_id = uuid.uuid4().hex
            self._stop_event.clear()
            self._job = {
                "id": job_id,
                "operation": operation,
                "source": source,
                "status": "ready",
                "status_label": "대기",
                "message": "플러그인 작업을 준비하고 있습니다.",
                "percent": 0,
                "logs": [],
                "result": None,
                "error": "",
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "can_install": False,
            }
            snapshot = copy.deepcopy(self._job)
        thread = threading.Thread(
            target=runner,
            args=(job_id,) + tuple(args),
            daemon=True,
            name="bookoasis-mate-plugin-manager",
        )
        thread.start()
        return snapshot

    def _catalog_item(self, plugin_id, settings=None):
        plugin_id = self._validate_plugin_id(plugin_id)
        catalog = (
            self._known_catalog_items(settings)
            if settings is not None
            else {item["id"]: copy.deepcopy(item) for item in self.CATALOG}
        )
        if plugin_id in catalog:
            item = copy.deepcopy(catalog[plugin_id])
            item["trusted"] = bool(
                item.get("trusted")
                or item.get("verified")
                or not item.get("custom")
            )
            return item
        raise PluginManagerError("카탈로그에서 플러그인을 찾을 수 없습니다.")

    def _validate_catalog_dependencies(self, item, settings):
        installed = self._installed_catalog(settings)
        missing = []
        outdated = []
        for dependency in item.get("dependencies") or []:
            match = re.fullmatch(
                r"([a-z][a-z0-9_]{1,63})(?:>=(\d+(?:\.\d+){0,2}))?",
                str(dependency or "").strip(),
            )
            if not match:
                raise PluginManagerError(
                    f"카탈로그 의존성 형식이 올바르지 않습니다: {dependency}"
                )
            dependency_id, minimum_version = match.groups()
            local = installed.get(dependency_id)
            if not local:
                missing.append(dependency_id)
                continue
            installed_version = local.get("installed_version") or ""
            if minimum_version and (
                not self._version_tuple(installed_version)
                or self._version_tuple(installed_version)
                < self._version_tuple(minimum_version)
            ):
                outdated.append(
                    f"{dependency_id} {installed_version or '버전 미상'} → {minimum_version} 이상"
                )
        if missing:
            raise PluginManagerError(
                "필수 플러그인을 먼저 설치해 주세요: " + ", ".join(missing)
            )
        if outdated:
            raise PluginManagerError(
                "필수 플러그인을 먼저 업데이트해 주세요: " + ", ".join(outdated)
            )

    def start_catalog_install(self, plugin_id, settings):
        item = self._catalog_item(plugin_id, settings)
        self._validate_catalog_dependencies(item, settings)
        spec = {
            "kind": "github",
            "repository": item["repository"],
            "ref": item["ref"],
            "plugin_id": item["id"],
            "trusted": bool(item.get("trusted")),
            "ignored_archive_files": list(item.get("ignored_archive_files") or []),
        }
        return self._begin_job(
            "install",
            item["repository"],
            self._run_source_job,
            (spec, self.normalized_settings(settings), True),
        )

    def start_github_inspect(self, repository, ref, plugin_id, settings):
        owner, repo = self.parse_github_url(repository)
        normalized_url = f"https://github.com/{owner}/{repo}"
        spec = {
            "kind": "github",
            "repository": normalized_url,
            "ref": self._validate_ref(ref),
            "plugin_id": self._validate_plugin_id(plugin_id or repo),
            "trusted": False,
        }
        self._discard_prepared()
        return self._begin_job(
            "inspect",
            normalized_url,
            self._run_source_job,
            (spec, self.normalized_settings(settings), False),
        )

    def _save_upload(self, storage, settings):
        if storage is None or not getattr(storage, "filename", ""):
            raise PluginManagerError("검사할 ZIP 파일을 선택해 주세요.")
        normalized = self.normalized_settings(settings)
        work_root = self._safe_resolve(normalized["work_dir"], "플러그인 작업 디렉터리", create=True)
        upload_root = work_root / "uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        target = upload_root / f"{uuid.uuid4().hex}.zip"
        total = 0
        stream = storage.stream
        try:
            stream.seek(0)
        except Exception:
            pass
        with target.open("wb") as output:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > normalized["max_archive_bytes"]:
                    raise PluginManagerError("업로드 ZIP 파일이 설정한 최대 크기를 초과했습니다.")
                output.write(chunk)
        if total == 0:
            target.unlink(missing_ok=True)
            raise PluginManagerError("업로드 ZIP 파일이 비어 있습니다.")
        return target, Path(storage.filename).name

    def start_zip_inspect(self, storage, plugin_id, settings):
        archive_path, filename = self._save_upload(storage, settings)
        try:
            spec = {
                "kind": "zip",
                "archive_path": str(archive_path),
                "filename": filename,
                "plugin_id": self._validate_plugin_id(
                    plugin_id or Path(filename).stem
                ),
                "trusted": False,
            }
            self._discard_prepared()
            return self._begin_job(
                "inspect",
                filename,
                self._run_source_job,
                (spec, self.normalized_settings(settings), False),
            )
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    def start_prepared_install(self, inspect_job_id, settings):
        key = str(inspect_job_id or "").strip()
        with self._lock:
            prepared = copy.deepcopy(self._prepared.get(key))
        if not prepared:
            raise PluginManagerError("설치 가능한 검사 결과가 없거나 만료되었습니다.")
        candidate = Path(prepared["candidate"])
        if not candidate.is_dir():
            raise PluginManagerError("검사한 임시 플러그인 파일을 찾을 수 없습니다.")
        return self._begin_job(
            "install",
            prepared["source"],
            self._run_prepared_install,
            (key, prepared, self.normalized_settings(settings)),
        )

    def _download_github(self, spec, target, settings, job_id):
        owner, repository = self.parse_github_url(spec["repository"])
        ref = self._validate_ref(spec.get("ref"))
        url = f"https://codeload.github.com/{owner}/{repository}/zip/{quote(ref, safe='/')}"
        request = Request(url, headers={"User-Agent": "BookOasis-Mate-Plugin-Manager/1"})
        self._append_log(job_id, f"GitHub {owner}/{repository}@{ref} 아카이브를 내려받고 있습니다.")
        total = 0
        with urlopen(request, timeout=self.DOWNLOAD_TIMEOUT) as response, target.open("wb") as output:
            final_host = urlparse(response.geturl()).hostname
            if final_host not in {"codeload.github.com", "github.com"}:
                raise PluginManagerError("허용되지 않은 호스트로 리다이렉트되었습니다.")
            while True:
                self._check_stop()
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > settings["max_archive_bytes"]:
                    raise PluginManagerError("GitHub 아카이브가 설정한 최대 크기를 초과했습니다.")
                output.write(chunk)
        if total == 0:
            raise PluginManagerError("GitHub에서 빈 아카이브를 받았습니다.")

    @classmethod
    def _validate_archive_member(cls, info, ignored_root_files=None):
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise PluginManagerError(f"ZIP에 허용되지 않은 경로가 있습니다: {info.filename}")
        if any(":" in part for part in path.parts):
            raise PluginManagerError(f"ZIP에 허용되지 않은 경로가 있습니다: {info.filename}")
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type == stat.S_IFLNK:
            raise PluginManagerError(f"ZIP의 심볼릭 링크는 허용하지 않습니다: {info.filename}")
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise PluginManagerError(f"ZIP의 특수 파일은 허용하지 않습니다: {info.filename}")
        ignored_root_files = {
            str(value or "").strip()
            for value in (ignored_root_files or [])
            if str(value or "").strip()
        }
        if (
            not info.is_dir()
            and len(path.parts) == 2
            and path.name in ignored_root_files
        ):
            return None
        if not info.is_dir() and path.suffix.lower() in cls.REJECTED_SUFFIXES:
            raise PluginManagerError(f"ZIP 안의 중첩 압축·실행 파일은 허용하지 않습니다: {info.filename}")
        return path

    def _extract_archive(
        self, archive_path, destination, settings, job_id, ignored_root_files=None
    ):
        try:
            archive = zipfile.ZipFile(archive_path)
        except (OSError, zipfile.BadZipFile) as error:
            raise PluginManagerError(f"유효한 ZIP 파일이 아닙니다: {error}") from error
        with archive:
            infos = archive.infolist()
            if len(infos) > settings["max_files"]:
                raise PluginManagerError("ZIP 파일 수가 설정한 최대 개수를 초과했습니다.")
            total = 0
            members = []
            ignored_count = 0
            for info in infos:
                safe_path = self._validate_archive_member(info, ignored_root_files)
                if safe_path is None:
                    ignored_count += 1
                    continue
                total += int(info.file_size or 0)
                if total > settings["max_extracted_bytes"]:
                    raise PluginManagerError("ZIP 해제 후 크기가 설정한 최대값을 초과했습니다.")
                if (
                    info.file_size > 1024 * 1024
                    and info.file_size / max(1, info.compress_size) > 200
                ):
                    raise PluginManagerError(f"비정상 압축률이 감지되었습니다: {info.filename}")
                members.append((info, safe_path))
            destination.mkdir(parents=True, exist_ok=False)
            root = destination.resolve()
            for index, (info, safe_path) in enumerate(members, start=1):
                self._check_stop()
                target = (destination / Path(*safe_path.parts)).resolve(strict=False)
                try:
                    target.relative_to(root)
                except ValueError as error:
                    raise PluginManagerError("ZIP 경로가 작업 디렉터리를 벗어납니다.") from error
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                if index % 100 == 0:
                    self._append_log(job_id, f"ZIP 파일 {index}/{len(members)}개를 검사·해제했습니다.")
        if ignored_count:
            self._append_log(
                job_id,
                f"검증 카탈로그의 배포용 ZIP {ignored_count}개를 추출 대상에서 제외했습니다.",
            )
        return {
            "archive_files": len(members),
            "archive_bytes": total,
            "archive_ignored_files": ignored_count,
        }

    @staticmethod
    def _collapse_wrapper(root):
        current = Path(root)
        while True:
            entries = [item for item in current.iterdir() if item.name != "__MACOSX"]
            directories = [item for item in entries if item.is_dir()]
            files = [item for item in entries if item.is_file()]
            if len(directories) == 1 and not files:
                current = directories[0]
                continue
            return current

    def _find_candidate(self, extracted_root, plugin_id):
        base = self._collapse_wrapper(extracted_root)
        direct = base / "plugins" / "metadata" / plugin_id
        if (direct / "__init__.py").is_file():
            return direct
        if (base / "__init__.py").is_file():
            return base
        candidates = []
        for init_path in base.rglob("__init__.py"):
            candidate = init_path.parent
            relative = candidate.relative_to(base)
            if len(relative.parts) > 4 or any(part in self.IGNORED_NAMES for part in relative.parts):
                continue
            if candidate.name.lower().replace("-", "_") == plugin_id:
                return candidate
            candidates.append(candidate)
        unique = sorted({path.resolve() for path in candidates}, key=lambda item: str(item))
        if len(unique) == 1:
            return unique[0]
        if not unique:
            raise PluginManagerError("ZIP에서 BookOasis 플러그인 루트를 찾을 수 없습니다.")
        raise PluginManagerError("ZIP에 플러그인 후보가 여러 개입니다. 플러그인 단일 패키지를 사용해 주세요.")

    @classmethod
    def _ignored_copy(cls, directory, names):
        return [name for name in names if name in cls.IGNORED_NAMES or name.endswith((".pyc", ".pyo"))]

    def _validate_plugin(self, candidate, plugin_id, settings):
        candidate = Path(candidate).resolve()
        if candidate.is_symlink() or not (candidate / "__init__.py").is_file():
            raise PluginManagerError("플러그인 루트에 __init__.py 파일이 필요합니다.")
        files = []
        python_files = []
        total = 0
        for path in candidate.rglob("*"):
            relative = path.relative_to(candidate)
            if any(part in self.IGNORED_NAMES for part in relative.parts):
                continue
            if path.is_symlink():
                raise PluginManagerError(f"플러그인 심볼릭 링크는 허용하지 않습니다: {relative}")
            if not path.is_file():
                continue
            if path.suffix.lower() in self.REJECTED_SUFFIXES:
                raise PluginManagerError(f"플러그인 패키지에 허용되지 않은 파일이 있습니다: {relative}")
            total += path.stat().st_size
            if total > settings["max_extracted_bytes"]:
                raise PluginManagerError("플러그인 파일 크기가 설정한 최대값을 초과했습니다.")
            files.append(str(relative).replace("\\", "/"))
            if path.suffix.lower() == ".py":
                python_files.append(path)
        if len(files) > settings["max_files"]:
            raise PluginManagerError("플러그인 파일 수가 설정한 최대 개수를 초과했습니다.")
        if len(python_files) < 2:
            raise PluginManagerError("__init__.py 외에 플러그인 공급자 Python 파일이 필요합니다.")
        for path in python_files:
            try:
                source = path.read_text(encoding="utf-8-sig")
                ast.parse(source, filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as error:
                raise PluginManagerError(f"Python 문법 검사에 실패했습니다: {path.name} · {error}") from error
        requirements = []
        requirements_path = candidate / "requirements.txt"
        if requirements_path.is_file():
            for line in requirements_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                value = line.strip()
                if value and not value.startswith("#"):
                    requirements.append(value[:240])
            requirements = requirements[:50]
        return {
            "plugin_id": plugin_id,
            "version": self._read_version(candidate),
            "file_count": len(files),
            "total_bytes": total,
            "files": files[:300],
            "files_truncated": len(files) > 300,
            "requirements": requirements,
        }

    def _backup_existing(self, target, plugin_id, work_root, keep, job_id):
        if not target.exists():
            return ""
        backup_root = work_root / "backups" / plugin_id
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = backup_root / f"{stamp}_{uuid.uuid4().hex[:8]}"
        shutil.copytree(target, backup, symlinks=True, ignore=self._ignored_copy)
        self._append_log(job_id, f"기존 플러그인을 백업했습니다: {backup.name}")
        backups = sorted(
            [item for item in backup_root.iterdir() if item.is_dir()],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
        return str(backup)

    def _install_candidate(self, candidate, manifest, settings, job_id):
        plugin_root = self._safe_resolve(settings["plugin_root"], "BookOasis plugins/metadata 경로", create=True)
        work_root = self._safe_resolve(settings["work_dir"], "플러그인 작업 디렉터리", create=True)
        try:
            work_root.relative_to(plugin_root)
            raise PluginManagerError("플러그인 작업 디렉터리는 plugins/metadata 경로 밖에 설정해 주세요.")
        except ValueError:
            pass
        plugin_id = manifest["plugin_id"]
        target = plugin_root / plugin_id
        incoming = plugin_root / f".mate_incoming_{plugin_id}_{job_id[:8]}"
        previous = plugin_root / f".mate_previous_{plugin_id}_{job_id[:8]}"
        if target.is_symlink():
            raise PluginManagerError("기존 플러그인 경로가 심볼릭 링크라서 교체할 수 없습니다.")
        if target.exists() and not target.is_dir():
            raise PluginManagerError("기존 플러그인 대상 경로가 디렉터리가 아닙니다.")
        for temporary in (incoming, previous):
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        existed = target.is_dir()
        backup = self._backup_existing(
            target,
            plugin_id,
            work_root,
            settings["backup_keep"],
            job_id,
        )
        self._check_stop()
        shutil.copytree(candidate, incoming, symlinks=False, ignore=self._ignored_copy)
        self._validate_plugin(incoming, plugin_id, settings)
        self._set_job(job_id, percent=82, message="검증한 플러그인을 설치 경로에 적용하고 있습니다.")
        try:
            if existed:
                target.rename(previous)
            incoming.rename(target)
            self._validate_plugin(target, plugin_id, settings)
        except Exception:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            if previous.exists():
                previous.rename(target)
            if incoming.exists():
                shutil.rmtree(incoming, ignore_errors=True)
            raise
        else:
            if previous.exists():
                shutil.rmtree(previous, ignore_errors=True)
        return {
            "action": "updated" if existed else "installed",
            "target": str(target),
            "backup": backup,
            "restart_recommended": True,
        }

    def _history_path(self, settings):
        work_root = self._safe_resolve(settings["work_dir"], "플러그인 작업 디렉터리", create=True)
        return work_root / "history.json"

    def _record_history(self, settings, job):
        try:
            path = self._history_path(settings)
            history = []
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                history = data if isinstance(data, list) else []
            result = job.get("result") or {}
            history.insert(
                0,
                {
                    "id": job.get("id"),
                    "operation": job.get("operation"),
                    "source": job.get("source"),
                    "status": job.get("status"),
                    "message": job.get("message"),
                    "plugin_id": result.get("plugin_id", ""),
                    "version": result.get("version", ""),
                    "started_at": job.get("started_at"),
                    "finished_at": job.get("finished_at"),
                },
            )
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(history[:100], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except Exception:
            if self.logger:
                self.logger.exception("[BookOasisMate] 플러그인 작업 이력 저장 실패")

    def history(self, settings):
        try:
            path = self._history_path(self.normalized_settings(settings))
            if not path.is_file():
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
            return data[:100] if isinstance(data, list) else []
        except Exception:
            return []

    def _finish_job(self, job_id, settings, status, message, result=None, error=""):
        label = {"completed": "완료", "failed": "실패", "stopped": "중지"}.get(status, status)
        with self._lock:
            if not self._job or self._job.get("id") != job_id:
                return
            snapshot = copy.deepcopy(self._job)
        snapshot.update(
            {
                "status": status,
                "status_label": label,
                "message": message,
                "percent": 100 if status == "completed" else snapshot.get("percent", 0),
                "result": result,
                "error": error,
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "can_install": bool(result and result.get("inspect_job_id")),
            }
        )
        self._record_history(settings, snapshot)
        self._set_job(job_id, **snapshot)

    def _run_source_job(self, job_id, spec, settings, install):
        job_dir = None
        try:
            self._set_job(job_id, status="running", status_label="진행 중", percent=2)
            work_root = self._safe_resolve(settings["work_dir"], "플러그인 작업 디렉터리", create=True)
            job_dir = work_root / "jobs" / job_id
            job_dir.parent.mkdir(parents=True, exist_ok=True)
            job_dir.mkdir(parents=False, exist_ok=False)
            archive_path = job_dir / "source.zip"
            if spec["kind"] == "github":
                self._set_job(job_id, percent=8, message="GitHub 아카이브를 내려받고 있습니다.")
                self._download_github(spec, archive_path, settings, job_id)
            else:
                upload_path = Path(spec["archive_path"])
                if not upload_path.is_file():
                    raise PluginManagerError("업로드 ZIP 파일을 찾을 수 없습니다.")
                shutil.move(str(upload_path), archive_path)
            self._check_stop()
            self._set_job(job_id, percent=28, message="ZIP 구조와 안전 제한을 검사하고 있습니다.")
            extracted = job_dir / "extracted"
            archive_info = self._extract_archive(
                archive_path,
                extracted,
                settings,
                job_id,
                ignored_root_files=spec.get("ignored_archive_files"),
            )
            archive_path.unlink(missing_ok=True)
            self._set_job(job_id, percent=58, message="BookOasis 플러그인 구조와 Python 문법을 검사하고 있습니다.")
            plugin_id = self._validate_plugin_id(spec["plugin_id"])
            candidate = self._find_candidate(extracted, plugin_id)
            manifest = self._validate_plugin(candidate, plugin_id, settings)
            manifest.update(archive_info)
            manifest.update(
                {
                    "source": spec.get("repository") or spec.get("filename") or "ZIP",
                    "ref": spec.get("ref", ""),
                    "trusted": bool(spec.get("trusted")),
                }
            )
            self._append_log(
                job_id,
                f"플러그인 {plugin_id} 구조 검사 완료 · {manifest['file_count']}개 파일",
            )
            if install:
                self._check_stop()
                install_result = self._install_candidate(candidate, manifest, settings, job_id)
                manifest.update(install_result)
                message = "플러그인 업데이트를 완료했습니다." if install_result["action"] == "updated" else "플러그인 설치를 완료했습니다."
                shutil.rmtree(job_dir, ignore_errors=True)
                self._finish_job(job_id, settings, "completed", message, manifest)
            else:
                manifest["inspect_job_id"] = job_id
                with self._lock:
                    self._prepared[job_id] = {
                        "candidate": str(candidate),
                        "manifest": manifest,
                        "source": manifest["source"],
                        "job_dir": str(job_dir),
                    }
                self._finish_job(
                    job_id,
                    settings,
                    "completed",
                    "플러그인 패키지 검사를 완료했습니다. 내용을 확인한 뒤 설치할 수 있습니다.",
                    manifest,
                )
        except PluginManagerStopped as error:
            if job_dir:
                shutil.rmtree(job_dir, ignore_errors=True)
            self._finish_job(job_id, settings, "stopped", str(error), error=str(error))
        except Exception as error:
            if self.logger:
                self.logger.exception("[BookOasisMate] 플러그인 패키지 작업 실패")
            self._append_log(job_id, str(error))
            if job_dir:
                shutil.rmtree(job_dir, ignore_errors=True)
            self._finish_job(
                job_id,
                settings,
                "failed",
                "플러그인 패키지 처리에 실패했습니다.",
                error=str(error),
            )

    def _run_prepared_install(self, job_id, inspect_job_id, prepared, settings):
        try:
            self._set_job(
                job_id,
                status="running",
                status_label="진행 중",
                percent=20,
                message="검사한 플러그인을 설치하고 있습니다.",
            )
            candidate = Path(prepared["candidate"])
            manifest = self._validate_plugin(
                candidate,
                prepared["manifest"]["plugin_id"],
                settings,
            )
            manifest.update(
                {
                    key: value
                    for key, value in prepared["manifest"].items()
                    if key not in {"files", "file_count", "total_bytes", "requirements"}
                }
            )
            self._check_stop()
            install_result = self._install_candidate(candidate, manifest, settings, job_id)
            manifest.update(install_result)
            message = "플러그인 업데이트를 완료했습니다." if install_result["action"] == "updated" else "플러그인 설치를 완료했습니다."
            shutil.rmtree(Path(prepared["job_dir"]), ignore_errors=True)
            with self._lock:
                self._prepared.pop(inspect_job_id, None)
            self._finish_job(job_id, settings, "completed", message, manifest)
        except PluginManagerStopped as error:
            self._finish_job(job_id, settings, "stopped", str(error), error=str(error))
        except Exception as error:
            if self.logger:
                self.logger.exception("[BookOasisMate] 검사 완료 플러그인 설치 실패")
            self._append_log(job_id, str(error))
            self._finish_job(
                job_id,
                settings,
                "failed",
                "플러그인 설치에 실패했습니다. 기존 설치본은 유지하거나 복원했습니다.",
                error=str(error),
            )

    def _discard_prepared(self):
        with self._lock:
            prepared_items = list(self._prepared.values())
            self._prepared.clear()
        for prepared in prepared_items:
            job_dir = str(prepared.get("job_dir") or "").strip()
            if job_dir:
                shutil.rmtree(Path(job_dir), ignore_errors=True)
