"""Login/readiness state and profile ownership with authored browser objects."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.routes import auth
from app.services import douyin_collector as module
from app.services.douyin_collector import DouyinCollector, FavoriteScrapeSnapshot


def fake_browser(monkeypatch, *, cookies=True, failure=None):
    collector = DouyinCollector()
    collector._set_login_state("pending", "logging_in", "waiting")
    seen = []

    def navigate(*args, **kwargs):
        seen.append(("navigate", collector.login_state()))
        if failure == "navigate":
            raise RuntimeError("authored upstream private detail")

    page = SimpleNamespace(goto=navigate)

    class Context:
        pages = [page]
        closed = False
        saved = False

        def cookies(self):
            return [{"name": "sessionid", "value": "authored-cookie"}] if cookies else []

        def storage_state(self, **kwargs):
            seen.append(("save", collector.login_state()))
            if failure == "save":
                raise RuntimeError("authored upstream private detail")
            self.saved = True

        def close(self):
            seen.append(("close", collector.login_state()))
            self.closed = True
            if failure == "close":
                raise RuntimeError("authored upstream private detail")

    context = Context()

    class Playwright:
        chromium = SimpleNamespace(launch_persistent_context=lambda **kwargs: context)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(module, "sync_playwright", Playwright)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(collector, "_browser_launch_kwargs", lambda: {})

    def fetch(actual_page):
        assert actual_page is page
        state = collector.login_state()
        seen.append(("fetch", state))
        assert state["status"] == "pending" and state["phase"] == "scraping"
        assert state["snapshot_ready"] is False
        if failure == "fetch":
            raise RuntimeError("module_not_found authored private detail")
        return FavoriteScrapeSnapshot(complete=failure != "incomplete")

    monkeypatch.setattr(collector, "_fetch_in_context", fetch)
    return collector, context, seen


def test_snapshot_is_published_only_after_complete_fetch_state_save_and_context_close(monkeypatch):
    collector, context, seen = fake_browser(monkeypatch)
    collector._login_and_fetch_sync_locked()
    assert [name for name, _ in seen] == ["navigate", "fetch", "save", "close"]
    assert all(state["snapshot_ready"] is False for _, state in seen)
    state = collector.login_state()
    assert context.closed and context.saved
    assert state["status"] == "logged_in" and state["phase"] == "ready"
    assert state["snapshot_ready"] is True and state["busy"] is False
    assert asyncio.run(collector.fetch_snapshot()).complete is True


@pytest.mark.parametrize("failure", ["navigate", "fetch", "incomplete", "save", "close"])
def test_every_failure_closes_context_and_never_exposes_internal_error_as_login_success(monkeypatch, failure):
    collector, context, seen = fake_browser(monkeypatch, failure=failure)
    collector._login_and_fetch_sync_locked()
    state = collector.login_state()
    assert context.closed
    assert state["status"] == state["phase"] == "failed"
    assert not state["snapshot_ready"] and collector._snapshot is None
    assert "private detail" not in state["message"] and "module_not_found" not in state["message"]
    assert all(not row["snapshot_ready"] for _, row in seen)
    with pytest.raises(RuntimeError, match="重新连接"):
        asyncio.run(collector.fetch_snapshot())


def test_login_timeout_closes_context_and_keeps_readiness_false(monkeypatch):
    collector, context, seen = fake_browser(monkeypatch, cookies=False)
    collector._login_and_fetch_sync_locked()
    assert context.closed and not context.saved
    assert [name for name, _ in seen] == ["navigate", "close"]
    state = collector.login_state()
    assert state["status"] == state["phase"] == "failed"
    assert not state["snapshot_ready"] and "超时" in state["message"]


@pytest.mark.parametrize("reported_status", ["pending", "logged_in", "failed"])
def test_active_task_is_singleflight_independent_of_reported_status(monkeypatch, reported_status):
    async def scenario():
        collector = DouyinCollector()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def flow():
            calls.append("started")
            collector._set_login_state(reported_status, "scraping", "reading")
            entered.set()
            await release.wait()
            collector._set_login_state("logged_in", "ready", "ready", snapshot=FavoriteScrapeSnapshot(complete=True))

        monkeypatch.setattr(collector, "_login_flow", flow)
        success, _ = collector.start_login()
        assert success
        task = collector._login_task
        await entered.wait()
        state = collector.login_state()
        assert state["busy"] and not state["snapshot_ready"]
        second_success, message = collector.start_login()
        assert not second_success and "正在进行" in message
        assert collector._login_task is task and calls == ["started"]
        assert collector.login_state() == state
        with pytest.raises(RuntimeError, match="等待"):
            await collector.fetch_snapshot()
        release.set()
        await task
        assert collector.login_state()["snapshot_ready"]
        assert not collector.login_state()["busy"]

    asyncio.run(scenario())


def test_logout_cannot_cancel_a_queued_login_thread_or_clean_its_profile(monkeypatch):
    async def scenario():
        collector = DouyinCollector()
        release = asyncio.Event()

        async def waiting_flow():
            await release.wait()

        monkeypatch.setattr(collector, "_login_flow", waiting_flow)
        monkeypatch.setattr(collector, "_logout_locked", lambda: pytest.fail("active login profile must not be cleaned"))
        assert collector.start_login()[0]
        task = collector._login_task
        result, message = collector.logout()
        assert not result and "等待" in message
        assert collector._login_task is task and not task.cancelled()
        release.set()
        await task

    asyncio.run(scenario())


def test_new_attempt_after_completed_failure_resets_state_then_can_publish_new_snapshot(monkeypatch):
    async def scenario():
        collector = DouyinCollector()
        attempts = []

        async def flow():
            attempts.append(1)
            if len(attempts) == 1:
                collector._set_login_state("failed", "failed", "retry")
            else:
                collector._set_login_state("logged_in", "ready", "ready", snapshot=FavoriteScrapeSnapshot(complete=True))

        monkeypatch.setattr(collector, "_login_flow", flow)
        assert collector.start_login()[0]
        await collector._login_task
        assert collector.login_state()["phase"] == "failed"
        assert collector.start_login()[0]
        assert collector.login_state()["phase"] == "logging_in"
        assert not collector.login_state()["snapshot_ready"]
        await collector._login_task
        assert collector.login_state()["snapshot_ready"] and len(attempts) == 2

    asyncio.run(scenario())


def test_auth_status_start_and_logout_share_structured_readiness_fields(monkeypatch):
    async def scenario():
        collector = DouyinCollector()
        release = asyncio.Event()

        async def flow():
            await release.wait()

        monkeypatch.setattr(collector, "_login_flow", flow)
        monkeypatch.setattr(auth, "collector", collector)
        fields = {"status", "message", "phase", "snapshot_ready", "busy"}
        initial = await auth.login_status()
        assert set(initial) == fields
        assert initial["phase"] == "idle" and not initial["busy"]
        started = await auth.login_start()
        task = collector._login_task
        assert set(started) == fields | {"success"}
        assert started["success"] and started["phase"] == "logging_in" and started["busy"]
        pending = await auth.login_status()
        assert set(pending) == fields and not pending["snapshot_ready"]
        blocked_logout = await auth.logout()
        assert set(blocked_logout) == fields | {"success"}
        assert not blocked_logout["success"] and blocked_logout["busy"]
        release.set()
        await task

    asyncio.run(scenario())
