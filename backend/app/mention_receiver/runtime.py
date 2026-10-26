"""Small, credential-free lifecycle records for the receive-only process.

The caller owns the receiver lock and performs its normal preflight before
creating a RunHistory. Stop requests deliberately do not acquire that lock.
Only enumerated states and numeric measurements enter these records; public
notification content belongs in the separate preview inbox.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
from collections.abc import Mapping
from uuid import uuid4


RUNTIME_FILES = (
    "active-run.json", "stop-request.json", "cycle-history.jsonl", "run-history.jsonl",
)
OUTCOMES = frozenset({"success", "timeout", "paused", "error"})
STAGES = frozenset({"startup", "identity", "notifications", "details", "cycle", "wait", "shutdown"})
OPEN_METHODS = frozenset({"none", "hover", "click", "reload"})
FINISH_REASONS = frozenset({
    "completed", "stopped", "duration_elapsed", "window_closed", "error", "interrupted",
})
_VISIBILITY_STATES = frozenset({"visible", "hidden", "prerender"})
_UI_BOOL_KEYS = frozenset({"focused", "panel_visible", "entry_hit", "navigation"})
_UI_KEYS = _UI_BOOL_KEYS | {"visibility_state", "page_count"}
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_PUBLIC_NUMBER = re.compile(r"[0-9]{5,30}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _root(path: Path) -> Path:
    """Reject redirects, including an existing symlink/junction ancestor."""
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("运行记录目录存在重定向")
    if path.exists() and not path.is_dir():
        raise ValueError("运行记录目录类型异常")
    return path


def _safe_file(path: Path) -> None:
    """Check lstat rather than following a link, including broken symlinks."""
    if path.parent.resolve() != path.parent:
        raise ValueError("运行记录文件的父目录存在重定向")
    try:
        item = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        raise ValueError("运行记录文件类型异常或存在链接")
    if path.resolve() != path:
        raise ValueError("运行记录文件存在重定向")


def _check_files(root: Path) -> None:
    _root(root)
    for filename in RUNTIME_FILES:
        _safe_file(root / filename)


def _opened_file(path: Path, flags: int) -> int:
    _safe_file(path)
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("运行记录文件在打开时发生改变或存在链接")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _atomic(path: Path, value: dict[str, Any]) -> None:
    """Use an exclusive random temporary file; never truncate an old record."""
    _safe_file(path)
    temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
    descriptor = _opened_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        _safe_file(path)
        _safe_file(temporary)
        os.replace(temporary, path)
    finally:
        # Only remove this invocation's exclusive temporary, never prior logs.
        if temporary.exists():
            _safe_file(temporary)
            temporary.unlink()


def _append(path: Path, value: dict[str, Any]) -> None:
    descriptor = _opened_file(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(_encoded(value))
        stream.flush()
        os.fsync(stream.fileno())


def _read(path: Path) -> dict[str, Any] | None:
    try:
        descriptor = _opened_file(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        # Corrupt or externally supplied files cannot cause unbounded reads.
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("运行记录大小异常")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _choice(value: str, choices: frozenset[str], name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(name + " 必须使用固定状态")
    return value


def _count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(name + " 必须是非负整数")
    return value


def _duration(value: float) -> float:
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("duration_ms 必须是有限非负数字")
    return round(value, 3)


def _ui_state(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(key not in _UI_KEYS for key in value):
        raise ValueError("ui_state 仅接受固定的界面状态字段")
    result = dict(value)
    for key, item in result.items():
        if key in _UI_BOOL_KEYS and item is not None and not isinstance(item, bool):
            raise ValueError(key + " 必须是布尔值或 None")
        if key == "visibility_state":
            _choice(item, _VISIBILITY_STATES, key)
        if key == "page_count":
            _count(item, key)
    return result


class RunHistory:
    """One process run. Construction happens inside the caller's receiver lock."""

    def __init__(self, root: Path, binding_public_number: str):
        if not isinstance(binding_public_number, str) or not _PUBLIC_NUMBER.fullmatch(binding_public_number):
            raise ValueError("助手绑定号必须是公开抖音号")
        self.root = _root(root)
        _check_files(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid4().hex
        self.pid = os.getpid()
        self.started_at = _now()
        self.binding_public_number = binding_public_number
        self._sequence = 0
        self._counts = {outcome: 0 for outcome in OUTCOMES}
        self._consecutive_success = 0
        self._consecutive_failure = 0
        self._last_success_at: str | None = None
        self._closed = False
        self._final: dict[str, Any] | None = None
        active = self._active()
        _append(self.root / "run-history.jsonl", {**active, "event": "started"})
        _atomic(self.root / "active-run.json", active)

    def _active(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "pid": self.pid, "started_at": self.started_at,
            "binding_public_number": self.binding_public_number,
            "status": "closed" if self._closed else "running", "summary": self.summary(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "cycle_count": self._sequence,
            **{outcome + "_count": self._counts[outcome] for outcome in sorted(OUTCOMES)},
            "consecutive_success": self._consecutive_success,
            "consecutive_failure": self._consecutive_failure,
            "last_success_at": self._last_success_at,
        }

    def record_cycle(self, outcome: str, stage: str, duration_ms: float, *,
                     notice_count: int = 0, new_event_count: int = 0,
                     open_method: str = "none", recovery_count: int = 0,
                     detail_retry_count: int = 0, open_attempts: int = 0,
                     ui_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("已结束的运行不能继续追加轮次")
        record = {
            "run_id": self.run_id, "seq": self._sequence + 1, "recorded_at": _now(),
            "outcome": _choice(outcome, OUTCOMES, "outcome"),
            "stage": _choice(stage, STAGES, "stage"), "duration_ms": _duration(duration_ms),
            "notice_count": _count(notice_count, "notice_count"),
            "new_event_count": _count(new_event_count, "new_event_count"),
            "open_method": _choice(open_method, OPEN_METHODS, "open_method"),
            "recovery_count": _count(recovery_count, "recovery_count"),
            "detail_retry_count": _count(detail_retry_count, "detail_retry_count"),
            "open_attempts": _count(open_attempts, "open_attempts"),
            "ui_state": _ui_state(ui_state),
        }
        _check_files(self.root)
        _append(self.root / "cycle-history.jsonl", record)
        self._sequence += 1
        self._counts[outcome] += 1
        self._consecutive_success = self._consecutive_success + 1 if outcome == "success" else 0
        self._consecutive_failure = self._consecutive_failure + 1 if outcome in {"timeout", "error"} else 0
        if outcome == "success":
            self._last_success_at = record["recorded_at"]
        _atomic(self.root / "active-run.json", self._active())
        return record.copy()

    def stop_requested(self) -> bool:
        request = _read(self.root / "stop-request.json")
        return bool(request and request.get("target_run_id") == self.run_id)

    def finish(self, reason: str = "completed") -> dict[str, Any]:
        _choice(reason, FINISH_REASONS, "reason")
        if self._closed:
            return dict(self._final or {})
        _check_files(self.root)
        self._closed = True
        self._final = {**self._active(), "closed_at": _now(), "reason": reason}
        _append(self.root / "run-history.jsonl", {**self._final, "event": "closed"})
        _atomic(self.root / "active-run.json", self._final)
        return self._final.copy()

    def __enter__(self) -> RunHistory:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        reason = "interrupted" if exc_type is not None and issubclass(exc_type, KeyboardInterrupt) else "error" if exc_type else "completed"
        self.finish(reason)


def request_stop(root: Path) -> bool:
    """Target the currently recorded run without blocking its receiver lock."""
    root = _root(root)
    active = _read(root / "active-run.json")
    if not active or active.get("status") != "running":
        return False
    run_id = active.get("run_id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return False
    _atomic(root / "stop-request.json", {"target_run_id": run_id, "requested_at": _now()})
    return True
