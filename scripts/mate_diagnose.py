# Linux 호스트에서 Mate·BookOasis 환경을 읽기 전용으로 점검하고 공유용 보고서를 만듭니다.
import argparse
import inspect
import json
import os
import platform
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


VERSION = "1.0"
LIMIT = 2 * 1024 * 1024
PATTERNS = (
    ("권한", r"PermissionError|Permission denied|EACCES", "실패한 경로의 실행 사용자·마운트 권한을 확인하세요."),
    ("파일 없음", r"FileNotFoundError|No such file or directory", "호스트 경로와 각 컨테이너 내부 경로를 구분하세요."),
    ("DB 손상", r"database disk image is malformed|database corruption", "대상 DB와 WAL을 확인하고 복구 전 백업하세요. 자동 복구하지 않습니다."),
    ("DB 잠금", r"database is locked|database table is locked|Lock wait timeout", "동시 작업·긴 트랜잭션을 확인하세요. 이 기록만으로 원인을 확정할 수 없습니다."),
    ("FTS", r"no such module: fts5|malformed.*fts|fts5.*error", "오류가 발생한 Python SQLite와 실제 DB를 확인하세요."),
    ("인증", r"\b401\b|Unauthorized|invalid_grant|invalid credentials", "계정·토큰 만료 여부를 확인하세요. 토큰을 보고서에 첨부하지 마세요."),
    ("접근 거부", r"\b403\b|Forbidden", "권한·API 범위·서비스 제한을 확인하세요. 403만으로 인증 실패라고 단정하지 않습니다."),
    ("접속", r"ConnectionRefused|Connection refused|NameResolutionError|Name or service not known|CERTIFICATE_VERIFY_FAILED", "호출 컨테이너의 DNS·포트·TLS를 확인하세요."),
    ("시간 초과", r"TimeoutError|ReadTimeout|timed out", "서버 부하·네트워크·마운트 지연을 구분하세요."),
    ("원본 서버/프록시", r"\b(?:502|503|504|521|522|524)\b", "응답한 계층과 원본 서버 상태를 비교하세요. 숫자 패턴만 검출한 후보입니다."),
    ("Activity 경로 보류", r"Activity.*(?:경로|path).*(?:보류|확인할 수|unknown)|부모 경로가 감시 폴더", "감시 밖 이동·이전 경로 부재·권한을 구분하세요. 다른 이벤트 수집 여부도 확인하세요."),
    ("외부 이벤트/자체 감지 충돌", r"자체 변경 감지 모드에서는 외부 이벤트", "자체 감지 사용 시 gd-poller 외부 이벤트 수신 거절은 정책입니다."),
    ("gd-poller 프로세스 종료", r"ProcessLookupError", "종료된 subprocess에 kill을 호출한 경쟁 조건일 수 있습니다. 이벤트 성공 여부와 별도로 확인하세요."),
    ("HTTPError 테스트 정리", r"KeyError: ['\"]file['\"]", "HTTPError(fp=None) close 정리 스택인지 확인하세요. 운영 오류와 테스트 실패를 구분하세요."),
    ("의존성/문법", r"ModuleNotFoundError|ImportError|SyntaxError", "실제 컨테이너 Python 버전·의존성·설치 소스를 확인하세요."),
    ("마운트 보호 보류", r"대량 삭제|빈 마운트|루트 식별 정보가 변경|감시 항목 한도", "연결 상태와 실제 삭제를 확인하세요. 확인 없이 기준을 초기화하지 마세요."),
    ("Redis", r"redis.*(?:error|failed|connection)|MISCONF|READONLY You can't write", "Redis 연결·지속 저장을 확인하세요. 이 진단은 Redis 데이터를 조회하거나 지우지 않습니다."),
)


def log_findings(text):
    return [(name, len(re.findall(pattern, text, re.I)), advice)
            for name, pattern, advice in PATTERNS if re.search(pattern, text, re.I)]


