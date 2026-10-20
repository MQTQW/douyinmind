"""
后台任务工作线程

使用独立线程 + 队列处理入库任务，避免阻塞 API 请求。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class WorkerBusyError(ValueError):
    """A local submission or maintenance action cannot safely start yet."""


class Worker:
    """
    后台任务工作器

    在独立线程中串行执行任务，支持：
    - 提交任务并获取 task_id
    - 查询任务进度和状态
    - 取消进行中的任务
    """

    def __init__(self) -> None:
        """
        初始化工作器

        启动后台线程监听任务队列。
        """
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread = threading.Thread(
            target=self._run, daemon=True, name="knowledge-worker"
        )
        self._current_task_id: Optional[str] = None
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._running = False
        self._submission_reservations = 0
        self._maintenance_active = False

    @contextmanager
    def submission_guard(self):
        """Reserve a submission before DB work; never hold the lock over SQL."""
        with self._lock:
            if self._maintenance_active:
                raise WorkerBusyError("正在恢复入库状态，请稍后重新提交。")
            self._submission_reservations += 1
        try:
            yield
        finally:
            with self._lock:
                self._submission_reservations -= 1

    @contextmanager
    def maintenance_guard(self):
        """Exclude local submissions/jobs while explicit recovery owns SQL.

        A dequeued job stays queued in the registry until _run assigns current
        under this same lock, so that scheduling window is not mistaken for idle.
        This coordinates one backend process, not other servers sharing its DB.
        """
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

    def start(self) -> None:
        """
        启动工作线程
        """
        if not self._running:
            self._running = True
            self._thread.start()
            logger.info("后台工作线程已启动")

    def stop(self) -> None:
        """
        停止工作线程
        """
        self._running = False

    def submit(
        self,
        task_id: str,
        func: Callable,
        *args,
        **kwargs,
    ) -> None:
        """
        提交任务到队列

        :param task_id: 任务唯一标识
        :param func: 要执行的可调用对象
        :param args: 位置参数
        :param kwargs: 关键字参数
        """
        with self._lock:
            if self._maintenance_active:
                raise WorkerBusyError("正在恢复入库状态，请稍后重新提交。")
            if task_id in self._tasks:
                raise ValueError("任务编号已存在，请重新提交。")
            self._tasks[task_id] = {
                "status": "queued",
                "progress": 0,
                "total": 0,
                "message": "等待处理...",
            }
            try:
                # The unbounded queue publishes without waiting for a consumer.
                self._queue.put_nowait((task_id, func, args, kwargs))
            except BaseException:
                self._tasks.pop(task_id, None)
                raise
        logger.info("任务已提交: %s", task_id)

    def get_progress(self, task_id: str) -> Optional[dict]:
        """
        查询任务进度

        :param task_id: 任务 ID
        :return: 进度信息字典，不存在则返回 None
        """
        with self._lock:
            return self._tasks.get(task_id)

    def get_current_task(self) -> Optional[str]:
        """
        获取当前正在执行的任务 ID

        :return: 任务 ID 或 None
        """
        with self._lock:
            return self._current_task_id

    def _run(self) -> None:
        """
        后台线程主循环

        从队列取任务并执行，更新状态。
        """
        while self._running:
            try:
                task_id, func, args, kwargs = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            with self._lock:
                task = self._tasks.get(task_id)
                valid = task is not None and task["status"] == "queued"
                if valid:
                    self._current_task_id = task_id
                    task["status"] = "running"
                    task["message"] = "正在处理..."
            if not valid:
                # Defensively discard stale/unregistered entries. The supported
                # publisher is standard Queue; this is not a transactional
                # delivery guarantee for custom queue implementations.
                self._queue.task_done()
                continue

            try:
                # 执行任务
                func(*args, task_id=task_id, **kwargs)

                with self._lock:
                    self._tasks[task_id]["status"] = "done"
                    self._tasks[task_id]["message"] = "处理完成"
                    self._tasks[task_id]["progress"] = (
                        self._tasks[task_id]["total"]
                    )

            except Exception as exc:
                logger.exception("任务执行失败: %s", task_id)
                with self._lock:
                    self._tasks[task_id]["status"] = "failed"
                    self._tasks[task_id]["message"] = str(exc)[:500]

            finally:
                with self._lock:
                    self._current_task_id = None
                self._queue.task_done()

    def update_progress(
        self,
        task_id: str,
        progress: int,
        total: int,
        message: str = "",
    ) -> None:
        """
        由执行中的任务回调，更新进度

        :param task_id: 任务 ID
        :param progress: 当前进度
        :param total: 总数量
        :param message: 进度消息
        """
        with self._lock:
            if task_id in self._tasks:
                self._tasks[task_id]["progress"] = progress
                self._tasks[task_id]["total"] = total
                if message:
                    self._tasks[task_id]["message"] = message


# 全局单例
worker = Worker()
