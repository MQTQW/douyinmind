"""Control only the registered product subprocess, using its stop request file.

No terminate/kill, old backend PID discovery, shell command or arbitrary HTTP
launch parameter exists here. A pending/unknown launch is never duplicated.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from .queue import QueueError, code, encoded, utc
from .background_mode import background_receiver_message, background_finished_message
from .native_lifecycle import project_native_session_status


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return kernel.GetLastError() == 5  # Permission denied: fail closed.
        try:
            exit_code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class AssistantControl:
    def __init__(self, queue, state_dir=None, *, command_factory=None, popen_factory=None, process_probe=None):
        self.queue = queue
        self.state_dir = Path(state_dir or queue.path.parent).absolute()
        if self.state_dir.resolve() != self.state_dir or self.state_dir != queue.path.parent:
            raise QueueError("invalid_control_directory")
        self.backend = Path(__file__).resolve().parents[2]
        self.command_factory = command_factory or (lambda: [sys.executable, "-m", "app.assistant", "--state-dir", str(self.state_dir), "--run-registered"])
        self.popen_factory = popen_factory or subprocess.Popen
        self.process_probe = process_probe or process_alive

    def snapshot(self):
        value = self.queue.get_control()
        value["process_alive"] = self.process_probe(value.get("pid")) if value.get("pid") else False
        if value.get("pid") and not value["process_alive"] and value["state"] not in {"stopped", "start_failed"}:
            # Observation only. Starting a replacement performs the durable
            # restart fence before claiming any queued work.
            value["state"] = "interrupted"
            value["reason_code"] = "registered_process_exited"
        return value

    def start(self):
        reserved = {"spawn": False}
        def change(current):
            pid = current.get("pid")
            if pid and self.process_probe(pid):
                if (current.get('manual_verification') or {}).get('active') is True:
                    raise QueueError('manual_verification_active')
                if current.get("desired_state") == "stopped" or current.get("state") == "stopping":
                    raise QueueError("runtime_stopping")
                current.update(desired_state="running", reason_code=None)
                return current
            if current.get("state") in {"starting", "launch_unknown"} and not pid:
                raise QueueError("launch_pending_requires_review")
            run_id = secrets.token_hex(16)
            reserved.update(spawn=True, run_id=run_id)
            return {"state": "starting", "desired_state": "running", "run_id": run_id, "pid": None,
                    "started_at": utc(), "heartbeat_at": None, "reason_code": None,
                    "capture_cycles": 0, "last_capture_at": None, "last_capture_admitted_count": None,
                    "last_capture_baseline_created": None,
                    "receiver_state": "starting", "message": "后台助手正在启动，抖音会话尚未就绪。"}
        value = self.queue.update_control(change)
        if not reserved["spawn"]:
            return value
        self.queue.recover_interrupted(current_run_id=reserved["run_id"])
        command = self.command_factory()
        if (not isinstance(command, (list, tuple)) or not command or any(not isinstance(item, str) or not item for item in command)
                or Path(command[0]).resolve() != Path(sys.executable).resolve()):
            self._launch_failed(reserved["run_id"], "invalid_launch_command")
            raise QueueError("invalid_launch_command")
        try:
            process = self.popen_factory(list(command), cwd=str(self.backend), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            pid = process.pid
            if type(pid) is not int or pid <= 0:
                raise ValueError("invalid product process id")
        except Exception:
            self._launch_failed(reserved["run_id"], "launch_result_unknown")
            raise QueueError("launch_result_unknown") from None
        def record(current):
            if current.get("run_id") != reserved["run_id"]:
                raise QueueError("runtime_owner_changed")
            # The child can register its actual Python PID before its launcher
            # returns. Never overwrite that registration with a wrapper PID.
            if current.get("state") == "starting":
                current["pid"] = pid
                current["launcher_pid"] = pid
            return current
        return self.queue.update_control(record)

    def _launch_failed(self, run_id, reason):
        def change(current):
            if current.get("run_id") == run_id:
                current.update(state="launch_unknown", reason_code=reason)
            return current
        self.queue.update_control(change)

    def register(self, run_id, pid=None):
        from .runtime_diagnostics import capture_build
        build = capture_build('helper')
        pid = os.getpid() if pid is None else pid
        if type(pid) is not int or pid <= 0:
            raise QueueError("invalid_runtime_pid")
        def change(current):
            if current.get("run_id") != run_id or current.get("state") not in {"starting", "running", "paused", "pausing", "stopping"}:
                raise QueueError("runtime_not_registered")
            current.update(pid=pid, state="running" if current["desired_state"] == "running" else "paused" if current["desired_state"] == "paused" else "stopping",
                           heartbeat_at=utc(), runtime_build=build)
            return current
        return self.queue.update_control(change)

    def set_receiver_health(self, run_id, receiver_state, reason_code=None):
        try:
            message = background_receiver_message(receiver_state)
        except ValueError:
            raise QueueError("invalid_receiver_state") from None
        if reason_code is not None:
            code(reason_code)
        def change(current):
            if current.get("run_id") != run_id:
                raise QueueError("runtime_owner_changed")
            current.update(receiver_state=receiver_state, message=message)
            if reason_code is not None:
                current["reason_code"] = reason_code
            elif receiver_state == "ready":
                current["reason_code"] = None
            return current
        return self.queue.update_control(change)

    def set_worker_health(self, run_id, worker_state, reason_code=None):
        messages = {"idle": "处理线程正在等待下一项任务。", "processing": "处理线程正在处理或核对任务。",
                    "error": "处理线程遇到问题，记录已保留，稍后会再次核对。"}
        if worker_state not in messages:
            raise QueueError("invalid_worker_state")
        if reason_code is not None:
            code(reason_code)
        def change(current):
            if current.get("run_id") != run_id:
                raise QueueError("runtime_owner_changed")
            current.update(worker_state=worker_state, worker_message=messages[worker_state], worker_updated_at=utc())
            current["worker_reason"] = reason_code
            if worker_state == "error":
                current["worker_error_count"] = current.get("worker_error_count", 0) + 1
                current["worker_last_error_at"] = utc()
            return current
        return self.queue.update_control(change)

    def record_capture(self, run_id, *, admitted_count, baseline_created, unavailable_details=0):
        """Record a fully successful notice capture, not an API read/heartbeat."""
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise QueueError("invalid_run_id")
        if type(admitted_count) is not int or not 0 <= admitted_count <= 3 or type(baseline_created) is not bool:
            raise QueueError("invalid_capture_observation")
        if type(unavailable_details) is not int or not 0 <= unavailable_details <= 3:
            raise QueueError('invalid_capture_observation')
        def change(current):
            if current.get("run_id") != run_id:
                raise QueueError("runtime_owner_changed")
            previous = current.get("capture_cycles", 0)
            if type(previous) is not int or previous < 0:
                raise QueueError("invalid_capture_counters")
            current.update(capture_cycles=previous + 1, last_capture_at=utc(),
                last_capture_admitted_count=admitted_count, last_capture_baseline_created=baseline_created,
                last_capture_unavailable_details=unavailable_details)
            return current
        return self.queue.update_control(change)

    def set_native_intake(self, run_id, summary):
        from .native_intake_visibility import project_native_intake
        try:
            observation = project_native_intake(summary)
        except Exception:
            raise QueueError('invalid_native_intake_snapshot') from None
        def change(current):
            if current.get('run_id') != run_id:
                raise QueueError('runtime_owner_changed')
            current['native_intake'] = observation
            return current
        return self.queue.update_control(change)

    def heartbeat(self, run_id, *, native_session_status=None):
        observation = None
        if native_session_status is not None:
            try:
                observation = project_native_session_status(native_session_status)
            except Exception:
                raise QueueError('invalid_native_session_status') from None
        def change(current):
            if current.get("run_id") != run_id:
                raise QueueError("runtime_owner_changed")
            current.update(heartbeat_at=utc(), state={"running": "running", "paused": "paused", "stopped": "stopping"}[current["desired_state"]])
            if observation is not None:
                current['native_session_status'] = observation
                current['native_session_status_observed_at'] = utc()
            return current
        return self.queue.update_control(change)

    def pause(self):
        def change(current):
            if current.get("state") == "stopping":
                raise QueueError("runtime_stopping")
            current["desired_state"] = "paused"
            if current.get("pid") and self.process_probe(current["pid"]):
                current["state"] = "pausing"
            return current
        return self.queue.update_control(change)

    def stop_path(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise QueueError("invalid_run_id")
        path = self.state_dir / "runs" / run_id / "stop.json"
        if path.resolve() != path or path.parent.resolve() != path.parent:
            raise QueueError("redirected_stop_path")
        return path

    def stop(self):
        def change(current):
            current["desired_state"] = "stopped"
            current["state"] = "stopping" if current.get("pid") and self.process_probe(current["pid"]) else "stopped"
            current["message"] = ("后台助手正在停止，任务记录已保留。" if current["state"] == "stopping"
                                  else "后台助手已停止，任务记录已保留。")
            return current
        result = self.queue.update_control(change)
        if result.get("run_id"):
            path = self.stop_path(result["run_id"])
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(encoded({"run_id": result["run_id"], "requested_at": utc(), "action": "stop"}) + "\n")
            except FileExistsError:
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise QueueError("unsafe_stop_file")
        return result

    def should_stop(self, run_id):
        value = self.queue.get_control()
        return (value.get("run_id") != run_id or value.get("desired_state") == "stopped" or self.stop_path(run_id).exists())

    def finish(self, run_id, reason_code="normal_stop"):
        code(reason_code)
        def change(current):
            if current.get("run_id") != run_id:
                raise QueueError("runtime_owner_changed")
            current.update(state="stopped", desired_state="stopped", pid=None, heartbeat_at=utc(), reason_code=reason_code,
                           receiver_state="page_unavailable", message=background_finished_message(reason_code))
            return current
        return self.queue.update_control(change)
