"""One serial ingestion owner; persistent history never implies automatic replay.

The global instance resolves its private ledger lazily. Plain Worker() remains
in-memory for existing isolated callers/tests; construction opens no database.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import logging
import math
import queue
import secrets
import threading
import time
from typing import Callable, Optional

from .ingestion_jobs import (
    IngestionJobLedger, IngestionOwnerLock, job_metadata as checked_metadata,
    progress_values, task_identity,
)

logger = logging.getLogger(__name__)


class WorkerBusyError(ValueError):
    """A local submission or maintenance action cannot safely start yet."""


class Worker:
    def __init__(self, ledger_path=None, *, clock=None, ledger_path_factory=None):
        if ledger_path is not None and ledger_path_factory is not None:
            raise ValueError("ingestion_ledger_path_ambiguous")
        self._ledger_path, self._ledger_path_factory = ledger_path, ledger_path_factory
        self._clock = clock or time.time
        self._ledger = None
        self._ledger_prepared = False
        self._owner_lock = None
        self._owner_token = secrets.token_hex(16)
        self._queue = queue.Queue()
        self._thread = self._new_thread()
        self._current_task_id = None
        self._tasks = {}
        self._lock = threading.Lock()
        # Short transitions only; callbacks and joins hold neither mutex.
        self._transition_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._running = False
        self._accepting = True
        self._submission_reservations = 0
        self._maintenance_active = False
        self._state_recording_errors = set()

    def _new_thread(self):
        return threading.Thread(target=self._run, daemon=True, name="knowledge-worker")

    def _get_ledger(self, *, create=False, owner=False):
        with self._lifecycle_lock:
            if self._ledger is None:
                path = self._ledger_path
                if path is None and self._ledger_path_factory is not None:
                    path = self._ledger_path_factory()
                if path is None:
                    return None
                self._ledger = IngestionJobLedger(path, clock=self._clock)
            if create and not self._ledger_prepared:
                self._ledger.prepare()
                self._ledger_prepared = True
            if owner and self._owner_lock is None:
                lock = IngestionOwnerLock(self._ledger.path)
                lock.acquire()
                try:
                    self._ledger.recover_previous(self._owner_token)
                except BaseException:
                    lock.release()
                    raise
                self._owner_lock = lock
            return self._ledger

    def _release_owner(self):
        with self._lifecycle_lock:
            if self._owner_lock is not None:
                self._owner_lock.release()
                self._owner_lock = None

    @contextmanager
    def submission_guard(self):
        """Reserve before business SQL, without holding the worker mutex."""
        with self._lock:
            if self._maintenance_active or not self._accepting:
                raise WorkerBusyError("正在恢复或停止入库服务，请稍后重新提交。")
            self._submission_reservations += 1
        try:
            yield
        finally:
            with self._lock:
                self._submission_reservations -= 1

    @contextmanager
    def maintenance_guard(self):
        """One-process exclusion; a dequeued job remains visible as queued."""
        with self._lock:
            busy = (self._maintenance_active or self._submission_reservations
                    or self._current_task_id is not None
                    or any(task["status"] in {"queued", "running"} for task in self._tasks.values()))
            if busy:
                raise WorkerBusyError("有任务正在处理或排队，请等待任务结束后再重置。")
            self._maintenance_active = True
        try:
            yield
        finally:
            with self._lock:
                self._maintenance_active = False

    def start(self):
        with self._lifecycle_lock:
            with self._lock:
                if self._running:
                    return
                if self._thread.is_alive():
                    raise WorkerBusyError("当前入库任务尚未退出，请等待其结束。")
                if self._state_recording_errors:
                    raise WorkerBusyError("入库任务记录尚未确认，请保留现场并重新核对服务状态。")
            self._get_ledger(create=True, owner=True)
            with self._lock:
                if self._thread.ident is not None:
                    self._thread = self._new_thread()
                self._running, self._accepting = True, True
                self._thread.start()
        logger.info("后台工作线程已启动")

    def _cache(self, task_id, snapshot):
        with self._lock:
            self._tasks[task_id] = deepcopy(snapshot)
            if snapshot["status"] in {"done", "failed", "interrupted", "needs_review"}:
                self._state_recording_errors.discard(task_id)

    def _persistence_failure(self, task_id):
        with self._lock:
            self._state_recording_errors.add(task_id)
            task = self._tasks.get(task_id)
            if task is not None:
                task.update(persistence_error=True, needs_review=True,
                            message="任务记录暂未确认，请核对原资料与调用结果。")
        logger.error("入库任务状态记录未确认: %s", task_id)

    def _finish(self, task_id, status, *, reason_code=None):
        with self._transition_lock:
            ledger = self._get_ledger()
            if ledger is not None:
                snapshot = ledger.finish(task_id, self._owner_token, status, reason_code=reason_code)
            else:
                with self._lock:
                    snapshot = deepcopy(self._tasks[task_id])
                now = self._clock()
                snapshot.update(status=status, reason_code=reason_code, updated_at=now, finished_at=now,
                    revision=snapshot["revision"] + 1, needs_review=status in {"interrupted", "needs_review"})
                snapshot["message"] = {"done": "处理完成", "failed": "处理失败，请核对任务记录。",
                    "interrupted": "处理已中断，请核对原资料与调用结果。", "needs_review": "任务未继续执行，请明确重新提交。"}[status]
                if status == "done":
                    snapshot.update(progress=snapshot["total"], stage="finished")
            self._cache(task_id, snapshot)

    def _quarantine_queued(self):
        with self._transition_lock:
            with self._lock:
                queued = [key for key, item in self._tasks.items() if item["status"] == "queued"]
            for key in queued:
                try:
                    self._finish(key, "needs_review", reason_code="worker_stopped_before_start")
                except Exception:
                    self._persistence_failure(key)
            return len(queued)

    def _discard_pending_entries(self):
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return
            self._queue.task_done()

    def stop(self, timeout=30.0):
        """Stop new claims; a bounded wait/timeout never cancels a paid call.

        On timeout its durable flag remains observable, and the OS owner lock
        stays held until the actual worker thread exits.
        """
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 3600:
            raise ValueError("invalid_worker_stop_timeout")
        deadline = time.monotonic() + timeout
        with self._lock:
            self._running, self._accepting = False, False
            current = self._current_task_id
        pending = self._quarantine_queued()
        self._discard_pending_entries()
        ledger = self._get_ledger()
        if current and ledger is not None:
            try:
                ledger.shutdown(current, self._owner_token)
            except Exception:
                self._persistence_failure(current)
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(max(0, deadline - time.monotonic()))
        stopped = not self._thread.is_alive()
        with self._lock:
            current = self._current_task_id
        if not stopped and current:
            if ledger is not None:
                try:
                    ledger.shutdown(current, self._owner_token, timed_out=True)
                except Exception:
                    self._persistence_failure(current)
            with self._lock:
                if current in self._tasks:
                    self._tasks[current].update(shutdown_timed_out=True, needs_review=True)
        if stopped:
            self._release_owner()
        result = {"stopped": stopped, "timed_out": not stopped,
                  "current_task_id": current, "pending_count": pending}
        with self._lock:
            if self._state_recording_errors:
                result["state_recorded"] = False
        return result

    def submit(self, task_id: str, func: Callable, *args, job_metadata=None, **kwargs):
        task_identity(task_id)
        if not callable(func):
            raise ValueError("invalid_worker_callable")
        metadata = checked_metadata(job_metadata)
        with self.submission_guard(), self._transition_lock:
            with self._lock:
                if task_id in self._tasks:
                    raise ValueError("任务编号已存在，请重新提交。")
                selected = set(metadata.get("platform_item_ids", []))
                if selected and any(selected.intersection(item.get("metadata", {}).get("platform_item_ids", []))
                        for item in self._tasks.values() if item["status"] in {"queued", "running"}):
                    raise WorkerBusyError("所选视频已有正在处理或排队的任务，请刷新原任务后再操作。")
                now = self._clock()
                self._tasks[task_id] = {"task_id": task_id, "status": "queued", "stage": "queued", "progress": 0,
                    "total": len(metadata.get("platform_item_ids", [])), "message": "等待处理...", "metadata": metadata,
                    "reason_code": None, "created_at": now, "updated_at": now, "started_at": None, "finished_at": None,
                    "shutdown_requested_at": None, "shutdown_timed_out": False, "needs_review": False,
                    "usage_state": "not_recorded", "current_item_id": None, "revision": 1}
            ledger = None
            try:
                ledger = self._get_ledger(create=True, owner=True)
                if ledger is not None:
                    self._cache(task_id, ledger.register(task_id, self._owner_token, metadata))
                self._queue.put_nowait((task_id, func, args, kwargs))
            except BaseException:
                with self._lock:
                    self._tasks.pop(task_id, None)
                if ledger is not None:
                    try:
                        ledger.publication_unconfirmed(task_id, self._owner_token)
                    except Exception:
                        self._persistence_failure(task_id)
                raise
        logger.info("任务已提交: %s", task_id)

    def get_progress(self, task_id: str) -> Optional[dict]:
        with self._lock:
            cached = deepcopy(self._tasks.get(task_id))
            unconfirmed = task_id in self._state_recording_errors
        ledger = self._get_ledger()
        if ledger is not None:
            value = ledger.get(task_id)
            if value is not None:
                if unconfirmed:
                    value.update(persistence_error=True, needs_review=True,
                        message="任务记录暂未确认，请核对原资料与调用结果。")
                return value
        return cached

    def list_tasks(self, limit=50, offset=0):
        ledger = self._get_ledger()
        if ledger is not None:
            records = ledger.list(limit=limit, offset=offset)
            with self._lock:
                uncertain = set(self._state_recording_errors)
            for record in records:
                if record["task_id"] in uncertain:
                    record.update(persistence_error=True, needs_review=True,
                        message="任务记录暂未确认，请核对原资料与调用结果。")
            return records
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("invalid_ingestion_job_page")
        with self._lock:
            values = sorted(self._tasks.values(), key=lambda item: (item["created_at"], item["task_id"]), reverse=True)
            return deepcopy(values[offset:offset + limit])

    def get_current_task(self) -> Optional[str]:
        with self._lock:
            return self._current_task_id

    def count_tasks(self) -> int:
        ledger = self._get_ledger()
        if ledger is not None:
            return ledger.count()
        with self._lock:
            return len(self._tasks)

    def active_video_tasks(self):
        """Return declared local video reservations, without inferring old scope."""
        with self._lock:
            return {video: task_id for task_id, item in self._tasks.items()
                    if item["status"] in {"queued", "running"}
                    for video in item.get("metadata", {}).get("platform_item_ids", [])}

    def _run(self):
        try:
            while self._running:
                try:
                    task_id, func, args, kwargs = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                claimed = False
                try:
                    with self._transition_lock:
                        with self._lock:
                            task = self._tasks.get(task_id)
                            valid = self._running and task is not None and task["status"] == "queued"
                            if valid:
                                self._current_task_id = task_id
                        if not valid:
                            continue
                        ledger = self._get_ledger()
                        if ledger is not None:
                            snapshot = ledger.start_job(task_id, self._owner_token)
                        else:
                            snapshot = deepcopy(task)
                            snapshot.update(status="running", stage="starting", message="正在处理...",
                                started_at=self._clock(), updated_at=self._clock(), revision=task["revision"] + 1)
                        self._cache(task_id, snapshot)
                        claimed = True
                    try:
                        func(*args, task_id=task_id, **kwargs)
                    except Exception:
                        self._finish(task_id, "failed", reason_code="ingestion_job_failed")
                        logger.error("入库任务执行失败，已保留阶段与进度: %s", task_id)
                    except BaseException:
                        self._finish(task_id, "interrupted", reason_code="ingestion_job_interrupted")
                        with self._lock:
                            self._running, self._accepting = False, False
                    else:
                        self._finish(task_id, "done")
                except BaseException:
                    self._persistence_failure(task_id)
                    with self._lock:
                        self._running, self._accepting = False, False
                    if not claimed:
                        try:
                            self._finish(task_id, "needs_review", reason_code="ingestion_job_start_unconfirmed")
                        except Exception:
                            pass
                finally:
                    with self._lock:
                        if self._current_task_id == task_id:
                            self._current_task_id = None
                    self._queue.task_done()
        finally:
            with self._lock:
                self._running = False
            self._quarantine_queued()
            self._discard_pending_entries()
            self._release_owner()

    def update_progress(self, task_id: str, progress: int, total: int, message: str = "", *, stage=None, item_id=None):
        progress_values(progress, total, message, stage)
        # An item hint cannot expand the batch frozen in metadata.
        with self._transition_lock:
            with self._lock:
                task = deepcopy(self._tasks.get(task_id))
            if task is None or task["status"] != "running":
                return
            ledger = self._get_ledger()
            if ledger is not None:
                snapshot = ledger.progress(task_id, self._owner_token, progress, total, message, stage=stage, item_id=item_id)
            else:
                snapshot = task
                snapshot.update(progress=progress, total=total, updated_at=self._clock(), revision=task["revision"] + 1)
                if message:
                    snapshot["message"] = message
                if stage is not None:
                    snapshot["stage"] = stage
                if item_id is not None:
                    snapshot["current_item_id"] = item_id
            self._cache(task_id, snapshot)


def _configured_ledger_path():
    from app.core.config import settings
    return settings.ingestion_job_db_path


worker = Worker(ledger_path_factory=_configured_ledger_path)