def redact(text):
    text = str(text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[가림]@", text)
    text = re.sub(r"(?i)(https?://[^\s?#]+)[?#][^\s]*", r"\1?[가림]", text)
    text = re.sub(r"(?im)((?:authorization|cookie|set-cookie|api[ _-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|비밀번호|토큰|암호)\s*['\"]?\s*[:=]\s*)[^\r\n]*", r"\1[가림]", text)
    text = re.sub(r"(?i)(/api/webhooks/)[^\s]+", r"\1[가림]", text)
    return text


def origin_url(value):
    parts = urlsplit(value.strip())
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or parts.path not in {"", "/"}
            or any(ord(c) < 33 for c in value)):
        raise ValueError("계정·쿼리·경로 없이 http(s)://호스트:포트 형식으로 입력하세요.")
    parts.port  # 잘못된 포트는 거부합니다.
    return urlunsplit((parts.scheme, parts.netloc, "/", "", ""))


def host_path(path, mounts):
    path = posixpath.normpath(path)
    for mount in sorted(mounts, key=lambda m: len(m["Destination"]), reverse=True):
        root = mount["Destination"].rstrip("/")
        if mount.get("Type") in {"bind", "volume"} and (path == root or path.startswith(root + "/")):
            return posixpath.normpath(mount["Source"].rstrip("/") + path[len(root):])
    return None


def run_command(args, payload=None, timeout=20):
    # 출력은 메모리에 무제한 누적하지 않습니다. 임시 원문은 보고서에 복사하지 않습니다.
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=output, stderr=output)
            try:
                process.communicate(payload.encode("utf-8") if payload is not None else None, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return None, "시간 제한 초과"
            output.seek(0)
            data = output.read(LIMIT + 1)
            if len(data) > LIMIT:
                return None, "출력 한도 초과"
            return process.returncode, data.decode("utf-8", "replace")
    except OSError as error:
        return None, type(error).__name__


def container_probe(request):
    # 이 함수만 컨테이너 Python으로 전달하며 Mate를 import하거나 시작하지 않습니다.
    import importlib.util
    import json
    import os
    import re
    import signal
    import sqlite3
    import sys
    import time
    from pathlib import Path
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, build_opener, HTTPRedirectHandler

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(124))
        signal.alarm(15)
    mode = request["mode"]
    if mode == "runtime":
        return {"python": sys.version.split()[0], "uid": os.geteuid() if hasattr(os, "geteuid") else None,
                "sqlite": sqlite3.sqlite_version,
                "dependencies": {name: importlib.util.find_spec(name) is not None for name in ("watchdog", "pymysql", "requests")}}
    if mode == "path":
        path = Path(request["path"])
        st = path.stat()
        result = {"exists": True, "directory": path.is_dir(), "readable": os.access(str(path), os.R_OK),
                  "searchable": os.access(str(path), os.X_OK), "writable_hint": os.access(str(path), os.W_OK),
                  "owner_uid": st.st_uid, "owner_gid": st.st_gid,
                  "executor_uid": os.geteuid() if hasattr(os, "geteuid") else None,
                  "executor_gid": os.getegid() if hasattr(os, "getegid") else None,
                  "mode": oct(st.st_mode & 0o777), "device": st.st_dev}
        if path.is_dir():
            with os.scandir(str(path)) as entries:
                result["directory_read_ok"] = next(entries, None) is not None
            result["empty_hint"] = not result.pop("directory_read_ok")
        mounts = []
        if Path("/proc/mounts").exists():
            for line in Path("/proc/mounts").read_text().splitlines():
                fields = line.split()
                if len(fields) >= 3:
                    mount = fields[1].replace("\\040", " ").replace("\\134", "\\")
                    if str(path) == mount or str(path).startswith(mount.rstrip("/") + "/"):
                        mounts.append((len(mount), fields[2]))
        result["filesystem"] = max(mounts, default=(0, "미확인"))[1]
        if hasattr(os, "statvfs"):
            space = os.statvfs(str(path))
            result["available_bytes"] = space.f_bavail * space.f_frsize
            result["free_inodes"] = space.f_favail
        return result
    if mode == "plugin":
        root = Path(request["path"])
        text = (root / "info.yaml").read_text(encoding="utf-8")[:16384]
        match = re.search(r'''(?m)^version:\s*["']?([\w.\-]+)''', text)
        return {"version": match.group(1) if match else "미확인", "git_present": (root / ".git").exists(),
                "local_watch": (root / "local_folder_watch.py").is_file(),
                "duplicate_default_copy": str(root) != "/data/plugins/bookoasis_mate" and Path("/data/plugins/bookoasis_mate/info.yaml").is_file()}
    if mode == "http":
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        started = time.monotonic()
        try:
            with build_opener(NoRedirect()).open(Request(request["url"], headers={"User-Agent": "Mate-Diagnostics/1"}), timeout=8) as response:
                code = response.status
        except HTTPError as error:
            code = error.code
            error.close()
        except URLError as error:
            return {"error": type(error.reason).__name__, "seconds": round(time.monotonic() - started, 3)}
        return {"http": code, "seconds": round(time.monotonic() - started, 3)}
    if mode == "db":
        path = Path(request["path"])
        if not path.is_file():
            return {"error": "FileNotFoundError"}
        result = {"bytes": path.stat().st_size, "wal_bytes": Path(str(path) + "-wal").stat().st_size if Path(str(path) + "-wal").is_file() else 0}
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 8
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 200")}
            result["table_count_up_to_200"] = len(tables)
            if request.get("quick"):
                result["quick_check"] = [row[0] for row in connection.execute("PRAGMA quick_check(1)").fetchmany(1)]
            if request.get("mate"):
                allowed = {"db_engine", "general_db_path", "adult_db_path", "audiobook_db_path", "video_db_path",
                           "gdrive_scan_enabled", "gdrive_scan_input_mode", "gdrive_scan_builtin_poll_seconds",
                           "gdrive_scan_builtin_local_root", "gdrive_scan_path_mappings", "local_watch_enabled",
                           "local_watch_interval", "local_watch_debounce", "local_watch_roots"}
                settings = {}
                for table in sorted(tables):
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", table) or "setting" not in table.lower():
                        continue
                    columns = {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}
                    if {"key", "value"} <= columns:
                        query = 'SELECT "key", "value" FROM "' + table + '" WHERE "key" IN (' + ','.join('?' for _ in allowed) + ')'
                        settings.update(connection.execute(query, sorted(allowed)).fetchall())
                result["settings"] = settings
                for table in ("gdrive_scan_event", "local_folder_event"):
                    if table in tables:
                        result[table] = {"sample": connection.execute('SELECT status, COUNT(*) FROM (SELECT status FROM "' + table + '" ORDER BY id DESC LIMIT 5000) GROUP BY status').fetchall(),
                                         "latest_received": connection.execute('SELECT created_at FROM "' + table + '" ORDER BY id DESC LIMIT 1').fetchone()}
        finally:
            connection.close()
        return result
    raise ValueError("지원하지 않는 검사")


