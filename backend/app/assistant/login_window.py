"""Explicit one-time visible login for the assistant's own B browser profile.

This helper starts no assistant, receiver, model or task loop. It only opens the
existing local assistant browser profile in a visible window so the operator can
complete the platform's own QR / SMS / slider login, then it closes and exits.
The normal assistant still runs headless afterwards and reuses this profile.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone

from app.core.config import settings
from app.mention_agent.service import configured_database
from app.mention_receiver.__main__ import preflight_state, receiver_lock
from app.mention_receiver.browser import SELF_URL, browser_options

BACKEND = Path(__file__).resolve().parents[2]
STORAGE = BACKEND / "app" / "storage"
RECEIVER_STATE = STORAGE / "mention_receiver"
LOGIN_URL = "https://www.douyin.com/"
MARKER_NAME = "login-ready.json"
BUSY_MESSAGE = "助手正在运行，会占用同一个浏览器目录。请先在项目网页点「停止」并等待停止，再重新运行本窗口。"
AUTO_STOP_PROMPT = "要自动停止助手并继续打开登录窗口吗？任务、会话和费用记录都会保留。[Y/n] "


def login_ready_path() -> Path:
    return RECEIVER_STATE / MARKER_NAME


def mark_login_ready() -> None:
    """Record only that a human completed the platform login on this machine."""
    path = login_ready_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "ready": True,
                                     "at": datetime.now(timezone.utc).isoformat()}),
                         encoding="utf-8")
    os.replace(temporary, path)


def clear_login_ready() -> None:
    try:
        login_ready_path().unlink()
    except FileNotFoundError:
        pass


def login_ready_marked() -> bool:
    return login_ready_path().is_file()


def probe_logged_in(api_request) -> bool:
    """Check the shared profile cookies without opening or disturbing a tab."""
    try:
        response = api_request.get(SELF_URL, max_redirects=5, timeout=15000)
        return response.status == 200 and "/user/self" in (response.url or "")
    except Exception:  # noqa: BLE001 - any probe error means "not confirmed"
        return False


def protected_profile_path() -> Path:
    protected = Path(settings.playwright_user_data_dir)
    return protected if protected.is_absolute() else BACKEND / protected


def assistant_busy(control_factory=None) -> str | None:
    """Return a fixed reason only when another product process still owns the profile."""
    if control_factory is None:
        from .control import AssistantControl
        from .queue import AssistantQueue
        control_factory = lambda: AssistantControl(  # noqa: E731
            AssistantQueue(STORAGE / "assistant" / "queue.sqlite3"))
    try:
        current = control_factory().snapshot()
    except Exception:
        return None
    if type(current) is not dict:
        return None
    if current.get("process_alive") is True:
        return "assistant_running"
    if current.get("state") not in {"stopped", "start_failed", None}:
        return "assistant_running"
    return None


def request_stop(wait_seconds: int = 90, control_factory=None) -> bool:
    """Ask the registered owner to stop and wait until the profile is released."""
    if control_factory is None:
        from .control import AssistantControl
        from .queue import AssistantQueue
        control_factory = lambda: AssistantControl(  # noqa: E731
            AssistantQueue(STORAGE / "assistant" / "queue.sqlite3"))
    try:
        control = control_factory()
        control.stop()
    except Exception:
        return False
    deadline = time.monotonic() + max(1, int(wait_seconds))
    while time.monotonic() < deadline:
        try:
            current = control.snapshot()
        except Exception:
            return False
        if (current.get("process_alive") is not True
                and current.get("state") in {"stopped", "start_failed"}):
            return True
        time.sleep(1)
    return False


def bring_login_window_to_front() -> bool:
    """Restore/raise the visible Chromium window (Windows only, best effort)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 - platform without ctypes
        return False
    user32 = ctypes.windll.user32
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    handles = []

    def _collect(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        # Only the project's own Chrome-for-Testing binary is touched.
        if "Chrome for Testing" in buffer.value:
            handles.append(hwnd)
        return True

    callback = enum_proc(_collect)
    if not user32.EnumWindows(callback, 0):
        return False
    changed = False
    for hwnd in handles:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        changed = True
    return changed


def run_login_window(wait_close: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    # ``app.mention_agent.service.configured_database`` already resolves the
    # settings URL against the backend root; it takes no arguments.
    database = configured_database()
    profile = preflight_state(RECEIVER_STATE, protected_profile_path(), database)
    with receiver_lock(RECEIVER_STATE):
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False, viewport={"width": 1100, "height": 800},
                **browser_options(STORAGE))
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(1)
                bring_login_window_to_front()
                if wait_close:
                    print("浏览器窗口已打开。请在窗口中完成助手 B 的登录；")
                    print("登录成功后本程序会自动记录，关闭浏览器窗口即结束。")
                    deadline = time.monotonic() + 30 * 60
                    ready = False
                    while time.monotonic() < deadline:
                        if page.is_closed():
                            break
                        if probe_logged_in(context.request):
                            ready = True
                            mark_login_ready()
                            print("已确认助手 B 登录成功；登录状态只保存在本机浏览器目录。")
                            break
                        time.sleep(3)
                    if ready:
                        remaining = max(1000, int((deadline - time.monotonic()) * 1000))
                        try:
                            if not page.is_closed():
                                page.wait_for_event("close", timeout=remaining)
                        except Exception:  # noqa: BLE001 - timeout or closed
                            pass
                        return 0
                    print("未确认到助手 B 登录成功；如已登录，可再次运行本窗口。")
                    return 1
                print("请在打开的浏览器窗口中完成助手 B 的登录：")
                print("  1) 点「登录」并扫码；")
                print("  2) 若出现「身份验证」，直接点「接收短信验证码」或「发送短信验证」，")
                print("     也可以在窗口里直接拖动滑块、输入短信验证码；")
                print("  3) 看到自己的抖音页面后，回到本窗口按 Enter 关闭。")
                try:
                    input()
                except EOFError:
                    pass
                if probe_logged_in(context.request):
                    mark_login_ready()
                    print("已确认助手 B 登录成功；登录状态只保存在本机浏览器目录。")
                else:
                    print("未确认到助手 B 登录成功；如未完成可再次运行本窗口。")
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001 - already closed by the operator
                    pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="可见窗口登录助手 B（一次性本机登录，不启动助手）")
    parser.add_argument("--ignore-assistant-state", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-close", action="store_true",
                        help="打开窗口后等操作者关闭浏览器即退出（不需要在控制台按 Enter）")
    args = parser.parse_args(argv)
    if assistant_busy() == "assistant_running" and not args.ignore_assistant_state:
        print("检测到助手正在运行。可见登录窗口需要先停止助手。")
        try:
            answer = input(AUTO_STOP_PROMPT)
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"", "y", "yes", "是", "好"}:
            print("已取消。" + BUSY_MESSAGE)
            return 2
        print("正在停止助手，请稍候……")
        if not request_stop():
            print("助手未能在限时内停止；请先在项目网页点「停止」后重试。")
            return 2
        time.sleep(2)
    try:
        return run_login_window(wait_close=args.wait_close)
    except Exception as error:  # noqa: BLE001 - never echo page/account text
        print("登录窗口未能启动（" + type(error).__name__ + "）。请确认助手已停止，再重试本窗口。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
