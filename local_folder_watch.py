# 로컬·네트워크 폴더 변경을 별도 프로세스에서 감지하고 검증된 이벤트를 전달합니다.
import fnmatch
import json
import os
import posixpath
import stat
import sys
import threading
import time


def overlaps(left, right):
    left, right = left.rstrip("/"), right.rstrip("/")
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def validate_roots(value, drive_roots=()):
    roots = json.loads(value) if isinstance(value, str) else value
    if not isinstance(roots, list) or not 1 <= len(roots) <= 10:
        raise ValueError("감시 경로를 1~10개 등록해 주세요.")
    result = []
    for row in roots:
        if not isinstance(row, dict):
            raise ValueError("감시 경로 형식이 올바르지 않습니다.")
        item = {}
        for key in ("path", "target"):
            path = str(row.get(key) or "").strip()
            if not path.startswith("/") or path.startswith("//") or "\\" in path or "\x00" in path or ".." in path.split("/"):
                raise ValueError("컨테이너에서 보이는 Linux 절대 경로를 입력해 주세요.")
            item[key] = posixpath.normpath(path)
            if item[key] == "/":
                raise ValueError("파일시스템 전체 루트는 감시할 수 없습니다.")
        item["mode"] = str(row.get("mode") or "auto")
        if item["mode"] not in {"auto", "native", "polling"}:
            raise ValueError("감지 방식이 올바르지 않습니다.")
        for previous in result:
            if overlaps(previous["path"], item["path"]) or overlaps(previous["target"], item["target"]):
                raise ValueError("감시 경로나 BookOasis 경로가 서로 겹칩니다.")
        if any(overlaps(item["target"], path) for path in drive_roots if path):
            raise ValueError("Google Drive 자체 감지 경로와 겹칩니다. 한 방식만 사용해 주세요.")
        result.append(item)
    return result


def filesystem_type(path):
    matches = []
    with open("/proc/mounts", encoding="utf-8") as stream:
        for line in stream:
            fields = line.split()
            if len(fields) < 3:
                continue
            mount = fields[1].replace("\\040", " ").replace("\\134", "\\")
            if path == mount or path.startswith(mount.rstrip("/") + "/"):
                matches.append((len(mount), fields[2]))
    return max(matches, default=(0, "unknown"))[1]