class Report:
    def __init__(self):
        self.lines = ["BookOasis Mate Linux 호스트 진단 v" + VERSION,
                      "UTC " + datetime.now(timezone.utc).isoformat(),
                      "원문 로그·환경변수·전체 설정·인증정보는 보고서에 포함하지 않습니다. 경로/컨테이너 이름은 공유 전 확인하세요.",
                      "정상은 해당 검사 범위에만 적용됩니다. 재시작·스캔·수정·복구는 실행하지 않았습니다."]
    def add(self, state, name, evidence, advice=""):
        line = redact("[{}] {}\n  근거. {}{}".format(state, name, evidence, "\n  다음 확인. " + advice if advice else ""))
        self.lines.append(line)
        print(line)
    def save(self, directory):
        directory = Path(directory).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ("mate-diagnosis-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".txt")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            summary = "검사 요약. " + " / ".join("{} {}건".format(state, sum(line.startswith("[" + state + "]") for line in self.lines))
                                                   for state in ("오류", "주의", "확인 불가", "정상"))
            stream.write("\n\n".join(self.lines[:4] + [summary] + self.lines[4:]) + "\n")
        return path.resolve()


def ask(label, default=""):
    return input(label + (" [" + default + "]" if default else "") + " > ").strip() or default


def choose(label, options, optional=False):
    print("\n" + label)
    if optional:
        print("0. 건너뛰기")
    for index, option in enumerate(options, 1):
        print(str(index) + ". " + redact(option))
    while True:
        value = ask("번호 선택", "0" if optional else "1")
        if value.isdigit() and 1 <= int(value) <= len(options):
            return options[int(value) - 1]
        if optional and value == "0":
            return None
        print("목록에 있는 번호를 입력하세요.")


def probe(container, python, mode, **values):
    source = "import json, sys\n" + inspect.getsource(container_probe) + "\ntry:\n print(json.dumps(container_probe(json.load(sys.stdin)), ensure_ascii=True))\nexcept Exception as e:\n print(json.dumps({'error': type(e).__name__}))\n"
    code, output = run_command(["docker", "exec", "-i", container, python, "-B", "-c", source], json.dumps(dict(mode=mode, **values)))
    if code != 0:
        return {"error": "검사 실패/시간 제한 (종료 코드 {})".format(code)}
    try:
        return json.loads(output)
    except ValueError:
        return {"error": "구조화된 응답 없음"}


