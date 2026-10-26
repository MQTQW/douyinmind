"""Explicitly run the isolated receiver; preview is the default.

Run from backend: python -m app.mention_receiver
An explicit --agent-binding enables scoped model tools and a separate task
ledger. Only --agent-auto-reply opts into one bounded publication attempt.
Only --agent-allow-import opts into scoped ingestion work; the original
application server is never started here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import time
from app.assistant.legacy_policy import legacy_testing_blocked, MESSAGE as MIGRATED_MESSAGE

from .browser import NormalPageReceiver, ReceiverPageError, ReceiverInterventionRequired, ReceiverDetailError, SELF_URL, browser_options, isolated_profile
from .catalog import read_libraries, configured_database
from .core import SQLiteInbox
from .dashboard import write_dashboard
from .runtime import RunHistory, RUNTIME_FILES, request_stop

BACKEND = Path(__file__).resolve().parents[2]
STORAGE = BACKEND / "app" / "storage"
STATE_DIR = STORAGE / "mention_receiver"


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def receiver_lock(root: Path):
    """OS-held lock is released even when a process is terminated."""
    import os
    stream = (root / "receiver.lock").open("a+b")
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise AlreadyRunningError("助手接收测试已在运行") from None
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def configuration(root: Path) -> dict:
    config = json.loads((root / "receiver.local.json").read_text(encoding="utf-8"))
    number = config.get("receiver_public_number", "")
    if not isinstance(number, str) or not number.isdigit() or not 5 <= len(number) <= 30:
        raise ValueError("请先配置助手 B 的公开抖音号")
    from .browser import canonical_profile
    config["receiver_profile_url"] = canonical_profile(config.get("receiver_profile_url", ""))
    return config


def preflight_state(root: Path, protected_profile: Path, database: Path) -> Path:
    """All collision checks happen before mkdir, locks or SQLite writes."""
    if root.resolve() != root or root.is_symlink():
        raise ValueError("助手数据目录存在重定向")
    original = protected_profile.resolve()
    if root == original or root.is_relative_to(original) or original.is_relative_to(root):
        raise ValueError("助手数据目录与原收藏账号目录重叠")
    if database.resolve().is_relative_to(root):
        raise ValueError("业务数据库位于助手数据目录内")
    profile = isolated_profile(root.parent, original)
    for name in ("inbox.sqlite3", "receiver.lock", "receiver-identity.json", "receiver.local.json", "status.json", "last-page-probe.json", "last-detail-probe.json", "接收测试台.html", *RUNTIME_FILES):
        path = root / name
        if path.resolve() != path or path.is_symlink():
            raise ValueError("助手文件存在重定向")
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise ValueError("助手文件类型异常或存在硬链接")
    return profile


def bind_receiver(root: Path, config: dict) -> None:
    path = root / "receiver-identity.json"
    identity = {key: config[key] for key in ("receiver_public_number", "receiver_profile_url")}
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != identity:
            raise ValueError("本台账已绑定另一助手账号，不能混用")
    else:
        path.write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def consume_detail(inbox, notice, detail, libraries, *, origin="live_normal_page"):
    require_live_fingerprint(notice, origin=origin)
    candidate = inbox.observe_notification(notice["source_profile_url"], notice["exact_text"], notice.get("semantic_fingerprint", ""))
    if candidate.status in {"received", "needs_review"}:
        return candidate
    return inbox.complete_notification(candidate.notification_key,
        sender_profile_url=notice["source_profile_url"], video_id=detail.get("video_id"),
        command_text=detail.get("command_text"), libraries=libraries,
        video_title=detail.get("video_title"), author_display_name=detail.get("author_display_name"),
        source_url=detail.get("source_url"),
        extra_metadata={"origin": origin, "sender_display_name": notice.get("sender_display_name", ""),
                        "receiver_identity_binding": "public_number_checked_profile_link_manually_configured"
                            if origin == "live_normal_page" else "not_checked_by_replay"})


def require_live_fingerprint(notice, *, origin="live_normal_page"):
    if origin == "live_normal_page" and not str(notice.get("semantic_fingerprint") or "").strip():
        raise ReceiverPageError("提醒封面尚未就绪，暂不保存不完整候选")


def replay(path: Path, inbox, libraries) -> None:
    value = json.loads(path.read_text(encoding="utf-8"))
    consume_detail(inbox, value["notice"], value["detail"], libraries, origin="sample_replay")
    write_dashboard(STATE_DIR, inbox, state="回放检查完成", mode="replay",
                    message="保存样本已经过接收核心；重复运行不会重复生成预览。样本真实性需另核对，自动在线接收仍需独立助手窗口登录。")


def run_live(args, inbox, libraries, config, profile, history: RunHistory, agent_handler=None) -> None:
    from playwright.sync_api import sync_playwright, TimeoutError as PageTimeout, Error as BrowserError
    auto_reply = getattr(args, "agent_auto_reply", False) is True
    allow_import = getattr(args, "agent_allow_import", False) is True
    if auto_reply and (agent_handler is None or getattr(args, "agent_max_tasks", 1) != 1):
        raise ValueError("自动回复需要绑定账号与单任务上限")
    if allow_import and (agent_handler is None or getattr(args, "agent_max_tasks", 1) != 1):
        raise ValueError("视频入库需要绑定账号与单任务上限")
    started = time.monotonic()
    def expired():
        return args.duration_minutes > 0 and time.monotonic() - started >= args.duration_minutes * 60
    def show(state, message):
        mode = "agent_auto_reply" if auto_reply else "agent" if agent_handler is not None else "live"
        write_dashboard(STATE_DIR, inbox, state=state, message=message, mode=mode,
                        health={"run_id": history.run_id, **history.summary()})
    def agent_exhausted():
        return agent_handler is not None and getattr(agent_handler, "exhausted", False) is True
    show("等待助手窗口", "正在打开独立助手浏览器。原收藏浏览器和原后台均不启动或重启。"
         + ("已显式启用本次单任务视频入库与一次回复；只处理当前视频和绑定库，结果以任务台账为准。" if allow_import and auto_reply else
            "已显式启用本次单任务视频入库；结果保存在独立任务台账，只留回复草稿。" if allow_import else
            "已显式启用本次单任务自动回复；发送前重新核对原评论，不导入视频。" if auto_reply else
            "已启用绑定账号的智能工具收件；只生成待发送回复，不自动发评或导入。" if agent_handler is not None else ""))
    reason = "completed"
    pause_latch = None
    publisher_called = False
    publish_state = None

    def pause_message():
        if pause_latch == "verification_required":
            return "网页需要本人处理核验，本轮已锁定暂停：不再刷新、收件或输入；请核对现场后停止，或等待运行截止。"
        return "本次提交结果不明，本轮已锁定暂停：不再刷新、收件或输入，也不重发；请本人核对原评论后停止，或等待运行截止。"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(profile), headless=False,
            viewport={"width": 1100, "height": 760}, **browser_options(STORAGE))
        try:
            page = context.pages[0] if context.pages else context.new_page()
            adapter = NormalPageReceiver(page, config["receiver_public_number"], config["receiver_profile_url"],
                                        open_strategy=config.get("notification_open_strategy", "hover"))
            needs_reload = True
            recovering = False

            def should_stop():
                # Model-task exhaustion must not cancel its freshly ready reply.
                return history.stop_requested() or expired() or page.is_closed()

            if allow_import:
                # The ingestion wait uses only this read-only cancellation
                # predicate; it cannot navigate or submit through the browser.
                agent_handler.service.import_should_stop = should_stop

            publisher = None
            if auto_reply:
                from app.mention_agent.publisher import ReplyPublisher
                evidence_root = Path(getattr(args, "agent_state",
                    STATE_DIR.parent / "mention_agent" / "agent.sqlite3")).resolve().parent / "reply-evidence"
                publisher = ReplyPublisher(agent_handler.service.store, agent_handler.service.binding,
                    adapter, evidence_root, should_stop=should_stop,
                    scope_authorizer=lambda _:not legacy_testing_blocked())

            def wait_interruptibly(seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline and not expired() and not history.stop_requested() and not page.is_closed():
                    try:
                        page.wait_for_timeout(min(1000, max(1, (deadline - time.monotonic()) * 1000)))
                    except BrowserError:
                        return

            def save_probe(error=None):
                # Diagnostics never change the receiver's control flow or hide
                # the original failure. No raw exception message is persisted.
                try:
                    probe = adapter.diagnostics()
                    if error is not None:
                        text = str(error)
                        probe["timeout_actionability"] = {label: phrase in text for label, phrase in {
                            "pointer_intercepted": "intercepts pointer events", "outside_viewport": "outside of the viewport",
                            "not_visible": "not visible", "not_stable": "not stable"}.items()}
                    (STATE_DIR / "last-page-probe.json").write_text(json.dumps(probe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                except Exception:
                    pass

            while True:
                if history.stop_requested():
                    reason = "stopped"
                    break
                if expired():
                    reason = "duration_elapsed"
                    break
                if page.is_closed():
                    reason = "window_closed"
                    break
                if pause_latch is not None:
                    # Permanent for this run, even if a challenge disappears.
                    # No adapter calls, navigation, metrics DOM reads or input.
                    wait_started = time.monotonic()
                    wait_interruptibly(5)
                    history.record_cycle("paused", "wait", (time.monotonic() - wait_started) * 1000,
                        notice_count=0, new_event_count=0, open_method="none",
                        ui_state={"navigation": False})
                    show("等待本人核对，本轮已暂停", pause_message())
                    continue
                if publisher_called:
                    break
                if agent_exhausted():
                    reason = "agent_task_limit"
                    break
                if args.max_cycles and history.summary()["cycle_count"] >= args.max_cycles:
                    break
                cycle_started = time.monotonic()
                outcome, phase = "error", "identity"
                notice_count = retry_count = new_events = 0
                agent_new_tasks = 0
                navigation = needs_reload
                recovery_count = int(recovering)
                delay = args.poll_seconds
                state, message = "正在核对", "正在核对独立助手页面。"
                adapter.begin_cycle()
                event_count_before = len(inbox.list_events()) if agent_handler is None else 0
                try:
                    if needs_reload:
                        adapter.stage = "载入助手主页"
                        page.goto(SELF_URL, wait_until="load", timeout=60000)
                        needs_reload = False
                    identity = adapter.identity()
                    if identity is None:
                        outcome, delay = "paused", 4
                        state = "等待 B 手动登录或主页载入"
                        message = "请核对助手窗口已登录 B（公开号 " + config["receiver_public_number"] + "）并在“我的”页。无需向聊天发送验证码。"
                    else:
                        phase = "notifications"
                        visible_notices = adapter.notifications()
                        if agent_handler is None:
                            notices = [notice for notice in visible_notices if any(
                                word in notice["exact_text"] for word in ("加入", "导入", "上传"))]
                        else:
                            notices = [notice for notice in visible_notices if agent_handler.accepts_notice(notice)]
                        notice_count = len(notices)
                        for notice in notices:
                            require_live_fingerprint(notice)
                        # The preview ledger cannot authorize agent work. Agent
                        # intake starts only with fresh, revalidated UI details.
                        candidates = ([(notice, inbox.observe_notification(notice["source_profile_url"], notice["exact_text"],
                            notice.get("semantic_fingerprint", ""))) for notice in notices] if agent_handler is None
                            else [(notice, None) for notice in notices])
                        for notice, candidate in candidates:
                            if agent_exhausted():
                                reason = "agent_task_limit"
                                break
                            if candidate is not None and candidate.status not in {"pending", "retryable"}:
                                continue
                            phase = "details"
                            try:
                                adapter.notifications()
                                detail = adapter.detail(notice)
                                if agent_handler is None:
                                    consume_detail(inbox, notice, detail, libraries)
                                else:
                                    result = agent_handler.handle_detail(notice, detail)
                                    if result.get("created") is True:
                                        agent_new_tasks += 1
                                    if allow_import and should_stop():
                                        reason = ("stopped" if history.stop_requested() else
                                                  "duration_elapsed" if expired() else "window_closed")
                                        break
                                    if (publisher is not None and result.get("state") == "answer_ready"
                                            and isinstance(result.get("task_id"), str) and result["task_id"]):
                                        # A ready task recovered after restart is
                                        # eligible only through this fresh detail.
                                        publisher_called = True
                                        try:
                                            published = publisher.publish(result["task_id"])
                                            publish_state = published.get("state")
                                        except ReceiverInterventionRequired:
                                            raise
                                        except Exception:
                                            # An exception cannot prove that an
                                            # external side effect never occurred.
                                            publish_state = "delivery_unknown"
                                        if publish_state not in {"b_view_visible", "delivery_unknown",
                                                "verification_required", "failed_no_submit",
                                                "preflight_blocked", "not_sendable"}:
                                            publish_state = "delivery_unknown"
                                        if publish_state in {"verification_required", "delivery_unknown"}:
                                            pause_latch = publish_state
                                    if agent_exhausted():
                                        reason = "agent_task_limit"
                                    if pause_latch is not None or publisher_called or agent_exhausted():
                                        break
                            except ReceiverInterventionRequired:
                                raise
                            except ReceiverDetailError as exc:
                                if candidate is not None:
                                    inbox.mark_retryable(candidate.notification_key, str(exc))
                                retry_count += 1
                                try:
                                    (STATE_DIR / "last-detail-probe.json").write_text(json.dumps({"run_id":history.run_id,"diagnostics":exc.diagnostics}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                                except (OSError, ValueError):
                                    pass
                            except (ReceiverPageError, PageTimeout):
                                if candidate is not None:
                                    inbox.mark_retryable(candidate.notification_key, "步骤“" + adapter.stage + "”尚未核对完整，等待正常页面重试")
                                retry_count += 1
                                save_probe()
                            except ValueError:
                                if agent_handler is None:
                                    raise
                                # Do not persist model/provider exceptions or
                                # promote incomplete evidence into a task.
                                retry_count += 1
                        outcome = "error" if retry_count else "success"
                        phase = "details" if retry_count else "cycle"
                        state = "部分提醒待重试" if retry_count else "正在检查最近通知"
                        if pause_latch is not None:
                            outcome, phase, state, delay = "paused", "details", "等待本人核对，本轮已暂停", 5
                            message = pause_message()
                        elif auto_reply and publisher_called:
                            state = "B 回读原评论可见" if publish_state == "b_view_visible" else "本次自动回复处理已停止"
                            message = ("本次回复在 B 的原评论回读中可见，A 或访客可见性需另行核对。"
                                       if publish_state == "b_view_visible" else
                                       "本次没有确认发表新回复，处理状态已保留；不会自动再次提交。")
                        elif agent_handler is None:
                            message = f"助手 {identity.display_name} 已核对；本轮看到 {notice_count} 条指令提醒，{retry_count} 条等待详情重试。所有结果仅作预览。"
                        else:
                            state = "已达到本次智能任务上限" if reason == "agent_task_limit" else state
                            message = (f"助手 {identity.display_name} 已核对；本轮看到 {notice_count} 条绑定账号提醒，"
                                       f"新建 {agent_new_tasks} 个智能任务，{retry_count} 条等待核对。"
                                       + ("结果保存在独立任务台账；入库状态按真实处理回执记录，未自动发评。" if allow_import else
                                          "结果保存在独立任务台账；没有自动发评或导入。"))
                        needs_reload = pause_latch is None
                        recovering = bool(retry_count)
                except ReceiverInterventionRequired as exc:
                    outcome, state, message, delay = "paused", "需要本人处理", str(exc), 5
                    if auto_reply:
                        pause_latch = "verification_required"
                        state, message = "等待本人核对，本轮已暂停", pause_message()
                    # Keep the exact page for the human; no reload or input.
                    needs_reload = False
                except ReceiverPageError as exc:
                    outcome, state, message = "error", "页面待恢复", str(exc)
                    save_probe()
                    needs_reload = recovering = True
                except PageTimeout as exc:
                    try:
                        adapter.check_intervention()
                    except ReceiverInterventionRequired as intervention:
                        outcome, state, message, delay = "paused", "需要本人处理", str(intervention), 5
                        if auto_reply:
                            pause_latch = "verification_required"
                            state, message = "等待本人核对，本轮已暂停", pause_message()
                        needs_reload = False
                    except BrowserError:
                        if page.is_closed():
                            outcome, phase, reason = "paused", "shutdown", "window_closed"
                            state, message = "助手窗口已关闭", "本次接收即将正常停止。"
                        else:
                            outcome, state, message = "error", "浏览器连接待恢复", "收件进度保留，下一轮重新核对页面。"
                            needs_reload = recovering = True
                    else:
                        outcome, state = "timeout", "页面等待超时"
                        message = "步骤“" + adapter.stage + "”尚未完成，记录保留；下一轮正常刷新后重试。"
                        save_probe(exc)
                        needs_reload = recovering = True
                except BrowserError:
                    if page.is_closed():
                        outcome, phase, reason = "paused", "shutdown", "window_closed"
                        state, message = "助手窗口已关闭", "本次接收即将正常停止。"
                    else:
                        outcome, state, message = "error", "浏览器连接待恢复", "收件进度已保留，下一轮重新核对页面。"
                        needs_reload = recovering = True
                finally:
                    new_events = (max(0, len(inbox.list_events()) - event_count_before)
                                  if agent_handler is None else agent_new_tasks)
                    try:
                        ui = adapter.ui_state() if pause_latch is None and not page.is_closed() else {}
                    except Exception:
                        ui = {}
                    ui["navigation"] = navigation
                    history.record_cycle(outcome, phase, (time.monotonic() - cycle_started) * 1000,
                        notice_count=notice_count, new_event_count=new_events, open_method=adapter.open_method,
                        recovery_count=recovery_count + getattr(adapter, "open_retries", 0), detail_retry_count=retry_count,
                        open_attempts=adapter.open_attempts, ui_state=ui)
                    show(state, message)
                if pause_latch is not None:
                    # max task/cycle limits cannot close a verification scene.
                    continue
                if reason in {"window_closed", "agent_task_limit", "stopped", "duration_elapsed"} or publisher_called:
                    break
                if not args.max_cycles or history.summary()["cycle_count"] < args.max_cycles:
                    wait_interruptibly(delay)
        finally:
            try:
                context.close()
            except BrowserError:
                pass
    # Keep the existing run-history vocabulary compatible with old readers.
    history.finish("completed" if reason == "agent_task_limit" else reason)
    if auto_reply:
        if pause_latch is not None:
            show("运行已停止，仍待本人核对", pause_message() + "本次窗口已正常结束，任务状态保留。")
        elif publish_state == "b_view_visible":
            show("本次自动回复已结束", "B 回读原评论可见；A 或访客可见性需另行核对。任务与发送状态已保留。")
        else:
            show("本次自动回复运行已停止", "未确认发表新回复；智能任务及处理状态保留，"
                 + ("入库结果请按台账核对，不会自动追加提交。" if allow_import else "没有自动导入或追加提交。"))
        return
    show("测试已停止", "本次独立助手运行已正常结束。收件和每轮记录保留，下次启动继续去重。"
         if agent_handler is None else
         ("已达到本次智能任务上限，收件已正常停止。" if reason == "agent_task_limit"
          else "本次智能工具收件已正常停止。")
          + ("独立任务与待发送回复保留；没有自动发评，入库结果请按台账核对。" if allow_import else
             "独立任务与待发送回复保留；没有自动发评或导入。"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DouyinMind 独立助手 @ 接收；默认仅预览，可显式启用绑定账号的智能工具")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--replay", type=Path, help="回放已保存的最小通知/详情 JSON；不打开浏览器")
    modes.add_argument("--stop", action="store_true", help="请求当前独立助手正常停止，保留登录和收件记录")
    parser.add_argument("--poll-seconds", type=int, default=45)
    parser.add_argument("--duration-minutes", type=int, default=60, help="本次测试窗口，默认60分钟；0表示直到本人关闭")
    parser.add_argument("--max-cycles", type=int, default=0, help="最多尝试的轮次，包含超时和暂停；0表示不限轮数")
    parser.add_argument("--agent-binding", type=Path, help="显式启用已绑定账号的智能工具；填写本机公开身份/知识库绑定JSON")
    parser.add_argument("--agent-marker", help="智能收件必须指定本次测试标记，只接收带该标记的绑定账号评论（最多80字符）")
    parser.add_argument("--agent-state", type=Path, default=STORAGE / "mention_agent" / "agent.sqlite3",
                        help="智能任务独立台账；默认 app/storage/mention_agent/agent.sqlite3")
    parser.add_argument("--agent-max-tasks", type=int, default=1,
                        help="本次最多触发的新模型任务数，默认1；自动回复模式要求1")
    parser.add_argument("--agent-auto-reply", action="store_true",
                        help="显式启用一次产品自动回评；须同时指定绑定、新标记和任务上限1，默认只留草稿")
    parser.add_argument("--agent-allow-import", action="store_true",
                        help="显式启用一次当前视频入库；须同时指定绑定、新标记和任务上限1，默认仅只读问答")
    args = parser.parse_args(argv)
    if args.poll_seconds < 20 or args.duration_minutes < 0 or args.max_cycles < 0:
        parser.error("检查间隔至少20秒，时间和轮数不得为负")
    if args.agent_max_tasks < 1:
        parser.error("智能任务上限至少为1")
    if args.agent_binding and (args.replay or args.stop):
        parser.error("智能工具收件不能与旧回放或停止命令同时启用")
    if args.agent_binding:
        args.agent_marker = (args.agent_marker or "").strip()
        if not args.agent_marker or len(args.agent_marker) > 80:
            parser.error("智能工具收件需要非空且最多80字符的本次测试标记 --agent-marker")
    if args.agent_auto_reply and (not args.agent_binding or args.agent_max_tasks != 1):
        parser.error("自动回复须同时启用 --agent-binding、--agent-marker 和 --agent-max-tasks 1")
    if args.agent_allow_import and (not args.agent_binding or not args.agent_marker
                                   or args.agent_max_tasks != 1 or args.replay or args.stop):
        parser.error("视频入库须同时启用 --agent-binding、--agent-marker 和 --agent-max-tasks 1，不能回放或停止")
    if args.agent_binding and legacy_testing_blocked():
        print(MIGRATED_MESSAGE)
        return 2
    inbox = None
    try:
        # Only settings are read; no engine, application lifecycle or worker.
        from app.core.config import settings
        database = configured_database(settings.database_url, BACKEND)
        protected = Path(settings.playwright_user_data_dir)
        protected = protected if protected.is_absolute() else BACKEND / protected
        profile = preflight_state(STATE_DIR, protected, database)
        if args.stop:
            print("已请求独立助手正常停止。" if request_stop(STATE_DIR) else "当前没有已登记的运行。")
            return 0
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with receiver_lock(STATE_DIR):
            try:
                config = configuration(STATE_DIR)
                bind_receiver(STATE_DIR, config)
                inbox = SQLiteInbox(STATE_DIR / "inbox.sqlite3")
                # Retain older incomplete startup records as audited review
                # rows; never guess which video they belonged to or retry them.
                if args.agent_binding is None:
                    inbox.quarantine_incomplete_candidates()
                libraries = read_libraries(database) if args.agent_binding is None else []
                agent_handler = None
                if args.agent_binding:
                    from app.mention_agent.service import build_receiver_handler
                    agent_handler = build_receiver_handler(
                        binding_path=args.agent_binding, state_path=args.agent_state,
                        max_tasks=args.agent_max_tasks, required_marker=args.agent_marker,
                        **({"allow_import": True} if args.agent_allow_import else {}))
                if args.replay:
                    replay(args.replay, inbox, libraries)
                else:
                    with RunHistory(STATE_DIR, config["receiver_public_number"]) as history:
                        run_live(args, inbox, libraries, config, profile, history, agent_handler=agent_handler)
            except KeyboardInterrupt:
                if inbox is not None:
                    write_dashboard(STATE_DIR, inbox, state="本人停止测试",
                                    mode="agent_auto_reply" if args.agent_auto_reply else "agent" if args.agent_binding else "live",
                                    message="收件记录已保留，未执行导入。" if not args.agent_binding
                                     else "任务、入库与发布状态均已保留，结果请按台账核对；不会自动追加提交。" if args.agent_allow_import
                                    else "任务与发布状态已保留，请按台账核对结果，勿重复提交。" if args.agent_auto_reply
                                    else "独立任务台账已保留，没有自动发评或导入。")
                return 0
            except Exception as exc:
                if inbox is not None:
                    write_dashboard(STATE_DIR, inbox, state="接收原型已停止",
                                    mode="agent_auto_reply" if args.agent_auto_reply else "agent" if args.agent_binding else "live",
                                    message="未能启动或页面连接已结束（" + type(exc).__name__ + "）。记录已保留，请核对助手窗口和本地配置。")
                else:
                    print("接收原型预检未通过（" + type(exc).__name__ + "），未启动浏览器或写入收件台账。")
                return 1
        return 0
    except AlreadyRunningError:
        print("助手接收测试已在运行，请使用现有助手窗口。")
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Avoid exporting Playwright exception strings, URLs or authentication state.
        print("接收原型预检未通过（" + type(exc).__name__ + "），未启动浏览器或写入收件台账。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