class PollingRoot:
    def __init__(self, root, max_entries=200000, ignore_patterns=(), extensions=None):
        self.root = root
        self.ignore_patterns = tuple(ignore_patterns)
        self.extensions = None if extensions is None else frozenset(extensions)
        self.max_entries = max_entries
        self.snapshot = None
        self.identity = None
        self.file_count = self.directory_count = 0

    def included(self, relative, directory):
        parts = relative.split('/')
        for index, name in enumerate(parts):
            is_directory = index < len(parts) - 1 or directory
            prefix = '/'.join(parts[:index + 1])
            if any((is_directory or not pattern.endswith('/')) and
                   fnmatch.fnmatchcase(prefix if '/' in pattern.rstrip('/') else name, pattern.rstrip('/'))
                   for pattern in self.ignore_patterns):
                return False
        return directory or self.extensions is None or parts[-1].lower() == '.bookoasisignore' or posixpath.splitext(relative)[1].lower() in self.extensions

    def collect(self, accept=None, paths=None):
        root = self.root["path"]
        if os.path.islink(root) or not os.path.isdir(root):
            raise OSError("감시 루트에 접근할 수 없습니다. 마운트와 권한을 확인하세요.")
        info = os.stat(root, follow_symlinks=False)
        identity = (info.st_dev, info.st_ino)
        if self.identity is not None and self.identity != identity:
            raise ValueError("루트 식별 정보가 변경되었습니다. 마운트 확인 후 기준을 재설정하세요.")
        full = paths is None or self.snapshot is None
        scopes = set()
        if not full:
            for path in paths:
                if path == '':
                    full = True
                    break
                if path.startswith('/') or '\\' in path or any(part in {'', '.', '..'} for part in path.split('/')):
                    raise ValueError('감시 범위 밖 변경 경로를 거부했습니다.')
                scopes.add(path)
            scopes = {path for path in scopes if not any('/'.join(path.split('/')[:i]) in scopes for i in range(1, len(path.split('/'))))}
        old = self.snapshot or {}
        if not full:
            previous = {}
            for path in scopes:
                if path in old:
                    previous[path] = old[path]
                    if old[path][0]:
                        prefix = path + '/'
                        previous.update((key, value) for key, value in old.items() if key.startswith(prefix))
            old = previous
        current = {}
        stack = [root] if full else []
        base_size = 0 if full else len(self.snapshot) - len(old)

        def record(path, relative, directory, info):
            current[relative] = (directory, info.st_size, info.st_mtime_ns)
            if base_size + len(current) > self.max_entries:
                raise ValueError('감시 항목 한도를 초과했습니다 ({} / {} 항목). 대상 파일·폴더 기준이며 감시 범위나 한도 설정을 확인하세요.'.format(base_size + len(current), self.max_entries))
            if directory:
                stack.append(path)

        if not full:
            for relative in scopes:
                path = root
                try:
                    for part in relative.split('/'):
                        path = os.path.join(path, part)
                        entry_info = os.lstat(path)
                        if stat.S_ISLNK(entry_info.st_mode):
                            break
                    else:
                        directory = stat.S_ISDIR(entry_info.st_mode)
                        if (directory or stat.S_ISREG(entry_info.st_mode)) and self.included(relative, directory):
                            record(path, relative, directory, entry_info)
                except (FileNotFoundError, NotADirectoryError):
                    pass
        while stack:
            parent = stack.pop()
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    directory = entry.is_dir(follow_symlinks=False)
                    relative = os.path.relpath(entry.path, root).replace(os.sep, "/")
                    if not self.included(relative, directory):
                        continue
                    if not directory and not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    record(entry.path, relative, directory, st)
        after = os.stat(root, follow_symlinks=False)
        if identity != (after.st_dev, after.st_ino):
            raise OSError("조회 중 마운트가 변경되었습니다. 기준은 유지합니다.")
        events = []
        if self.snapshot is not None:
            removed = old.keys() - current.keys()
            if removed and (base_size + len(current) == 0 or len(removed) >= 100 or (len(removed) >= 20 and len(removed) >= len(self.snapshot) * .2)):
                raise ValueError("대량 삭제 또는 빈 마운트 의심. 확인 후 기준 재설정 또는 수동 스캔이 필요합니다.")
            for path in sorted(removed):
                events.append(self.event("delete", path, old[path][0]))
            for path, signature in current.items():
                previous = old.get(path)
                if previous is None:
                    events.append(self.event("create", path, signature[0]))
                elif previous[0] != signature[0]:
                    events.append(self.event("delete", path, previous[0]))
                    events.append(self.event("create", path, signature[0]))
                elif previous != signature and not signature[0]:
                    events.append(self.event("edit", path, False))
                if len(events) > 10000:
                    raise ValueError("한 번에 10,000건을 초과한 변경입니다. 수동 스캔 후 기준을 재설정하세요.")
        if events and accept:
            if len(events) > 10000:
                raise ValueError("한 번에 10,000건을 초과한 변경입니다. 범위를 줄이거나 수동 스캔 후 기준을 재설정하세요.")
            accept(events)
        directories = sum(value[0] for value in current.values())
        old_directories = sum(value[0] for value in old.values())
        self.directory_count = (0 if full else self.directory_count - old_directories) + directories
        self.file_count = base_size + len(current) - self.directory_count
        if full:
            self.snapshot = current
        else:
            for path in old:
                self.snapshot.pop(path, None)
            self.snapshot.update(current)
        self.identity = identity
        return events

    def event(self, action, path, directory):
        target = posixpath.join(self.root["target"], path)
        return {"action": action, "item_type": "directory" if directory else "file",
                "path": target, "removed_path": target if action == "delete" else ""}


def emit(message):
    print(json.dumps(message, ensure_ascii=True), flush=True)


