# 라이브러리 통계 스냅샷 복원과 위젯 계산 중복 억제를 담당합니다.
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor


class _InflightWidget:
    def __init__(self):
        self.event = threading.Event()
        self.error = None


class StatisticsWidgetCache:
    def __init__(self, ttl_seconds=300, max_entries=64, clock=None):
        self.ttl_seconds = max(1, int(ttl_seconds or 300))
        self.max_entries = max(1, int(max_entries or 64))
        self.clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._values = {}
        self._inflight = {}

    def _prune_locked(self, now):
        self._values = {
            key: value
            for key, value in self._values.items()
            if now - value[0] <= self.ttl_seconds
        }
        while len(self._values) >= self.max_entries:
            oldest_key = min(self._values, key=lambda key: self._values[key][0])
            self._values.pop(oldest_key, None)

    def get_or_compute(self, key, compute):
        now = self.clock()
        with self._lock:
            self._prune_locked(now)
            cached = self._values.get(key)
            if cached is not None:
                return copy.deepcopy(cached[1])
            inflight = self._inflight.get(key)
            if inflight is None:
                inflight = _InflightWidget()
                self._inflight[key] = inflight
                leader = True
            else:
                leader = False

        if not leader:
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            with self._lock:
                cached = self._values.get(key)
                if cached is None:
                    raise RuntimeError("통계 위젯 계산 결과를 찾을 수 없습니다.")
                return copy.deepcopy(cached[1])

        try:
            result = compute()
            with self._lock:
                self._prune_locked(self.clock())
                self._values[key] = (self.clock(), copy.deepcopy(result))
            return result
        except Exception as error:
            inflight.error = error
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
                inflight.event.set()

    def clear(self):
        with self._lock:
            self._values = {}


def run_widget_tasks(tasks, max_workers=3):
    task_items = list((tasks or {}).items())
    if not task_items:
        return {}
    worker_count = max(1, min(int(max_workers or 1), len(task_items)))
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="bookoasis-statistics",
    ) as executor:
        futures = {
            name: executor.submit(task)
            for name, task in task_items
        }
        return {
            name: futures[name].result()
            for name, unused_task in task_items
        }


def build_cached_statistics_status(snapshot):
    result = copy.deepcopy((snapshot or {}).get("result") or {})
    return {
        "is_working": "done",
        "job_type": "library_statistics",
        "db_type": str((snapshot or {}).get("db_type") or "general"),
        "library_id": str((snapshot or {}).get("library_id") or ""),
        "library_name": str(result.get("library_name") or "전체 보관함"),
        "stage": "cached",
        "current": 100,
        "total": 100,
        "progress_percent": 100,
        "started_at": "",
        "finished_at": str((snapshot or {}).get("created_at") or ""),
        "elapsed_seconds": 0,
        "message": "저장된 통계를 먼저 표시하고 원본 변경 여부를 확인하고 있습니다.",
        "result": result,
        "error": "",
        "stopped": False,
        "cached": True,
        "stale": False,
        "validation_pending": True,
        "validation_error": "",
        "snapshot_id": int((snapshot or {}).get("id") or 0),
    }


DEFAULT_STATISTICS_WIDGET_CACHE = StatisticsWidgetCache()