def show_probe(report, label, result):
    state = "확인 불가" if "error" in result else "정상"
    if result.get("quick_check") not in (None, ["ok"]):
        state = "오류"
    if result.get("readable") is False or (result.get("directory") and result.get("searchable") is False):
        state = "오류"
    if result.get("empty_hint") or result.get("http", 200) >= 400:
        state = "주의"
    if result.get("duplicate_default_copy"):
        state = "주의"
    for table in ("gdrive_scan_event", "local_folder_event"):
        if any(status in {"failed", "retry"} and count for status, count in result.get(table, {}).get("sample", [])):
            state = "주의"
    report.add(state, label, json.dumps(result, ensure_ascii=False),
               "owner는 파일 소유자, executor는 검사 사용자이며 실제 앱 사용자와 다를 수 있습니다. 쓰기 검사는 os.access 참고값이며 파일을 만들지 않습니다." if "executor_uid" in result else "")


def check_settings(report, settings):
    if not settings:
        report.add("확인 불가", "Mate 설정", "허용된 설정 키를 찾지 못했습니다.", "Mate DB 경로·테이블 형식을 확인하세요.")
        return
    report.add("정상", "Mate 설정 읽기", json.dumps(settings, ensure_ascii=False))
    if str(settings.get("gdrive_scan_enabled")).lower() == "true" and settings.get("gdrive_scan_input_mode") == "builtin":
        report.add("주의", "Google Drive 수신 모드", "자체 감지 활성", "gd-poller 외부 이벤트는 거절됩니다. 경로 보류는 감시 밖 이동·삭제 후 경로 소실과 구분하세요. Drive API/체크포인트 자체는 미검사입니다.")
    if str(settings.get("local_watch_enabled")).lower() == "true":
        report.add("주의", "로컬 감지 기준", "설정에서 활성화됨. 실제 작업자 실행 여부는 이 값만으로 확정할 수 없습니다.", "최초/재시작 기준 수집은 과거 변경을 재수집하지 않습니다. CIFS/NFS/FUSE/Windows 마운트는 폴링을 사용하세요.")
        try:
            roots = json.loads(settings.get("local_watch_roots") or "[]")
            if not isinstance(roots, list) or not roots:
                raise ValueError()
            for row in roots:
                if not isinstance(row, dict) or not all(str(row.get(key, "")).startswith("/") for key in ("path", "target")):
                    raise ValueError()
        except (ValueError, TypeError):
            report.add("오류", "로컬 감시 경로 설정", "활성화 상태이나 경로 JSON이 비어 있거나 절대 경로 형식이 아닙니다.", "Mate 설정의 감시 경로와 BookOasis 대상 경로를 확인하세요.")
    if settings.get("db_engine") == "mariadb":
        report.add("확인 불가", "MariaDB", "설정만 확인. 인증·쿼리·잠금 상태 미검사", "Mate DB 진단 화면 결과를 추가로 제공하세요.")


