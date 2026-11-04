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
import sqlite3
import secrets
import threading
import time
from typing import Callable, Optional

from .ingestion_jobs import (
    IngestionJobLedger, IngestionJobError, IngestionOwnerLock, job_metadata as checked_metadata,
    progress_values, task_identity, checked_kind, record_kind, TaskKind,
)

logger = logging.getLogger(__name__)
_ANY_OWNER = object()


def _deadline_wait_failed(error):
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, sqlite3.OperationalError):
        code = getattr(error, 'sqlite_errorcode', None)
        if type(code) is int and (code & 255) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_INTERRUPT}:
            return True
    return error.args in (('ingestion_job_deadline_exhausted',), ('storage_guard_timeout',),
                         ('storage_guard_initialization_timeout',))


class WorkerBusyError(ValueError):
    """A local submission or maintenance action cannot safely start yet."""


@contextmanager
def _bounded_lock(lock, deadline=None):
    acquired = (lock.acquire() if deadline is None else
                lock.acquire(timeout=max(0.0, deadline - time.monotonic())))
    if not acquired:
        raise TimeoutError("worker_shutdown_deadline_exhausted")
    try:
        yield
    finally:
        lock.release()


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
        self._stop_requested = threading.Event()
        self._stop_epoch = 0
        self._run_epoch = 0
        self._stop_state_lock = threading.Lock()
        self._pending_stops = set()
        self._shutdown_in_progress = threading.Event()
        self._shutdown_state_unconfirmed = False

    def _new_thread(self):
        return threading.Thread(target=self._run, daemon=True, name="knowledge-worker")

    def _admission_current(self, epoch):
        return (epoch == self._stop_epoch and self._accepting
                and not self._stop_requested.is_set() and not self._shutdown_in_progress.is_set())

    def _get_ledger(self, *, create=False, owner=False, admission_epoch=None):
        with self._lifecycle_lock:
            if owner and (self._shutdown_in_progress.is_set()
                    or admission_epoch is not None and not self._admission_current(admission_epoch)):
                raise WorkerBusyError("入库服务正在停止，请核对状态后再启动。")
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
                    if (self._shutdown_in_progress.is_set()
                            or admission_epoch is not None and not self._admission_current(admission_epoch)):
                        raise WorkerBusyError("入库服务正在停止，请核对状态后再启动。")
                    self._ledger.upgrade_owned_schema()
                    self._ledger.recover_previous(self._owner_token)
                    if (self._shutdown_in_progress.is_set()
                            or admission_epoch is not None and not self._admission_current(admission_epoch)):
                        raise WorkerBusyError("入库服务正在停止，请核对状态后再启动。")
                except BaseException:
                    lock.release()
                    raise
                self._owner_lock = lock
            return self._ledger

    def _release_owner(self, *, deadline=None, expected_thread=None, expected_epoch=None,
                       expected_owner=_ANY_OWNER, expected_token=None):
        with _bounded_lock(self._lifecycle_lock, deadline):
            if (expected_thread is not None and self._thread is not expected_thread
                    or expected_epoch is not None and self._run_epoch != expected_epoch
                    or expected_token is not None and self._owner_token != expected_token
                    or expected_owner is not _ANY_OWNER and self._owner_lock is not None
                    and self._owner_lock is not expected_owner):
                return False
            if self._owner_lock is not None:
                self._owner_lock.release()
                self._owner_lock = None
            return True

    @contextmanager
    def submission_guard(self):
        """Reserve before business SQL, without holding the worker mutex."""
        with self._lock:
            if self._maintenance_active or not self._accepting or self._stop_requested.is_set():
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
        if self._shutdown_in_progress.is_set():
            raise WorkerBusyError("入库服务正在停止，请核对状态后再启动。")
        with self._lifecycle_lock:
            with self._lock:
                stop_epoch = self._stop_epoch
                if self._shutdown_in_progress.is_set():
                    raise WorkerBusyError("入库服务正在停止，请核对状态后再启动。")
                if self._running:
                    return
                if self._thread.is_alive():
                    raise WorkerBusyError("当前入库任务尚未退出，请等待其结束。")
                if self._state_recording_errors or self._shutdown_state_unconfirmed:
                    raise WorkerBusyError("入库任务记录尚未确认，请保留现场并重新核对服务状态。")
            prior_owner = self._owner_lock
            try:
                self._get_ledger(create=True, owner=True)
                with self._lock:
                    if stop_epoch != self._stop_epoch or self._shutdown_in_progress.is_set():
                        raise WorkerBusyError("入库服务正在停止，请核对状态后再启动。")
                    if self._thread.ident is not None:
                        self._thread = self._new_thread()
                    self._stop_requested.clear()
                    self._run_epoch += 1
                    self._running, self._accepting = True, True
                    self._thread.start()
            except BaseException:
                # A startup cancelled after acquisition has no thread to run
                # its normal finally. Return only the owner acquired here.
                if prior_owner is None and self._owner_lock is not None and not self._thread.is_alive():
                    self._release_owner(expected_thread=self._thread, expected_epoch=self._run_epoch,
                        expected_owner=self._owner_lock, expected_token=self._owner_token)
                raise
        logger.info("后台工作线程已启动")

    def _cache(self, task_id, snapshot, *, deadline=None):
        with _bounded_lock(self._lock, deadline):
            self._tasks[task_id] = deepcopy(snapshot)
            if snapshot["status"] in {"done", "failed", "interrupted", "needs_review"}:
                self._state_recording_errors.discard(task_id)

    def _persistence_failure(self, task_id, *, deadline=None):
        self._shutdown_state_unconfirmed |= deadline is not None
        deadline_failed = False
        try:
            with _bounded_lock(self._lock, deadline):
                self._state_recording_errors.add(task_id)
                task = self._tasks.get(task_id)
                if task is not None:
                    task.update(persistence_error=True, needs_review=True,
                                message="任务记录暂未确认，请核对原资料与调用结果。")
        except TimeoutError:
            self._shutdown_state_unconfirmed = True
            deadline_failed = True
        if deadline is None:
            logger.error("入库任务状态记录未确认: %s", task_id)
        return deadline_failed

    def _finish(self, task_id, status, *, reason_code=None, deadline=None):
        with _bounded_lock(self._transition_lock, deadline):
            ledger = self._get_ledger() if deadline is None else self._ledger
            if ledger is not None:
                if deadline is not None and self._owner_lock is None:
                    raise WorkerBusyError("任务所有权尚未确认，请重新启动后核对原记录。")
                options = {"deadline": deadline} if deadline is not None else {}
                snapshot = ledger.finish(task_id, self._owner_token, status, reason_code=reason_code, **options)
            else:
                with _bounded_lock(self._lock, deadline):
                    snapshot = deepcopy(self._tasks[task_id])
                now = self._clock()
                snapshot.update(status=status, reason_code=reason_code, updated_at=now, finished_at=now,
                    revision=snapshot["revision"] + 1, needs_review=status in {"interrupted", "needs_review"})
                snapshot["message"] = {"done": "处理完成", "failed": "处理失败，请核对任务记录。",
                    "interrupted": "处理已中断，请核对原资料与调用结果。", "needs_review": "任务未继续执行，请明确重新提交。"}[status]
                if status == "done":
                    snapshot.update(progress=snapshot["total"], stage="finished")
            self._cache(task_id, snapshot, deadline=deadline)

    def _quarantine_queued(self, *, deadline=None):
        with _bounded_lock(self._transition_lock, deadline):
            with _bounded_lock(self._lock, deadline):
                queued = [key for key, item in self._tasks.items() if item["status"] == "queued"]
            for key in queued:
                try:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("worker_shutdown_deadline_exhausted")
                    self._finish(key, "needs_review", reason_code="worker_stopped_before_start", deadline=deadline)
                except Exception:
                    failed = self._persistence_failure(key, deadline=deadline)
                    if deadline is not None:
                        if failed:
                            raise TimeoutError("worker_shutdown_deadline_exhausted") from None
                        raise
            return len(queued)

    def _discard_pending_entries(self, *, deadline=None):
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return True
            self._queue.task_done()

    def stop(self, timeout=30.0):
        """Stop new claims; a bounded wait/timeout never cancels a paid call.

        The total wait budget includes ledger/file/mutex contention. ``stopped``
        reports only the thread; unconfirmed status writes have state_recorded
        false, even when that thread has exited. A zero budget cannot promise a
        durable shutdown marker. Native filesystem calls are not hard realtime.
        A running callback retains its OS owner until the thread actually exits.
        """
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 <= timeout <= 3600:
            raise ValueError("invalid_worker_stop_timeout")
        deadline = time.monotonic() + timeout
        # Independent fence precedes every potentially contended lock/SQLite
        # wait. A dequeued or ledger-started job still rechecks before callback.
        self._shutdown_in_progress.set()
        self._stop_requested.set()
        self._running, self._accepting = False, False
        ticket, registered, result = object(), False, None
        try:
            with _bounded_lock(self._stop_state_lock, deadline):
                self._pending_stops.add(ticket)
                registered = True
                self._stop_epoch += 1
                self._shutdown_in_progress.set()
            result = self._stop_until(timeout, deadline, self._thread, self._run_epoch,
                                      self._owner_lock, self._owner_token)
        except Exception:
            self._shutdown_state_unconfirmed = True
            result = {'stopped': not self._thread.is_alive(), 'timed_out': True,
                      'current_task_id': self._current_task_id, 'pending_count': 0,
                      'pending_count_known': False, 'state_recorded': False}
        finally:
            if registered:
                try:
                    with _bounded_lock(self._stop_state_lock, deadline):
                        self._pending_stops.discard(ticket)
                        if not self._pending_stops:
                            self._shutdown_in_progress.clear()
                except Exception:
                    self._shutdown_state_unconfirmed = True
                    if result is not None:
                        result.update(state_recorded=False, timed_out=True)
        return result

    def _stop_until(self, timeout, deadline, thread, run_epoch, owner, owner_token):
        pending, current, incomplete, pending_known = 0, self._current_task_id, False, False
        deadline_failed = False
        try:
            with _bounded_lock(self._lock, deadline):
                current = self._current_task_id
                pending = sum(item["status"] == "queued" for item in self._tasks.values())
                pending_known = True
            self._quarantine_queued(deadline=deadline)
            if not self._discard_pending_entries(deadline=deadline) and pending:
                incomplete = True
        except Exception as error:
            incomplete = True
            deadline_failed |= _deadline_wait_failed(error)
        ledger = self._ledger  # Stop never initializes a ledger or waits on lifecycle lookup.
        if current and ledger is not None:
            try:
                ledger.shutdown(current, self._owner_token, deadline=deadline)
            except Exception as error:
                deadline_failed |= _deadline_wait_failed(error)
                deadline_failed |= self._persistence_failure(current, deadline=deadline)
                incomplete = True
        if thread.is_alive() and thread is not threading.current_thread():
            thread.join(max(0, deadline - time.monotonic()))
        stopped = not thread.is_alive()
        current = self._current_task_id
        if not stopped and current:
            if ledger is not None:
                try:
                    ledger.shutdown(current, self._owner_token, timed_out=True, deadline=deadline)
                except Exception as error:
                    deadline_failed |= _deadline_wait_failed(error)
                    deadline_failed |= self._persistence_failure(current, deadline=deadline)
                    incomplete = True
            try:
                with _bounded_lock(self._lock, deadline):
                    if current in self._tasks:
                        self._tasks[current].update(shutdown_timed_out=True, needs_review=True)
            except TimeoutError:
                incomplete = True
                deadline_failed = True
        if stopped:
            try:
                if self._release_owner(deadline=deadline, expected_thread=thread,
                        expected_epoch=run_epoch, expected_owner=owner, expected_token=owner_token) is False:
                    incomplete = True
            except Exception as error:
                incomplete = True
                deadline_failed |= _deadline_wait_failed(error)
        result = {"stopped": stopped, "timed_out": (not stopped or deadline_failed
                  or incomplete and time.monotonic() >= deadline
                  or timeout > 0 and time.monotonic() > deadline),
                  "current_task_id": current, "pending_count": pending}
        if not pending_known:
            result['pending_count_known'] = False
        try:
            with _bounded_lock(self._lock, deadline):
                if self._state_recording_errors:
                    incomplete = True
                self._shutdown_state_unconfirmed = incomplete
        except TimeoutError:
            incomplete = True
            result['timed_out'] = True
            self._shutdown_state_unconfirmed = True
        if incomplete:
            result["state_recorded"] = False
            result["timed_out"] |= time.monotonic() >= deadline
        return result

    def submit(self, task_id: str, func: Callable, *args, job_metadata=None, job_kind: TaskKind | None = None, **kwargs):
        admission_epoch = self._stop_epoch
        task_identity(task_id)
        if not callable(func):
            raise ValueError("invalid_worker_callable")
        metadata = checked_metadata(job_metadata)
        kind = checked_kind(job_kind, metadata)
        with self.submission_guard(), self._transition_lock:
            with self._lock:
                if not self._admission_current(admission_epoch):
                    raise WorkerBusyError("入库服务正在停止，请核对状态后再提交。")
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
                    "usage_state": "not_recorded", "current_item_id": None, "revision": 1, "kind": kind}
            ledger = None
            prior_owner, prior_thread, prior_epoch = self._owner_lock, self._thread, self._run_epoch
            try:
                ledger = self._get_ledger(create=True, owner=True, admission_epoch=admission_epoch)
                if not self._admission_current(admission_epoch):
                    raise WorkerBusyError("入库服务正在停止，请核对状态后再提交。")
                if ledger is not None:
                    self._cache(task_id, ledger.register(task_id, self._owner_token, metadata, kind=kind))
                if not self._admission_current(admission_epoch):
                    raise WorkerBusyError("入库服务正在停止，请核对状态后再提交。")
                self._queue.put_nowait((task_id, func, args, kwargs))
            except BaseException:
                with self._lock:
                    self._tasks.pop(task_id, None)
                if ledger is not None:
                    try:
                        ledger.publication_unconfirmed(task_id, self._owner_token)
                    except Exception:
                        self._persistence_failure(task_id)
                if prior_owner is None and self._owner_lock is not None and not self._thread.is_alive():
                    self._release_owner(expected_thread=prior_thread, expected_epoch=prior_epoch,
                        expected_owner=self._owner_lock, expected_token=self._owner_token)
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
                if unconfirmed and value['status'] in {'queued', 'running'}:
                    value.update(persistence_error=True, needs_review=True,
                        message="任务记录暂未确认，请核对原资料与调用结果。")
                return value
        return cached

    def list_tasks(self, limit=50, offset=0, *, kind: TaskKind | None = None):
        if kind is not None:
            checked_kind(kind)
        ledger = self._get_ledger()
        if ledger is not None:
            records = ledger.list(limit=limit, offset=offset, **({"kind": kind} if kind is not None else {}))
            with self._lock:
                uncertain = set(self._state_recording_errors)
            for record in records:
                if record["task_id"] in uncertain and record['status'] in {'queued', 'running'}:
                    record.update(persistence_error=True, needs_review=True,
                        message="任务记录暂未确认，请核对原资料与调用结果。")
            return records
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise ValueError("invalid_ingestion_job_page")
        with self._lock:
            values = sorted((item for item in self._tasks.values() if kind is None or record_kind(item) == kind),
                            key=lambda item: (item["created_at"], item["task_id"]), reverse=True)
            return deepcopy(values[offset:offset + limit])

    def get_current_task(self) -> Optional[str]:
        with self._lock:
            return self._current_task_id

    def is_stopping(self) -> bool:
        """Read-only paid-step fence; never clears a stop or resolves storage."""
        return self._stop_requested.is_set() or not self._accepting

    def count_tasks(self, *, kind: TaskKind | None = None) -> int:
        if kind is not None:
            checked_kind(kind)
        ledger = self._get_ledger()
        if ledger is not None:
            return ledger.count(**({"kind": kind} if kind is not None else {}))
        with self._lock:
            return sum(kind is None or record_kind(item) == kind for item in self._tasks.values())

    def active_video_tasks(self):
        """Return declared local video reservations, without inferring old scope."""
        with self._lock:
            return {video: task_id for task_id, item in self._tasks.items()
                    if item["status"] in {"queued", "running"}
                    for video in item.get("metadata", {}).get("platform_item_ids", [])}

    def activity_snapshot(self):
        """All kinds share maintenance exclusion; unknown is never idle."""
        with self._lock:
            current = self._current_task_id
            record = self._tasks.get(current) if current is not None else None
            known = not (self._state_recording_errors or self._shutdown_state_unconfirmed
                         or current is not None and record is None)
            kind = record_kind(record) if record is not None else None
            busy = (not known or self._maintenance_active or self._submission_reservations
                    or current is not None
                    or any(item['status'] in {'queued', 'running'} for item in self._tasks.values()))
            return {'active_task_id': current, 'active_task_kind': kind,
                    'maintenance_busy': bool(busy), 'activity_known': bool(known)}

    def _run(self):
        try:
            while self._running and not self._stop_requested.is_set():
                try:
                    task_id, func, args, kwargs = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                claimed = False
                try:
                    with self._transition_lock:
                        with self._lock:
                            task = self._tasks.get(task_id)
                            valid = self._running and not self._stop_requested.is_set() and task is not None and task["status"] == "queued"
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
                        if self._stop_requested.is_set():
                            self._finish(task_id, "needs_review", reason_code="worker_stopped_before_callback")
                            continue
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
            items = task.get('metadata', {}).get('platform_item_ids')
            if (items is not None and total != len(items)
                    or item_id is not None and (type(item_id) is not str or items is not None and item_id not in items)):
                raise IngestionJobError('ingestion_progress_scope_changed')
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
