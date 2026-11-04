"""Run local services under one foreground control window.

Double-click 启动项目.bat. Keep its window open; Ctrl+C stops only the children
created here. Existing healthy services are reused and never stopped.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


PROJECT_ROOT = Path(__file__).resolve().parent
LOG_ROOT = PROJECT_ROOT / "backend" / "app" / "storage" / "local_services"
URL = "http://127.0.0.1:5173/"


_IDENTITY_FIELDS = ("project_id", "backend_code_id", "dependency_id", "config_revision")
_FRONTEND_API_TARGET_ID = hashlib.sha256(b"http://127.0.0.1:8000").hexdigest()


def _valid_identity(value) -> bool:
    return isinstance(value, dict) and all(type(value.get(field)) is str
        and re.fullmatch(r"[0-9a-f]{64}", value[field]) for field in _IDENTITY_FIELDS)


def expected_backend_identity() -> dict:
    """Compute once per launcher in the actual selected backend environment."""
    backend = PROJECT_ROOT / "backend"
    python_exe = backend / ".venv" / "Scripts" / "python.exe"
    helper = backend / "app" / "core" / "runtime_identity.py"
    if not python_exe.is_file() or not helper.is_file():
        raise RuntimeError("无法核对本项目的运行环境身份，请先完成安装。")
    try:
        if Path(sys.executable).resolve() == python_exe.resolve():
            # Execute only this public helper without importing app settings,
            # writing bytecode or accidentally reusing another clone's module.
            namespace = {"__name__": "_douyinmind_launcher_identity", "__file__": str(helper)}
            previous_bytecode_policy = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                exec(compile(helper.read_text(encoding="utf-8"), str(helper), "exec"), namespace)
                value = namespace["capture_runtime_identity"](project_root=PROJECT_ROOT)
            finally:
                sys.dont_write_bytecode = previous_bytecode_policy
        else:
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run([str(python_exe), "-B", str(helper), "--project-root", str(PROJECT_ROOT)],
                cwd=backend, env=env, shell=False, capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            if completed.returncode != 0:
                raise ValueError()
            value = json.loads(completed.stdout)
        if not _valid_identity(value):
            raise ValueError()
        return {field: value[field] for field in _IDENTITY_FIELDS}
    except Exception:
        raise RuntimeError("运行环境身份核对未完成，请保留当前服务并检查本机安装。") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read_local(url: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=2) as response:
        if response.status != 200:
            raise ValueError("local_service_not_ready")
        return response.read(1_000_000).decode("utf-8")


def service_ready(name: str, *, expected_identity=None) -> bool:
    try:
        expected = expected_identity if expected_identity is not None else expected_backend_identity()
        if not _valid_identity(expected):
            return False
        if name == "backend":
            body = json.loads(_read_local("http://127.0.0.1:8000/"))
            return (isinstance(body, dict) and body.get("status") == "ok" and body.get("service") == "DouyinRAG"
                    and _valid_identity(body) and all(body[field] == expected[field] for field in _IDENTITY_FIELDS))
        if name != "frontend":
            return False
        body = json.loads(_read_local(URL + "__douyinmind_identity"))
        if (not isinstance(body, dict) or body.get("service") != "DouyinMindFrontend"
                or body.get("project_id") != expected["project_id"]
                or body.get("api_target_id") != _FRONTEND_API_TARGET_ID):
            return False
        content = _read_local(URL)
        return "<title>DouyinMind" in content and "/src/main.tsx" in content
    except (OSError, RuntimeError, ValueError, urllib.error.URLError):
        return False


def port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class WindowsChildJob:
    """Keep only our own subprocess tree tied to this foreground window."""

    def __init__(self) -> None:
        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = EXTENDED_LIMIT()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def add(self, child: subprocess.Popen) -> None:
        process_handle = self.kernel.OpenProcess(0x0101, False, child.pid)  # SET_QUOTA | TERMINATE
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process_handle)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class LocalSupervisor:
    def __init__(self, *, expected_identity=None) -> None:
        self.children: list[tuple[str, subprocess.Popen]] = []
        self.logs: list = []
        self.job: WindowsChildJob | None = None
        self.expected_identity = expected_identity

    def start(self, name: str, port: int, command: list[str], cwd: Path) -> None:
        if self.expected_identity is None:
            self.expected_identity = expected_backend_identity()
        if service_ready(name, expected_identity=self.expected_identity):
            print(f"[{name}] 已在运行，继续使用端口 {port}。", flush=True)
            return
        if port_in_use(port):
            raise RuntimeError(f"端口 {port} 已被占用，且不是就绪的本项目服务。请检查占用程序；不会关闭它。")
        if os.name == "nt" and self.job is None:
            self.job = WindowsChildJob()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log_path = LOG_ROOT / f"{name}-{stamp}.log"
        stream = log_path.open("ab", buffering=0)
        self.logs.append(stream)
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["DOUYINMIND_API_TARGET"] = "http://127.0.0.1:8000"
        child = subprocess.Popen(
            command, cwd=cwd, env=child_env, stdout=stream, stderr=subprocess.STDOUT,
            # Separate Ctrl+C delivery; the foreground parent performs cleanup.
            # No detached-process or hidden-window flags are used.
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.children.append((name, child))
        if self.job:
            self.job.add(child)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError(f"{name} 启动后退出（代码 {child.returncode}）。日志：{log_path}")
            if service_ready(name, expected_identity=self.expected_identity):
                print(f"[{name}] 已就绪，端口 {port}。", flush=True)
                return
            time.sleep(0.4)
        raise RuntimeError(f"{name} 等待就绪超时。日志：{log_path}")

    def stop(self) -> None:
        try:
            for name, child in reversed(self.children):
                if child.poll() is not None:
                    continue
                print(f"[{name}] 正在关闭本窗口启动的服务…", flush=True)
                # Backend lifespan allows its current ingestion call 30s to
                # finish. Do not kill it after the old five-second UI grace.
                grace_seconds = 40 if name == "backend" else 5
                try:
                    if os.name == "nt":
                        child.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        child.terminate()
                except OSError:
                    print(f"[{name}] 停止请求未确认，继续等待本窗口启动的服务退出。", flush=True)
                try:
                    child.wait(timeout=grace_seconds)
                except (OSError, subprocess.TimeoutExpired):
                    print(f"[{name}] 退出结果未确认，将结束本窗口创建的进程；入库处理结果仍需核对原记录。", flush=True)
                    try:
                        child.kill()
                        child.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        print(f"[{name}] 进程清理结果未确认，请保留本机任务记录并核对。", flush=True)
        finally:
            # Closing this handle also reclaims descendants, such as esbuild.
            if self.job:
                self.job.close()
            for stream in self.logs:
                stream.close()

    def wait(self) -> None:
        while True:
            for name, child in self.children:
                if child.poll() is not None:
                    raise RuntimeError(f"{name} 已退出。请查看日志并重新启动。")
            # A busy API may briefly miss an HTTP timeout. Do not interrupt its
            # running ingestion jobs; supervise process lifetime after startup.
            time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="启动后不打开浏览器")
    parser.add_argument("--check", action="store_true", help="只读核对当前服务身份，不启动或停止服务")
    args = parser.parse_args()
    if args.check:
        try:
            expected = expected_backend_identity()
        except (OSError, RuntimeError):
            print(json.dumps({"success": False, "reason": "runtime_identity_unavailable"}))
            return 1
        backend_ready = service_ready("backend", expected_identity=expected)
        frontend_ready = service_ready("frontend", expected_identity=expected)
        print(json.dumps({"success": backend_ready and frontend_ready, "project_id": expected["project_id"],
                          "backend_ready": backend_ready, "frontend_ready": frontend_ready}))
        return 0 if backend_ready and frontend_ready else 1
    supervisor = LocalSupervisor()
    launcher_lock = None
    try:
        python_exe = PROJECT_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
        vite_entry = PROJECT_ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if not python_exe.is_file():
            raise RuntimeError("找不到 backend\\.venv\\Scripts\\python.exe，请先安装后端依赖。")
        if not vite_entry.is_file():
            raise RuntimeError("找不到前端依赖，请先在 frontend 文件夹执行 npm install。")
        node_exe = shutil.which("node.exe")
        if not node_exe:
            raise RuntimeError("找不到 Node.js，请先安装并重新打开启动窗口。")
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            import msvcrt
            launcher_lock = (LOG_ROOT / "launcher.lock").open("a+b")
            launcher_lock.write(b"0")
            launcher_lock.flush()
            launcher_lock.seek(0)
            try:
                msvcrt.locking(launcher_lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"启动控制窗口已打开，请继续使用那个窗口。网站地址：{URL}") from exc
        supervisor.start("backend", 8000, [str(python_exe), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], PROJECT_ROOT / "backend")
        supervisor.start("frontend", 5173, [node_exe, str(vite_entry), "--host", "127.0.0.1", "--port", "5173", "--strictPort"], PROJECT_ROOT / "frontend")
        print(f"\n项目已就绪：{URL}", flush=True)
        print("请保持此控制窗口打开。按 Ctrl+C 停止本窗口启动的服务。", flush=True)
        print("已存在的服务仅复用，关闭本窗口不会停止它们。", flush=True)
        print(f"本地日志：{LOG_ROOT}", flush=True)
        if not args.no_browser:
            import webbrowser
            webbrowser.open(URL)
        supervisor.wait()
    except KeyboardInterrupt:
        print("\n正在关闭…", flush=True)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"\n启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        supervisor.stop()
        if launcher_lock:
            launcher_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