def diagnose(report):
    report.add("정상", "호스트", "{} / {} / Python {}".format(platform.system(), platform.machine(), platform.python_version()))
    code, output = run_command(["docker", "ps", "-a", "--format", "{{.Names}}"])
    if code != 0:
        report.add("확인 불가", "Docker", "Docker CLI 실행 실패. 원문 출력은 저장하지 않았습니다.", "Docker 데몬·접근 권한을 확인하세요. 자동 sudo는 하지 않습니다.")
        return
    names = [name for name in output.splitlines() if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)]
    if not names:
        report.add("확인 불가", "Docker", "컨테이너 없음")
        return
    ff = choose("FlaskFarm 컨테이너", names, True)
    book = choose("BookOasis 컨테이너", names, True)
    symptom = choose("주요 증상", ["전체 기본 점검", "Google Drive / gd-poller 감지", "로컬 폴더 감지", "접속 / 화면 지연", "DB / 스캔 / 표지", "설치 / 버전 / Gitea"])
    report.add("정상", "사용자 선택", symptom)
    incident = ask("증상 발생 시각·짧은 설명 (비밀번호/토큰 입력 금지, Enter 생략)")
    if incident:
        report.add("주의", "사용자 설명 (검증 전)", incident)
    containers = {}
    for name in dict.fromkeys(n for n in (ff, book) if n):
        template = '{"State":{"Running":{{json .State.Running}},"Restarting":{{json .State.Restarting}},"OOMKilled":{{json .State.OOMKilled}},"StartedAt":{{json .State.StartedAt}}},"RestartCount":{{json .RestartCount}},"Image":{{json .Image}},"Config":{"Image":{{json .Config.Image}}},"HostConfig":{"NetworkMode":{{json .HostConfig.NetworkMode}}},"Mounts":{{json .Mounts}}}'
        code, output = run_command(["docker", "inspect", "--format", template, name])
        try:
            data = json.loads(output) if code == 0 else {}
        except ValueError:
            data = {}
        if not data:
            report.add("확인 불가", name, "inspect 실패")
            continue
        state = data.get("State", {})
        mounts = [{key: m.get(key) for key in ("Type", "Source", "Destination", "RW")} for m in data.get("Mounts", [])]
        report.add("정상" if state.get("Running") and not state.get("OOMKilled") else "오류", name + " 컨테이너",
                   json.dumps({"running": state.get("Running"), "restarting": state.get("Restarting"), "oom_killed": state.get("OOMKilled"),
                               "restart_count": data.get("RestartCount"), "started": state.get("StartedAt"),
                               "image": data.get("Config", {}).get("Image"), "image_id": data.get("Image"),
                               "network_mode": data.get("HostConfig", {}).get("NetworkMode"), "mounts": mounts}, ensure_ascii=False))
        if not state.get("Running"):
            continue
        python = None
        for executable in ("python3", "python"):
            code, _ = run_command(["docker", "exec", name, executable, "-B", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 2)"])
            if code == 0:
                python = executable
                break
        if not python:
            report.add("확인 불가", name + " Python", "컨테이너 Python 3.8+ 없음. 내부 검사를 건너뜁니다.")
            continue
        containers[name] = (python, mounts)
        show_probe(report, name + " 런타임", probe(name, python, "runtime"))
    if ff in containers:
        python, _ = containers[ff]
        plugin = ask("Mate 설치 경로 (FlaskFarm 내부)", "/data/plugins/bookoasis_mate")
        show_probe(report, "Mate 설치", probe(ff, python, "plugin", path=plugin))
        report.add("주의", "업데이트/중복 설치 판단", ".git 존재만으로 버전 복귀를 확정할 수 없습니다.", "실제 로딩 경로·path_dev 중복과 자동 업데이트 설정을 대조하세요.")
        db = ask("Mate SQLite DB 경로 (건너뛰기는 -)", "/data/db/bookoasis_mate.db")
        if db != "-":
            result = probe(ff, python, "db", path=db, mate=True)
            settings = result.pop("settings", {})
            show_probe(report, "Mate DB/이벤트 (최신 최대 5000건 표본)", result)
            check_settings(report, settings)
    if containers and ask("감시/자료 경로를 비교할까요? y/n", "y" if "감지" in symptom else "n").lower() == "y":
        mapped = []
        for role, name in (("Mate", ff), ("BookOasis", book)):
            if name not in containers:
                continue
            path = ask(role + " 컨테이너 내부 절대 경로 (건너뛰기는 -)")
            if not path.startswith("/") or path == "-":
                report.add("확인 불가", role + " 경로", "미입력/절대 경로 아님")
                continue
            python, mounts = containers[name]
            show_probe(report, role + " 경로 " + path, probe(name, python, "path", path=path))
            mapped.append(host_path(path, mounts))
        if len(mapped) == 2:
            report.add("정상" if mapped[0] and mapped[0] == mapped[1] else "주의", "호스트 마운트 매핑 비교", str(mapped),
                       "서로 다른 원본 경로도 같은 원격 자료를 가리킬 수 있습니다. 동일 경로 역시 내용 동일성을 보장하지 않습니다.")
    url = ask("BookOasis 접속 URL 원점 (예 http://192.168.1.10:5000, 건너뛰기는 Enter)")
    if url:
        try:
            url = origin_url(url)
        except ValueError as error:
            report.add("확인 불가", "URL", str(error))
        else:
            show_probe(report, "호스트 HTTP " + url, local_probe("http", url=url))
            if ff in containers:
                show_probe(report, "FlaskFarm HTTP " + url, probe(ff, containers[ff][0], "http", url=url))
            report.add("주의", "HTTP 검사 범위", "인증 없이 원점 / GET 1회. 리디렉션을 따라가지 않습니다.", "응답 시간은 API/브라우저 탭 전환 속도가 아닙니다. 401/403은 보호된 서비스의 정상 동작일 수도 있습니다.")
    if containers and ask("추가 SQLite DB quick_check를 실행할까요? 부하 발생 가능. y/N", "n").lower() == "y":
        name = choose("검사 DB가 보이는 컨테이너", list(containers))
        path = ask("검사할 SQLite DB 절대 경로")
        if path.startswith("/"):
            show_probe(report, "선택 DB quick_check", probe(name, containers[name][0], "db", path=path, quick=True))
            report.add("주의", "DB 검사 한계", "읽기 전용 단일 DB 검사. WAL 크기는 기록하되 수정하지 않습니다.", "ok라도 다른 DB·FTS 쿼리·실제 앱 오류까지 정상임을 의미하지 않습니다.")
    if ask("최근 1시간 Docker 로그에서 오류 패턴을 검사할까요? y/n", "y").lower() == "y":
        for name in dict.fromkeys(n for n in (ff, book) if n):
            code, output = run_command(["docker", "logs", "--since", "1h", "--tail", "300", name])
            if code != 0:
                report.add("확인 불가", name + " 로그", "조회 실패/시간 제한/출력 한도")
                continue
            findings = log_findings(output)
            if not findings:
                report.add("확인 불가", name + " 로그", "최근 최대 300줄에서 알려진 패턴 없음. 파일 로그와 오래된 오류는 미검사입니다.")
            for label, count, advice in findings:
                report.add("주의", name + " 로그 후보 / " + label, "패턴 {}회. 원문 미포함; 현재 장애라고 확정할 수 없음.".format(count), advice)
    report.add("확인 불가", "추가 범위", "실제 Drive API·체크포인트 내용·Gitea 인증·MariaDB 쿼리·Redis 데이터·브라우저 성능·파일 로그는 미검사입니다.",
               "이 보고서와 증상 발생 시각을 함께 전달하세요. 필요하면 해당 항목만 추가 진단합니다.")


def local_probe(mode, **values):
    source = "import json,sys\n" + inspect.getsource(container_probe) + "\ntry:\n print(json.dumps(container_probe(json.load(sys.stdin))))\nexcept Exception as e:\n print(json.dumps({'error': type(e).__name__}))\n"
    code, output = run_command([sys.executable, "-B", "-c", source], json.dumps(dict(mode=mode, **values)))
    try:
        return json.loads(output) if code == 0 else {"error": "호스트 검사 실패/시간 제한"}
    except ValueError:
        return {"error": "호스트 응답 해석 실패"}


def main():
    parser = argparse.ArgumentParser(description="Linux 호스트용 대화형 Mate 진단. Python 3.8+와 Docker CLI 필요. 서비스 변경 없음.")
    parser.add_argument("--output-dir", default=".", help="텍스트 보고서 저장 폴더 (기본 현재 폴더)")
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("Linux 호스트에서 실행하세요. Windows/macOS는 지원하지 않습니다.")
    if sys.version_info < (3, 8):
        parser.error("Python 3.8 이상이 필요합니다.")
    report = Report()
    print("읽기 전용 진단입니다. 보고서 파일만 생성합니다. Docker 권한이 필요하며 자동 sudo는 하지 않습니다.")
    try:
        if not shutil.which("docker"):
            report.add("확인 불가", "Docker", "CLI가 PATH에 없습니다.")
        else:
            diagnose(report)
    except (KeyboardInterrupt, EOFError):
        report.add("확인 불가", "진단 중단", "사용자 중단/입력 종료. 부분 결과를 저장합니다.")
    except Exception as error:
        report.add("확인 불가", "진단 예외", type(error).__name__, "부분 결과입니다. 원문 예외는 민감정보 보호를 위해 제외했습니다.")
    try:
        path = report.save(args.output_dir)
    except OSError as error:
        print("보고서 저장 실패. " + type(error).__name__ + ". 위 콘솔 결과를 확인하세요.", file=sys.stderr)
        return 1
    print("\n보고서 저장. " + str(path) + "\n공유 전 경로·컨테이너 이름을 확인하세요. 자동 업로드하지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