def run(config):
    parent_pid = os.getppid()
    roots = validate_roots(config["roots"])
    interval = max(30, min(int(config.get("interval", 300)), 86400))
    debounce = max(2, min(int(config.get("debounce", 10)), 120))
    max_entries = int(config.get("max_entries", 200000))
    if not 1 <= max_entries <= 1000000:
        raise ValueError("감시 항목 한도는 1~1000000 사이여야 합니다.")
    monitors = []
    observers = []
    local_types = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "tmpfs", "overlay"}

    def accept(events):
        emit({"events": events})
        # 영속 저장 확인 전에는 비교 기준을 진행하지 않습니다.
        if sys.stdin.readline().strip() != "ok":
            raise RuntimeError("이벤트 영속 저장 확인을 받지 못했습니다.")

    try:
        for root in roots:
            monitor = PollingRoot(root, max_entries=max_entries, ignore_patterns=config.get("ignore_patterns", ()), extensions=config.get("extensions"))
            state = {"monitor": monitor, "next": 0, "changed": 0, "first": 0, "lock": threading.Lock(), "mode": "polling", "pending": set(), "full": True, "reconcile": 0}
            try:
                fstype = filesystem_type(root["path"])
                native = root["mode"] == "native" or (root["mode"] == "auto" and fstype in local_types)
                if native:
                    if fstype not in local_types:
                        raise ValueError("이 파일시스템은 실시간 감지 대신 폴링을 선택해 주세요.")
                    try:
                        from watchdog.events import FileSystemEventHandler
                        from watchdog.observers import Observer
                    except ImportError as error:
                        raise RuntimeError("실시간 감지에 watchdog이 필요합니다. FlaskFarm 컨테이너의 감지 Python에서 python -m pip install 'watchdog>=4,<7' 실행 후 다시 시작하세요. 폴링에는 필요하지 않습니다.") from error

                    class Handler(FileSystemEventHandler):
                        def __init__(self, target):
                            self.target = target

                        def on_any_event(self, event):
                            if event.event_type not in {"created", "modified", "deleted", "moved"}:
                                return
                            if event.event_type == "modified" and event.is_directory:
                                return
                            with self.target["lock"]:
                                for path in (event.src_path, getattr(event, 'dest_path', '')):
                                    if not path:
                                        continue
                                    relative = os.path.relpath(path, self.target["monitor"].root["path"]).replace(os.sep, '/')
                                    if relative == '..' or relative.startswith('../') or os.path.isabs(relative):
                                        continue
                                    if relative == '.':
                                        self.target["full"] = True
                                    elif self.target["monitor"].included(relative, event.is_directory) and not self.target["full"]:
                                        self.target["pending"].add(relative)
                                    if len(self.target["pending"]) > 10000:
                                        self.target["full"] = True
                                        self.target["pending"].clear()
                                now = time.monotonic()
                                self.target["changed"] = now
                                self.target["first"] = self.target["first"] or now

                    observer = Observer()
                    observer.schedule(Handler(state), root["path"], recursive=True)
                    observer.start()
                    observers.append(observer)
                    state["observer"] = observer
                    state["mode"] = "native"
                monitors.append(state)
                emit({"path": root["path"], "mode": state["mode"], "status": "기준 수집 중", "filesystem": fstype, "max_entries": max_entries})
            except Exception as error:
                emit({"path": root["path"], "status": "오류", "error": str(error)})
        if not monitors:
            return
        while True:
            if os.getppid() != parent_pid:
                return
            now = time.monotonic()
            for state in monitors:
                monitor = state["monitor"]
                with state["lock"]:
                    dirty = state["changed"] and (now - state["changed"] >= debounce or now - state["first"] >= 30)
                    full = state["full"] or state["mode"] == "polling" or now >= state["reconcile"] or monitor.snapshot is None
                    due = now >= state["next"] and (full or dirty)
                    if not due:
                        continue
                    state["changed"] = state["first"] = 0
                    pending = state["pending"]
                    state["pending"] = set()
                    state["full"] = False
                try:
                    if state.get("observer") and not state["observer"].is_alive():
                        raise RuntimeError("실시간 감지 작업이 종료되었습니다. inotify 한도·권한 확인 후 재시작하세요.")
                    emit({"path": monitor.root["path"], "status": "확인 중", "scan_scope": "전체" if full else "변경 범위"})
                    if full:
                        monitor.collect(accept)
                        state["reconcile"] = time.monotonic() + 3600
                    else:
                        monitor.collect(accept, paths=pending)
                    emit({"path": monitor.root["path"], "mode": state["mode"], "status": "감시 중", "entries": len(monitor.snapshot), "file_count": monitor.file_count, "directory_count": monitor.directory_count, "max_entries": max_entries, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "error": ""})
                    state["next"] = now + (interval if state["mode"] == "polling" else 2)
                except Exception as error:
                    emit({"path": monitor.root["path"], "status": "보류", "error": str(error)})
                    state["next"] = now + interval
                    with state["lock"]:
                        state["changed"] = state["first"] = now
                        state["full"] = state["full"] or full
                        state["pending"].update(pending)
                        if state["full"] or len(state["pending"]) > 10000:
                            state["full"] = True
                            state["pending"].clear()
            time.sleep(1)
    finally:
        for observer in observers:
            observer.stop()
        for observer in observers:
            observer.join(timeout=2)


if __name__ == "__main__":
    try:
        run(json.loads(sys.stdin.readline()))
    except Exception as error:
        emit({"status": "오류", "error": str(error)})
        sys.exit(1)
