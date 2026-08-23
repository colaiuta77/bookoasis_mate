# BookOasis Docker Compose 상태 조회와 제한된 관리 작업을 제공합니다.
import copy
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .bookoasis_compose import BASE_COMPOSE_NAMES, OVERRIDE_COMPOSE_NAMES
except ImportError:  # 단독 테스트/실행 호환
    from bookoasis_compose import BASE_COMPOSE_NAMES, OVERRIDE_COMPOSE_NAMES


SERVICE_NAME = "bookoasis"
CONTAINER_NAME = "bookoasis"
COMMAND_OUTPUT_LIMIT = 128 * 1024
REGISTRY_RESPONSE_LIMIT = 2 * 1024 * 1024
REGISTRY_TOKEN_LIMIT = 64 * 1024
DOCKER_CLI_CANDIDATES = (
    "/usr/bin/docker",
    "/usr/local/bin/docker",
    "/var/packages/ContainerManager/target/usr/bin/docker",
)
COMPOSE_CLI_CANDIDATES = (
    "/usr/libexec/docker/cli-plugins/docker-compose",
    "/usr/lib/docker/cli-plugins/docker-compose",
    "/usr/local/lib/docker/cli-plugins/docker-compose",
    "/usr/local/libexec/docker/cli-plugins/docker-compose",
    "/var/packages/ContainerManager/target/usr/libexec/docker/cli-plugins/docker-compose",
    "/var/packages/ContainerManager/target/usr/lib/docker/cli-plugins/docker-compose",
    "/usr/bin/docker-compose",
    "/usr/local/bin/docker-compose",
)
GIT_CLI_CANDIDATES = ("/usr/bin/git", "/usr/local/bin/git")
OCI_INDEX_ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
OCI_MANIFEST_ACCEPT = ", ".join((
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


class DockerManagerError(ValueError):
    pass


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str
    stderr: str = ""


class GhcrRegistryClient:
    """Public GHCR Registry API client used only for read-only image metadata."""

    _TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}

    def __init__(self, http_open=None, sleep=None, timeout=5, attempts=3, logger=None):
        self.http_open = http_open or urlopen
        self.sleep = sleep or time.sleep
        self.timeout = max(1, min(int(timeout), 15))
        self.attempts = max(1, min(int(attempts), 3))
        self.logger = logger

    @staticmethod
    def _bounded_read(response, limit, label):
        data = response.read(int(limit) + 1)
        if len(data) > int(limit):
            raise DockerManagerError(f"{label} 응답이 허용 크기를 초과했습니다.")
        return data

    def _request(self, url, *, headers=None, limit=REGISTRY_RESPONSE_LIMIT, label="GHCR 요청"):
        request = Request(
            url,
            headers={"User-Agent": "BookOasis-Mate", **(headers or {})},
            method="GET",
        )
        last_error = None
        for attempt in range(self.attempts):
            try:
                with self.http_open(request, timeout=self.timeout) as response:
                    data = self._bounded_read(response, limit, label)
                    return data, response.headers
            except HTTPError as error:
                last_error = error
                code = int(getattr(error, "code", 0) or 0)
                if code in self._TRANSIENT_HTTP and attempt + 1 < self.attempts:
                    self.sleep(0.35 * (2 ** attempt))
                    continue
                raise DockerManagerError(f"{label}에 실패했습니다. HTTP {code or 'Error'}") from error
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt + 1 < self.attempts:
                    self.sleep(0.35 * (2 ** attempt))
                    continue
                raise DockerManagerError(f"{label}에 실패했습니다. {error}") from error
        raise DockerManagerError(f"{label}에 실패했습니다. {last_error or 'unknown error'}")

    @staticmethod
    def _json(data, label):
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DockerManagerError(f"{label} JSON을 해석하지 못했습니다.") from error
        if not isinstance(payload, dict):
            raise DockerManagerError(f"{label} 형식이 올바르지 않습니다.")
        return payload

    @staticmethod
    def _parse_image(image):
        value = str(image or "").strip()
        if not value.startswith("ghcr.io/"):
            raise DockerManagerError("GHCR 업데이트 확인은 ghcr.io 이미지에서만 사용할 수 있습니다.")
        if "@" in value:
            raise DockerManagerError("digest로 고정된 GHCR 이미지는 자동 업데이트 확인 대상이 아닙니다.")
        rest = value[len("ghcr.io/"):]
        slash = rest.rfind("/")
        colon = rest.rfind(":")
        if colon > slash:
            repository, reference = rest[:colon], rest[colon + 1:]
        else:
            repository, reference = rest, "latest"
        if not repository or not re.fullmatch(r"[a-z0-9._/-]+", repository):
            raise DockerManagerError("GHCR 저장소 이름이 올바르지 않습니다.")
        if not reference or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", reference):
            raise DockerManagerError("GHCR 이미지 태그가 올바르지 않습니다.")
        return repository, reference

    @staticmethod
    def _digest(headers, body, label):
        digest = str(headers.get("Docker-Content-Digest") or "").strip()
        if not digest:
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise DockerManagerError(f"{label} digest가 올바르지 않습니다.")
        return digest

    def inspect(self, image, platform):
        repository, reference = self._parse_image(image)
        platform_value = str(platform or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", platform_value):
            raise DockerManagerError("GHCR 이미지 플랫폼 값이 올바르지 않습니다.")
        os_name, architecture = platform_value.split("/", 1)

        token_query = urlencode({
            "service": "ghcr.io",
            "scope": f"repository:{repository}:pull",
        })
        token_bytes, _ = self._request(
            f"https://ghcr.io/token?{token_query}",
            limit=REGISTRY_TOKEN_LIMIT,
            label="GHCR public pull token 조회",
        )
        token_payload = self._json(token_bytes, "GHCR token")
        token = str(token_payload.get("token") or token_payload.get("access_token") or "").strip()
        if not token:
            raise DockerManagerError("GHCR public pull token을 받지 못했습니다.")
        auth_headers = {"Authorization": f"Bearer {token}"}

        index_body, index_headers = self._request(
            f"https://ghcr.io/v2/{repository}/manifests/{reference}",
            headers={"Accept": OCI_INDEX_ACCEPT, **auth_headers},
            label="GHCR 이미지 manifest 조회",
        )
        index_payload = self._json(index_body, "GHCR manifest")
        remote_digest = self._digest(index_headers, index_body, "GHCR manifest")
        manifests = index_payload.get("manifests")

        if isinstance(manifests, list):
            descriptor = None
            for item in manifests:
                if not isinstance(item, dict):
                    continue
                item_platform = item.get("platform") if isinstance(item.get("platform"), dict) else {}
                if str(item_platform.get("os") or "").lower() == os_name and str(item_platform.get("architecture") or "").lower() == architecture:
                    descriptor = item
                    break
            if descriptor is None:
                raise DockerManagerError(f"GHCR 이미지에서 {platform_value} manifest를 찾지 못했습니다.")
            remote_platform_digest = str(descriptor.get("digest") or "").strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", remote_platform_digest):
                raise DockerManagerError("GHCR 플랫폼 manifest digest가 올바르지 않습니다.")
            manifest_body, _ = self._request(
                f"https://ghcr.io/v2/{repository}/manifests/{remote_platform_digest}",
                headers={"Accept": OCI_MANIFEST_ACCEPT, **auth_headers},
                label="GHCR 플랫폼 manifest 조회",
            )
            manifest_payload = self._json(manifest_body, "GHCR 플랫폼 manifest")
        else:
            remote_platform_digest = remote_digest
            manifest_payload = index_payload

        config = manifest_payload.get("config") if isinstance(manifest_payload.get("config"), dict) else {}
        config_digest = str(config.get("digest") or "").strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest):
            raise DockerManagerError("GHCR 이미지 config digest가 올바르지 않습니다.")
        config_body, _ = self._request(
            f"https://ghcr.io/v2/{repository}/blobs/{config_digest}",
            headers=auth_headers,
            label="GHCR 이미지 config 조회",
        )
        config_payload = self._json(config_body, "GHCR 이미지 config")
        config_data = config_payload.get("config") if isinstance(config_payload.get("config"), dict) else {}
        labels = config_data.get("Labels") if isinstance(config_data.get("Labels"), dict) else {}
        return {
            "remote_digest": remote_digest,
            "remote_platform_digest": remote_platform_digest,
            "remote_version": str(labels.get("org.opencontainers.image.version") or "").strip(),
            "remote_revision": str(labels.get("org.opencontainers.image.revision") or "").strip(),
        }


class SubprocessRunner:
    @staticmethod
    def _read_tail(fileobj, limit=COMMAND_OUTPUT_LIMIT):
        size = fileobj.tell()
        fileobj.seek(max(0, size - int(limit)))
        return fileobj.read(int(limit)).decode("utf-8", "replace")

    def run(self, argv, timeout=30):
        with tempfile.TemporaryFile(mode="w+b") as output_file, tempfile.TemporaryFile(mode="w+b") as error_file:
            try:
                completed = subprocess.run(
                    list(argv),
                    stdout=output_file,
                    stderr=error_file,
                    timeout=max(1, int(timeout)),
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise DockerManagerError(
                    f"Docker 명령 제한시간을 초과했습니다. ({timeout}초)"
                ) from error
            output = self._read_tail(output_file)
            stderr = self._read_tail(error_file)
        return CommandResult(completed.returncode, output, stderr)

    def run_stream(self, argv, timeout=30, on_output=None):
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        tail = bytearray()

        def read_output():
            stream = process.stdout
            if stream is None:
                return
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > COMMAND_OUTPUT_LIMIT:
                    del tail[:-COMMAND_OUTPUT_LIMIT]
                if on_output is not None:
                    try:
                        on_output(chunk.decode("utf-8", "replace"))
                    except Exception:
                        pass

        reader = threading.Thread(
            target=read_output,
            name="bookoasis-mate-command-stream",
            daemon=True,
        )
        reader.start()
        try:
            returncode = process.wait(timeout=max(1, int(timeout)))
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            reader.join()
            if process.stdout is not None:
                process.stdout.close()
            raise DockerManagerError(
                f"Docker 명령 제한시간을 초과했습니다. ({timeout}초)"
            ) from error
        reader.join()
        if process.stdout is not None:
            process.stdout.close()
        return CommandResult(returncode, bytes(tail).decode("utf-8", "replace"))


class BookOasisDockerManager:
    def __init__(
        self,
        logger=None,
        runner=None,
        spawn=None,
        host_mount="/host",
        chroot_bin="/usr/sbin/chroot",
        docker_bin=None,
        git_bin=None,
        registry_client=None,
    ):
        self.logger = logger
        self.runner = runner or SubprocessRunner()
        self.spawn = spawn
        self.host_mount = Path(host_mount)
        self.chroot_bin = str(chroot_bin)
        self._docker_bin_hint = str(docker_bin or "").strip()
        self._git_bin_hint = str(git_bin or "").strip()
        self.docker_bin = ""
        self.git_bin = ""
        self.registry_client = registry_client or GhcrRegistryClient(logger=logger)
        self._docker_cli_info = None
        self._git_cli_info = None
        self._lock = threading.RLock()
        self._job = self._empty_job()
        self._job_handle = None
        self._job_started_monotonic = None

    @staticmethod
    def _empty_job():
        return {
            "is_working": "wait",
            "action": "",
            "message": "Docker 작업 대기 중입니다.",
            "started_at": "",
            "finished_at": "",
            "elapsed_seconds": 0,
            "error": "",
            "log_tail": "",
            "before": None,
            "after": None,
        }

    def _run(self, argv, timeout=30, label="Docker 명령"):
        result = self.runner.run(list(argv), timeout=timeout)
        if result.returncode != 0:
            stdout_detail = str(result.output or "").strip()
            stderr_detail = str(getattr(result, "stderr", "") or "").strip()
            detail = "\n".join(item for item in (stdout_detail, stderr_detail) if item)
            if len(detail) > 1500:
                detail = detail[-1500:]
            raise DockerManagerError(
                f"{label} 실행에 실패했습니다."
                + (f" {detail}" if detail else "")
            )
        return str(result.output or "")

    def _resolve_root(self, docker_root):
        raw = str(docker_root or "").strip()
        if not raw:
            raise DockerManagerError("BookOasis Docker 경로를 먼저 설정해 주세요.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise DockerManagerError("BookOasis Docker 경로는 절대 경로여야 합니다.")
        try:
            mount = self.host_mount.resolve(strict=True)
        except OSError as error:
            raise DockerManagerError("FlaskFarm의 /host 마운트를 찾을 수 없습니다.") from error

        resolved = None
        try:
            direct = path.resolve(strict=True)
            direct.relative_to(mount)
            resolved = direct
        except (OSError, ValueError):
            host_candidate = mount / path.relative_to(Path("/"))
            try:
                resolved = host_candidate.resolve(strict=True)
                resolved.relative_to(mount)
            except (OSError, ValueError) as error:
                raise DockerManagerError(
                    "Docker 관리는 FlaskFarm의 /host 마운트에서 확인 가능한 호스트 BookOasis 경로만 사용할 수 있습니다."
                ) from error

        relative = resolved.relative_to(mount)
        if relative == Path("."):
            raise DockerManagerError("호스트 파일시스템 루트 자체는 Docker 경로로 사용할 수 없습니다.")
        if not resolved.is_dir():
            raise DockerManagerError("BookOasis Docker 경로가 디렉터리가 아닙니다.")
        return resolved, Path("/") / relative

    @staticmethod
    def _existing_named_files(root, names, label):
        found = []
        for name in names:
            candidate = root / name
            if candidate.is_symlink():
                raise DockerManagerError(f"보안을 위해 심볼릭 링크 {name} 파일은 사용할 수 없습니다.")
            if candidate.exists():
                if not candidate.is_file():
                    raise DockerManagerError(f"{name} 경로가 일반 파일이 아닙니다.")
                found.append(candidate)
        return found

    @staticmethod
    def _label_config_basenames(labels):
        raw = str((labels or {}).get("com.docker.compose.project.config_files") or "")
        names = []
        for item in raw.split(","):
            item = item.strip()
            name = Path(item).name if item else ""
            if name and name not in names:
                names.append(name)
        return names

    @staticmethod
    def _normalize_compose_choice(value, names, label, allow_none=False):
        choice = str(value or "auto").strip()
        if not choice or choice.lower() == "auto":
            return "auto"
        if allow_none and choice.lower() == "none":
            return "none"
        if choice not in names:
            raise DockerManagerError(f"지원하지 않는 {label} 파일입니다: {choice}")
        return choice

    @staticmethod
    def _candidate_by_name(candidates, name):
        for candidate in candidates:
            if candidate.name == name:
                return candidate
        return None

    def _select_compose_files(self, container_root, labels, compose_file=None, override_file=None):
        bases = self._existing_named_files(container_root, BASE_COMPOSE_NAMES, "base")
        if not bases:
            raise DockerManagerError("지원하는 BookOasis Docker Compose 파일을 찾지 못했습니다.")
        overrides = self._existing_named_files(container_root, OVERRIDE_COMPOSE_NAMES, "override")
        label_names = self._label_config_basenames(labels)

        base_choice = self._normalize_compose_choice(
            compose_file, BASE_COMPOSE_NAMES, "기본 Compose"
        )
        if base_choice != "auto":
            base_file = self._candidate_by_name(bases, base_choice)
            if base_file is None:
                raise DockerManagerError(f"선택한 기본 Compose 파일을 찾지 못했습니다: {base_choice}")
        else:
            labelled_bases = [
                self._candidate_by_name(bases, name)
                for name in label_names
                if name in BASE_COMPOSE_NAMES
            ]
            labelled_bases = [item for item in labelled_bases if item is not None]
            if len(labelled_bases) == 1:
                base_file = labelled_bases[0]
            elif len(labelled_bases) > 1:
                raise DockerManagerError(
                    "현재 BookOasis 컨테이너 label에 기본 Compose 파일이 여러 개 기록되어 있어 자동 판별할 수 없습니다."
                )
            elif len(bases) == 1:
                base_file = bases[0]
            else:
                raise DockerManagerError(
                    "Docker Compose 파일이 여러 개이고 현재 BookOasis가 사용한 파일을 판별할 수 없습니다. "
                    "설정에서 기본 Compose 파일을 직접 선택해 주세요."
                )

        override_choice = self._normalize_compose_choice(
            override_file, OVERRIDE_COMPOSE_NAMES, "Compose Override", allow_none=True
        )
        selected_overrides = []
        if override_choice == "none":
            selected_overrides = []
        elif override_choice != "auto":
            selected = self._candidate_by_name(overrides, override_choice)
            if selected is None:
                raise DockerManagerError(f"선택한 Compose Override 파일을 찾지 못했습니다: {override_choice}")
            selected_overrides = [selected]
        else:
            for name in label_names:
                if name not in OVERRIDE_COMPOSE_NAMES:
                    continue
                selected = self._candidate_by_name(overrides, name)
                if selected is not None and selected not in selected_overrides:
                    selected_overrides.append(selected)
            if not selected_overrides and not label_names:
                if len(overrides) == 1:
                    selected_overrides = [overrides[0]]
                elif len(overrides) > 1:
                    raise DockerManagerError(
                        "Compose Override 파일이 여러 개이고 현재 BookOasis가 사용한 파일을 판별할 수 없습니다. "
                        "설정에서 Override 파일을 직접 선택하거나 사용 안 함을 선택해 주세요."
                    )

        return base_file, selected_overrides, base_choice, override_choice

    def resolve_compose_editor_selection(self, docker_root, kind, compose_file=None, override_file=None):
        key = str(kind or "").strip().lower()
        if key not in ("compose", "override"):
            raise DockerManagerError("Compose 파일 종류가 올바르지 않습니다.")
        container_root, unused_host_root = self._resolve_root(docker_root)
        container = self._container_inspect()
        name = str(container.get("Name") or "").lstrip("/")
        config = container.get("Config") if isinstance(container.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        service_label = str(labels.get("com.docker.compose.service") or "").strip()
        project_label = str(labels.get("com.docker.compose.project") or "").strip()
        if name != CONTAINER_NAME or service_label != SERVICE_NAME or not project_label:
            raise DockerManagerError(
                "실행 중인 bookoasis 컨테이너가 검증된 Docker Compose bookoasis 서비스가 아닙니다."
            )

        base_file, override_files, unused_base_choice, override_choice = self._select_compose_files(
            container_root,
            labels,
            compose_file=compose_file,
            override_file=override_file,
        )
        if key == "compose":
            return base_file.name
        if str(override_file or "auto").strip().lower() == "none":
            return "none"
        if len(override_files) > 1:
            raise DockerManagerError(
                "현재 BookOasis 컨테이너가 Compose Override 파일을 여러 개 사용하고 있어 편집 대상을 하나로 판별할 수 없습니다. "
                "설정에서 편집할 Override 파일을 직접 선택해 주세요."
            )
        if override_files:
            return override_files[0].name
        return override_choice

    @staticmethod
    def _candidate_list(preferred, defaults):
        result = []
        for value in ([preferred] if preferred else []) + list(defaults):
            value = str(value or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    @staticmethod
    def _version_from_text(text):
        match = re.search(r"(?:version\s+)?(v?\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)?)", str(text or ""), re.I)
        return match.group(1) if match else str(text or "").strip()

    @staticmethod
    def _version_major(text):
        match = re.search(r"(?:^|[^0-9])v?(\d+)(?:\.|$)", str(text or ""), re.I)
        return int(match.group(1)) if match else None

    @staticmethod
    def _probe_detail(result, limit=260):
        stderr = str(getattr(result, "stderr", "") or "").strip()
        stdout = str(getattr(result, "output", "") or "").strip()
        detail = stderr or stdout
        detail = " ".join(detail.split())
        if len(detail) > int(limit):
            detail = detail[-int(limit):]
        return detail

    def _ensure_docker_cli(self):
        if self._docker_cli_info is not None:
            return self._docker_cli_info
        errors = []
        working_docker = None
        for candidate in self._candidate_list(self._docker_bin_hint, DOCKER_CLI_CANDIDATES):
            version_argv = [
                self.chroot_bin, str(self.host_mount), candidate,
                "version", "--format={{.Client.Version}}|{{.Server.Version}}",
            ]
            version_result = self.runner.run(version_argv, timeout=15)
            if version_result.returncode != 0:
                detail = self._probe_detail(version_result)
                errors.append(f"{candidate}: docker version 실패" + (f" ({detail})" if detail else ""))
                continue
            values = str(version_result.output or "").strip().split("|", 1)
            client_version = values[0].strip() if values else ""
            server_version = values[1].strip() if len(values) > 1 else client_version
            if working_docker is None:
                working_docker = (candidate, client_version, server_version)

            compose_argv = [self.chroot_bin, str(self.host_mount), candidate, "compose", "version"]
            compose_result = self.runner.run(compose_argv, timeout=15)
            if compose_result.returncode != 0:
                detail = self._probe_detail(compose_result)
                errors.append(f"{candidate}: docker compose 실패" + (f" ({detail})" if detail else ""))
                continue
            compose_text = str(compose_result.output or "").strip()
            self.docker_bin = candidate
            self._docker_cli_info = {
                "docker_cli": candidate,
                "docker_version": server_version or client_version,
                "docker_client_version": client_version,
                "compose_cli": candidate + " compose",
                "compose_mode": "plugin",
                "compose_version": self._version_from_text(compose_text),
            }
            return self._docker_cli_info

        if working_docker is not None:
            docker_candidate, client_version, server_version = working_docker
            for compose_candidate in COMPOSE_CLI_CANDIDATES:
                compose_argv = [self.chroot_bin, str(self.host_mount), compose_candidate, "version"]
                compose_result = self.runner.run(compose_argv, timeout=15)
                if compose_result.returncode != 0:
                    continue
                compose_text = str(compose_result.output or "").strip()
                compose_version = self._version_from_text(compose_text)
                major = self._version_major(compose_version)
                if major is not None and major < 2:
                    errors.append(
                        f"{compose_candidate}: Docker Compose {compose_version or compose_text} 감지됨 (Compose v2 필요)"
                    )
                    continue
                if major is None:
                    errors.append(f"{compose_candidate}: Compose 버전을 확인하지 못했습니다.")
                    continue
                self.docker_bin = docker_candidate
                self._docker_cli_info = {
                    "docker_cli": docker_candidate,
                    "docker_version": server_version or client_version,
                    "docker_client_version": client_version,
                    "compose_cli": compose_candidate,
                    "compose_mode": "standalone",
                    "compose_version": compose_version,
                }
                return self._docker_cli_info

            detail = "; ".join(errors[-6:])
            raise DockerManagerError(
                "호스트 Docker Engine은 확인했지만 Docker Compose v2를 찾지 못했습니다."
                + (f" {detail}" if detail else "")
            )

        detail = "; ".join(errors[-3:])
        raise DockerManagerError(
            "호스트 Docker CLI와 Docker Compose v2를 찾지 못했습니다."
            + (f" {detail}" if detail else "")
        )

    def _ensure_git_cli(self):
        if self._git_cli_info is not None:
            return self._git_cli_info
        errors = []
        for candidate in self._candidate_list(self._git_bin_hint, GIT_CLI_CANDIDATES):
            argv = [self.chroot_bin, str(self.host_mount), candidate, "--version"]
            result = self.runner.run(argv, timeout=15)
            if result.returncode != 0:
                errors.append(f"{candidate}: git --version 실패")
                continue
            text = str(result.output or "").strip()
            self.git_bin = candidate
            self._git_cli_info = {
                "git_cli": candidate,
                "git_version": self._version_from_text(text),
            }
            return self._git_cli_info
        detail = "; ".join(errors[-2:])
        raise DockerManagerError("호스트 Git CLI를 찾지 못했습니다." + (f" {detail}" if detail else ""))

    def _host_command(self, *parts):
        docker_info = self._ensure_docker_cli()
        return [self.chroot_bin, str(self.host_mount), docker_info["docker_cli"], *parts]

    def _compose_host_command(self):
        docker_info = self._ensure_docker_cli()
        if docker_info.get("compose_mode") == "standalone":
            return [self.chroot_bin, str(self.host_mount), docker_info["compose_cli"]]
        return [self.chroot_bin, str(self.host_mount), docker_info["docker_cli"], "compose"]

    def _compose_command(self, host_root, compose_files, *parts):
        argv = self._compose_host_command()
        for compose_file in compose_files:
            argv.extend(["-f", str(host_root / compose_file.name)])
        argv.extend(parts)
        return argv

    @staticmethod
    def _image_repository(image):
        value = str(image or "").strip().split("@", 1)[0]
        slash = value.rfind("/")
        colon = value.rfind(":")
        if colon > slash:
            value = value[:colon]
        return value

    @staticmethod
    def _digest_from_repo_digests(repo_digests, image):
        repository = BookOasisDockerManager._image_repository(image)
        values = [str(item or "").strip() for item in (repo_digests or []) if str(item or "").strip()]
        for value in values:
            if "@" not in value:
                continue
            repo, digest = value.rsplit("@", 1)
            if repo == repository and digest.startswith("sha256:"):
                return digest
        for value in values:
            if "@" in value:
                digest = value.rsplit("@", 1)[1]
                if digest.startswith("sha256:"):
                    return digest
        return ""

    def _image_update_info(self, container, image):
        image_id = str(container.get("Image") or "").strip()
        if not image_id:
            raise DockerManagerError("실행 중인 BookOasis 이미지 ID를 확인하지 못했습니다.")
        local_output = self._run(
            self._host_command("image", "inspect", image_id, "--format={{json .}}"),
            timeout=15,
            label="BookOasis 로컬 이미지 조회",
        )
        try:
            local = json.loads(local_output.strip())
        except (TypeError, json.JSONDecodeError) as error:
            raise DockerManagerError("BookOasis 로컬 이미지 정보를 해석하지 못했습니다.") from error
        if not isinstance(local, dict):
            raise DockerManagerError("BookOasis 로컬 이미지 정보 형식이 올바르지 않습니다.")
        local_digest = self._digest_from_repo_digests(local.get("RepoDigests"), image)
        if not local_digest:
            raise DockerManagerError("BookOasis 로컬 이미지 digest를 확인하지 못했습니다.")
        os_name = str(local.get("Os") or "").strip().lower()
        architecture = str(local.get("Architecture") or "").strip().lower()
        if not os_name or not architecture:
            raise DockerManagerError("BookOasis 이미지 플랫폼을 확인하지 못했습니다.")
        if not re.fullmatch(r"[a-z0-9_.-]+", os_name) or not re.fullmatch(r"[a-z0-9_.-]+", architecture):
            raise DockerManagerError("BookOasis 이미지 플랫폼 값이 올바르지 않습니다.")
        platform = f"{os_name}/{architecture}"
        remote = self.registry_client.inspect(image, platform)
        remote_digest = str(remote.get("remote_digest") or "").strip()
        remote_platform_digest = str(remote.get("remote_platform_digest") or "").strip()
        remote_version = str(remote.get("remote_version") or "").strip()
        remote_revision = str(remote.get("remote_revision") or "").strip()
        if not remote_digest:
            raise DockerManagerError("BookOasis 원격 이미지 digest를 확인하지 못했습니다.")
        latest_digests = {item for item in (remote_digest, remote_platform_digest) if item}
        update_available = local_digest not in latest_digests
        return {
            "local_digest": local_digest,
            "remote_digest": remote_digest,
            "remote_platform_digest": remote_platform_digest,
            "remote_version": remote_version,
            "remote_revision": remote_revision,
            "image_platform": platform,
            "update_available": update_available,
            "update_status": "available" if update_available else "latest",
            "update_check_error": "",
        }

    def _container_inspect(self):
        output = self._run(
            self._host_command("inspect", CONTAINER_NAME, "--format={{json .}}"),
            timeout=15,
            label="BookOasis 컨테이너 조회",
        )
        try:
            payload = json.loads(output.strip())
        except (TypeError, json.JSONDecodeError) as error:
            raise DockerManagerError("BookOasis 컨테이너 정보를 해석하지 못했습니다.") from error
        if not isinstance(payload, dict):
            raise DockerManagerError("BookOasis 컨테이너 정보 형식이 올바르지 않습니다.")
        return payload

    def inspect(self, docker_root, compose_file=None, override_file=None):
        container_root, host_root = self._resolve_root(docker_root)
        container = self._container_inspect()
        container_id = str(container.get("Id") or "").strip()
        name = str(container.get("Name") or "").lstrip("/")
        config = container.get("Config") if isinstance(container.get("Config"), dict) else {}
        labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
        service_label = str(labels.get("com.docker.compose.service") or "").strip()
        project_label = str(labels.get("com.docker.compose.project") or "").strip()
        if name != CONTAINER_NAME or service_label != SERVICE_NAME or not project_label:
            raise DockerManagerError(
                "실행 중인 bookoasis 컨테이너가 검증된 Docker Compose bookoasis 서비스가 아닙니다."
            )
        if not container_id:
            raise DockerManagerError("BookOasis 컨테이너 ID를 확인하지 못했습니다.")

        base_file, override_files, base_choice, override_choice = self._select_compose_files(
            container_root,
            labels,
            compose_file=compose_file,
            override_file=override_file,
        )
        compose_files = [base_file, *override_files]
        config_output = self._run(
            self._compose_command(host_root, compose_files, "config", "--format", "json"),
            timeout=20,
            label="BookOasis Compose 구성 확인",
        )
        try:
            compose_config = json.loads(config_output.strip())
        except (TypeError, json.JSONDecodeError) as error:
            raise DockerManagerError("Docker Compose 구성을 해석하지 못했습니다.") from error
        services = compose_config.get("services") if isinstance(compose_config, dict) else None
        service = services.get(SERVICE_NAME) if isinstance(services, dict) else None
        if not isinstance(service, dict):
            raise DockerManagerError("선택한 Compose 구성에 bookoasis 서비스가 없습니다.")

        compose_id = self._run(
            self._compose_command(host_root, compose_files, "ps", "-q", SERVICE_NAME),
            timeout=15,
            label="BookOasis Compose 컨테이너 확인",
        ).strip()
        if compose_id != container_id:
            raise DockerManagerError(
                "선택한 Docker 경로의 bookoasis 서비스가 현재 실행 중인 bookoasis 컨테이너와 일치하지 않습니다."
            )

        mode = "build" if service.get("build") is not None else "image" if service.get("image") else "unknown"
        if mode == "unknown":
            raise DockerManagerError("bookoasis 서비스의 image/build 설치 방식을 판별할 수 없습니다.")
        state = container.get("State") if isinstance(container.get("State"), dict) else {}
        image = str(service.get("image") or config.get("Image") or "").strip()
        version = str(labels.get("org.opencontainers.image.version") or "").strip()
        revision = str(labels.get("org.opencontainers.image.revision") or "").strip()
        update_info = {
            "local_digest": "",
            "remote_digest": "",
            "remote_platform_digest": "",
            "remote_version": "",
            "remote_revision": "",
            "image_platform": "",
            "update_available": None,
            "update_status": "build" if mode == "build" else "unknown",
            "update_check_error": "",
        }
        if mode == "image":
            try:
                update_info.update(self._image_update_info(container, image))
            except Exception as error:
                detail = str(error or "").strip()
                if len(detail) > 500:
                    detail = detail[-500:]
                update_info.update(
                    {
                        "update_available": None,
                        "update_status": "error",
                        "update_check_error": "이미지 업데이트 확인 실패" + (f": {detail}" if detail else ""),
                    }
                )
        docker_cli_info = self._ensure_docker_cli()
        git_cli_info = {"git_cli": "", "git_version": ""}
        build_update_ready = False
        if mode == "build":
            try:
                git_cli_info = self._ensure_git_cli()
                build_update_ready = True
            except Exception as error:
                update_info["update_status"] = "build_error"
                update_info["update_check_error"] = "Git 확인 실패: " + str(error)
        data = {
            "manageable": True,
            "service": SERVICE_NAME,
            "container_name": CONTAINER_NAME,
            "container_id": container_id,
            "project": project_label,
            "state": str(state.get("Status") or "unknown"),
            "running": bool(state.get("Running")),
            "mode": mode,
            "image": image,
            "version": version,
            "revision": revision,
            "docker_cli": docker_cli_info.get("docker_cli", ""),
            "docker_version": docker_cli_info.get("docker_version", ""),
            "docker_client_version": docker_cli_info.get("docker_client_version", ""),
            "compose_version": docker_cli_info.get("compose_version", ""),
            "compose_cli": docker_cli_info.get("compose_cli", ""),
            "git_cli": git_cli_info.get("git_cli", ""),
            "git_version": git_cli_info.get("git_version", ""),
            "build_update_ready": build_update_ready,
            "docker_root": str(host_root),
            "compose_file": str(host_root / base_file.name),
            "override_file": str(host_root / override_files[0].name) if override_files else "",
            "compose_files": [str(host_root / item.name) for item in compose_files],
            "compose_selection": base_choice,
            "override_selection": override_choice,
        }
        data.update(update_info)
        return data

    def _spawn(self, target, args=(), name="bookoasis-mate-docker"):
        if self.spawn is not None:
            return self.spawn(target, args=args, name=name)
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        return thread

    def _compose_status_command(self, status, *parts):
        argv = self._compose_host_command()
        for compose_file in status.get("compose_files") or []:
            argv.extend(["-f", str(compose_file)])
        argv.extend(parts)
        return argv

    def _git_command(self, host_root, *parts):
        git_info = self._ensure_git_cli()
        return [
            self.chroot_bin,
            str(self.host_mount),
            git_info["git_cli"],
            "-C",
            str(host_root),
            *parts,
        ]

    def _append_job_log_chunk(self, text):
        text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        with self._lock:
            combined = (self._job.get("log_tail") or "") + text
            encoded = combined.encode("utf-8", "replace")
            if len(encoded) > COMMAND_OUTPUT_LIMIT:
                combined = encoded[-COMMAND_OUTPUT_LIMIT:].decode("utf-8", "replace")
            self._job["log_tail"] = combined

    def _append_job_log(self, text):
        text = str(text or "").strip()
        if text:
            self._append_job_log_chunk(text + "\n")

    def _set_job_message(self, message):
        with self._lock:
            self._job["message"] = str(message or "")

    def _run_action_command(self, argv, timeout, message, label):
        self._set_job_message(message)
        self._append_job_log(message)
        stream_run = getattr(self.runner, "run_stream", None)
        if callable(stream_run):
            result = stream_run(
                list(argv),
                timeout=timeout,
                on_output=self._append_job_log_chunk,
            )
            if result.returncode != 0:
                detail = str(result.output or "").strip()
                if len(detail) > 1500:
                    detail = detail[-1500:]
                raise DockerManagerError(
                    f"{label} 실행에 실패했습니다."
                    + (f" {detail}" if detail else "")
                )
            return str(result.output or "")
        output = self._run(argv, timeout=timeout, label=label)
        self._append_job_log(output)
        return output

    def _run_action(self, docker_root, action, before):
        try:
            if action == "restart":
                self._run_action_command(
                    self._compose_status_command(before, "restart", SERVICE_NAME),
                    180,
                    "BookOasis 컨테이너를 재시작하고 있습니다.",
                    "BookOasis 재시작",
                )
            elif action == "apply":
                self._run_action_command(
                    self._compose_status_command(before, "up", "-d", "--no-deps", SERVICE_NAME),
                    600,
                    "BookOasis Compose 구성을 적용하고 있습니다.",
                    "BookOasis 구성 적용",
                )
            elif action == "update" and before.get("mode") == "image":
                self._run_action_command(
                    self._compose_status_command(before, "pull", SERVICE_NAME),
                    1200,
                    "BookOasis 최신 이미지를 내려받고 있습니다.",
                    "BookOasis 이미지 업데이트",
                )
                self._run_action_command(
                    self._compose_status_command(before, "up", "-d", "--no-deps", SERVICE_NAME),
                    600,
                    "새 BookOasis 이미지로 컨테이너를 적용하고 있습니다.",
                    "BookOasis 이미지 적용",
                )
            elif action == "update" and before.get("mode") == "build":
                host_root = before.get("docker_root")
                inside = self._run_action_command(
                    self._git_command(host_root, "rev-parse", "--is-inside-work-tree"),
                    30,
                    "BookOasis 소스 Git 저장소를 확인하고 있습니다.",
                    "BookOasis Git 확인",
                ).strip().lower()
                if inside != "true":
                    raise DockerManagerError("Build 방식 업데이트에는 BookOasis Docker 경로가 Git 저장소여야 합니다.")
                dirty = self._run_action_command(
                    self._git_command(host_root, "status", "--porcelain"),
                    30,
                    "BookOasis 소스 변경 여부를 확인하고 있습니다.",
                    "BookOasis Git 상태 확인",
                ).strip()
                if dirty:
                    raise DockerManagerError(
                        "BookOasis 소스에 수정된 파일이 있어 자동 업데이트를 중단했습니다. Git 상태를 먼저 정리해 주세요."
                    )
                self._run_action_command(
                    self._git_command(host_root, "pull", "--ff-only"),
                    600,
                    "BookOasis 소스를 fast-forward 방식으로 업데이트하고 있습니다.",
                    "BookOasis Git 업데이트",
                )
                self._run_action_command(
                    self._compose_status_command(
                        before,
                        "up",
                        "-d",
                        "--build",
                        "--no-deps",
                        SERVICE_NAME,
                    ),
                    2400,
                    "BookOasis 이미지를 빌드하고 컨테이너를 적용하고 있습니다.",
                    "BookOasis 빌드 업데이트",
                )
            else:
                raise DockerManagerError("지원하지 않는 BookOasis Docker 작업입니다.")

            after = self.inspect(
                docker_root,
                compose_file=before.get("compose_selection"),
                override_file=before.get("override_selection"),
            )
            with self._lock:
                self._job.update(
                    {
                        "is_working": "done",
                        "message": "BookOasis Docker 작업을 완료했습니다.",
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(time.monotonic() - self._job_started_monotonic, 1),
                        "after": after,
                        "error": "",
                    }
                )
        except Exception as error:
            if self.logger is not None:
                try:
                    self.logger.error(f"BookOasis Mate Docker 작업 실패: {error}")
                except Exception:
                    pass
            with self._lock:
                self._job.update(
                    {
                        "is_working": "fail",
                        "message": "BookOasis Docker 작업에 실패했습니다.",
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": round(time.monotonic() - self._job_started_monotonic, 1),
                        "error": str(error),
                    }
                )
        finally:
            with self._lock:
                self._job_handle = None

    def start(self, docker_root, action, confirmation, compose_file=None, override_file=None):
        action = str(action or "").strip().lower()
        if action not in {"restart", "apply", "update"}:
            raise DockerManagerError("지원하지 않는 BookOasis Docker 작업입니다.")
        expected_confirmation = f"BOOKOASIS_{action.upper()}"
        if str(confirmation or "").strip() != expected_confirmation:
            raise DockerManagerError("BookOasis Docker 작업 확인값이 올바르지 않습니다.")

        with self._lock:
            if self._job.get("is_working") == "run":
                raise DockerManagerError("다른 BookOasis Docker 작업이 이미 실행 중입니다.")
            if compose_file is None and override_file is None:
                before = self.inspect(docker_root)
            else:
                before = self.inspect(
                    docker_root, compose_file=compose_file, override_file=override_file
                )
            if not before.get("manageable"):
                raise DockerManagerError("현재 BookOasis Docker 대상을 안전하게 관리할 수 없습니다.")
            if action == "update" and before.get("mode") == "build" and before.get("build_update_ready") is not True:
                raise DockerManagerError(
                    before.get("update_check_error") or "Build 방식 업데이트에 사용할 Git CLI를 확인하지 못했습니다."
                )
            if action == "update" and before.get("mode") == "image":
                if before.get("update_available") is False:
                    version = str(before.get("remote_version") or before.get("version") or "").strip()
                    raise DockerManagerError(
                        "BookOasis 이미지는 이미 최신 상태입니다."
                        + (f" ({version})" if version else "")
                    )
                if before.get("update_available") is not True:
                    raise DockerManagerError(
                        "GHCR 이미지 업데이트 여부를 확인하지 못해 업데이트를 시작하지 않았습니다. 상태 새로고침을 다시 시도해 주세요."
                    )
            self._job_started_monotonic = time.monotonic()
            self._job = self._empty_job()
            self._job.update(
                {
                    "is_working": "run",
                    "action": action,
                    "message": "BookOasis Docker 작업을 시작합니다.",
                    "log_tail": "BookOasis Docker 작업을 시작합니다.\n",
                    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "before": before,
                }
            )
            handle = self._spawn(
                self._run_action,
                (str(docker_root), action, before),
                name=f"bookoasis-mate-docker-{action}",
            )
            self._job_handle = handle
            return {"started": True, "status": copy.deepcopy(self._job)}

    def job_status(self):
        with self._lock:
            status = copy.deepcopy(self._job)
            if status.get("is_working") == "run" and self._job_started_monotonic is not None:
                status["elapsed_seconds"] = round(time.monotonic() - self._job_started_monotonic, 1)
            return status
