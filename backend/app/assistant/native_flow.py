"""Web-triggered enable/disable of the opt-in native (API) intake channel.

The provisioning itself runs in a separate short-lived process (it needs the
exclusive B browser profile). This flow stops the headless assistant, runs the
fixed provisioning command, reads its fixed result file, then starts the normal
headless assistant again. It never publishes, imports or resumes any task.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

BACKEND = Path(__file__).resolve().parents[2]
STORAGE = BACKEND / "app" / "storage"
QUEUE_STATE = STORAGE / "assistant"
RESULT_NAME = "native-provision-result.json"

STOP_TIMEOUT_SECONDS = 45
PROVISION_TIMEOUT_SECONDS = 300


class NativeIntakeFlow:
    """One bounded enable/disable run, serialized by a single background thread."""

    def __init__(self, control, *, backend=None, result_path=None,
                 python_executable=None, spawn=None):
        self.control = control
        self.backend = Path(backend or BACKEND)
        self.result_path = Path(result_path or (QUEUE_STATE / RESULT_NAME))
        self.python = python_executable or sys.executable
        self.spawn = spawn or subprocess.Popen
        self._lock = threading.Lock()
        self._thread = None
        self._stage = "idle"
        self._message = None
        self._last_result = None

    def status(self) -> dict:
        with self._lock:
            return {"active": bool(self._thread and self._thread.is_alive()),
                    "stage": self._stage, "message": self._message,
                    "last_result": self._last_result}

    def start_enable(self) -> bool:
        return self._start("enabling", "正在检查 B 会话并启用接口收件；助手会短暂重启。", "enable")

    def start_disable(self) -> bool:
        return self._start("disabling", "正在关闭接口收件并恢复网页收件；助手会短暂重启。", "disable")

    def _start(self, stage, message, action) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._stage, self._message, self._last_result = stage, message, None
            self._thread = threading.Thread(target=self.run_once, args=(action,),
                                            name="DouyinMindNativeIntakeFlow", daemon=True)
            self._thread.start()
            return True

    def _set(self, stage, message, result=None):
        with self._lock:
            self._stage, self._message = stage, message
            if result is not None:
                self._last_result = result

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

    def _read_result(self):
        try:
            return json.loads(self.result_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def run_once(self, action) -> str:
        """Synchronous flow used by the background thread and by tests."""
        try:
            stopping = "正在停止助手，准备" + ("启用" if action == "enable" else "关闭") + "接口收件。"
            self._set("stopping_assistant", stopping)
            try:
                self.control.stop()
            except Exception:
                pass
            if not self._wait_stopped():
                self._set("stop_timeout", "助手未能停止；请先在页面上点「停止」后重试。")
                return "stop_timeout"
            try:
                self.result_path.parent.mkdir(parents=True, exist_ok=True)
                self.result_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self._set("provisioning", "正在核对当前 B 会话并写入本机接口配置（不会读取通知内容）。")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            command = [self.python, "-m", "app.assistant.native_provision",
                       "--action", action, "--result-file", str(self.result_path)]
            try:
                process = self.spawn(command, cwd=str(self.backend), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=flags)
            except Exception:
                self._set("provision_failed", "接口收件配置进程未能启动；原网页收件保持不变。")
                self._restart()
                return "provision_failed"
            try:
                process.wait(timeout=PROVISION_TIMEOUT_SECONDS)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
                self._set("provision_timeout", "接口收件配置超时；原网页收件保持不变。")
                self._restart()
                return "provision_timeout"
            result = self._read_result()
            if not isinstance(result, dict) or result.get("ok") is not True:
                code = result.get("code") if isinstance(result, dict) else "native_provision_failed"
                self._set("provision_failed", _failure_message(action, code), result)
                self._restart()
                return "provision_failed"
            self._restart()
            if action == "enable":
                self._set("enabled", "接口收件已启用，助手已请求重启；请以页面运行状态为准。", result)
                return "enabled"
            self._set("disabled", "接口收件已关闭，网页收件已恢复；请以页面运行状态为准。", result)
            return "disabled"
        except Exception as error:  # noqa: BLE001 - fixed public stage only
            self._set("failed", "接口收件操作未完成（" + type(error).__name__ + "）。")
            return "failed"

    def _restart(self):
        try:
            self.control.start()
        except Exception:
            self._set("start_failed", "配置已写入，但助手未能自动开启；请回页面点「开启助手」。")


def _failure_message(action, code):
    """Product wording for fixed provisioning codes; never raw platform data."""
    messages = {
        "native_provision_receiver_configuration_required":
            "尚未完成 B 账号的首次配置；请先完成前面的设置步骤。",
        "native_provision_profile_unverified":
            "当前浏览器登录的不是已配置的 B 账号；请先点「打开登录窗口」重新登录 B。",
        "native_provision_verification_required":
            "当前 B 会话需要本人在抖音完成安全验证；请先点「打开登录窗口」完成核验。",
        "native_provision_session_invalid":
            "当前 B 登录已失效；请先点「打开登录窗口」重新登录。",
        "native_provision_session_unavailable":
            "未能读取 B 会话（可能配置目录被占用或会话不完整）；原网页收件保持不变。",
        "native_provision_identity_changed":
            "当前登录账号与已配置的 B 不一致；为避免串号，未覆盖原配置。",
        "native_provision_store_mismatch":
            "本机已有另一份接口收件记录，未自动覆盖；请核对原设置。",
        "native_provision_configuration_invalid":
            "本机接口收件配置无效，未覆盖原文件；请核对原设置。",
        "native_provision_network_unavailable":
            "暂时无法连接抖音接口，未启用接口收件；原网页收件保持不变。",
        "native_provision_api_error":
            "抖音接口返回未识别的失败状态，未启用接口收件；原网页收件保持不变。",
    }
    base = messages.get(code, "接口收件未启用；原网页收件保持不变。请稍后重试或查看本机记录。")
    return base
