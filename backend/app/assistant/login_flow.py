"""Web-triggered visible login, then an automatic headless assistant restart.

The web page detects that the assistant is paused waiting for the operator's
platform login. This flow stops the headless owner, opens the same local
browser profile in a visible window, waits for the operator to finish, and then
starts the normal headless assistant again. It never reads, stores or returns
credentials, and it never publishes or imports anything by itself.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

BACKEND = Path(__file__).resolve().parents[2]
STORAGE = BACKEND / "app" / "storage"
RECEIVER_STATE = STORAGE / "mention_receiver"
MARKER_NAME = "login-ready.json"

STOP_TIMEOUT_SECONDS = 45
LOGIN_WINDOW_TIMEOUT_SECONDS = 1800


class LoginFlow:
    """One bounded visible-login run, serialized by a single background thread."""

    def __init__(self, control, *, backend=None, receiver_state=None,
                 python_executable=None, spawn=None):
        self.control = control
        self.backend = Path(backend or BACKEND)
        self.receiver_state = Path(receiver_state or RECEIVER_STATE)
        self.python = python_executable or sys.executable
        self.spawn = spawn or subprocess.Popen
        self._lock = threading.Lock()
        self._thread = None
        self._stage = "idle"
        self._message = None

    def marker_path(self) -> Path:
        return self.receiver_state / MARKER_NAME

    def status(self) -> dict:
        with self._lock:
            return {"active": bool(self._thread and self._thread.is_alive()),
                    "stage": self._stage, "message": self._message}

    def start(self) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stage = "stopping_assistant"
            self._message = "正在停止助手，准备可见登录窗口。"
            self._thread = threading.Thread(target=self.run_once, name="DouyinMindLoginFlow",
                                            daemon=True)
            self._thread.start()
            return True

    def _set(self, stage, message):
        with self._lock:
            self._stage, self._message = stage, message

    def _wait_stopped(self, seconds=STOP_TIMEOUT_SECONDS) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                current = self.control.snapshot()
            except Exception:
                return False
            if (current.get("process_alive") is not True
                    and current.get("state") in {"stopped", "start_failed"}):
                return True
            time.sleep(1)
        return False

    def run_once(self) -> str:
        """Synchronous flow used by the background thread and by tests."""
        try:
            self._set("stopping_assistant", "正在停止助手，准备可见登录窗口。")
            try:
                self.control.stop()
            except Exception:
                pass
            if not self._wait_stopped():
                self._set("stop_timeout", "助手未能停止；请先在页面上点「停止」后重试。")
                return "stop_timeout"
            marker = self.marker_path()
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self._set("waiting_login",
                      "登录窗口已打开：请在 Chrome 中完成助手 B 的登录，完成后关闭窗口。")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            command = [self.python, "-m", "app.assistant.login_window", "--wait-close"]
            process = self.spawn(command, cwd=str(self.backend), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=flags)
            try:
                process.wait(timeout=LOGIN_WINDOW_TIMEOUT_SECONDS)
            except Exception:
                pass
            if not marker.is_file():
                self._set("login_not_finished",
                          "未确认登录成功；可重新点「打开登录窗口」再试。")
                return "login_not_finished"
            self._set("starting_assistant", "登录已完成，正在重新开启助手。")
            try:
                self.control.start()
            except Exception:
                self._set("start_failed", "登录已完成，但助手未能自动开启；请回页面点「开启助手」。")
                return "start_failed"
            self._set("started", "登录完成，助手已请求开启；请以页面运行状态为准。")
            return "started"
        except Exception as error:  # noqa: BLE001 - fixed public stage only
            self._set("failed", "登录流程未完成（" + type(error).__name__ + "）。")
            return "failed"
