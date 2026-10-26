"""Receiver lifecycle regressions using fake browser pages and temporary state.

Only the inbox, run history and dashboard implementations are real. No browser
is launched, no account/profile is opened, and no project database is read.
"""
from collections import deque
from contextlib import ExitStack
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import Error as BrowserError, TimeoutError as PageTimeout

from app.mention_receiver import __main__ as runner
from app.mention_receiver.browser import ReceiverIdentity, ReceiverInterventionRequired, ReceiverPageError
from app.mention_receiver.core import SQLiteInbox
from app.mention_receiver.runtime import RunHistory, request_stop


def notification_fixture(*, marker="DM-FP-COMPLETE", fingerprint="/public-cover.jpg"):
    return {
        "source_profile_url": "https://www.douyin.com/user/sender-fixture",
        "sender_display_name": "发送者样本",
        "exact_text": "@助手样本 帮我把这个视频加入‘测试知识库’，测试编号 " + marker + "。",
        "semantic_fingerprint": fingerprint,
    }


def detail_fixture(notice):
    return {
        "video_id": "7420000000000000001",
        "command_text": notice["exact_text"],
        "video_title": "公开视频样本",
        "source_url": "https://www.douyin.com/video/7420000000000000001",
    }


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def advance(self, milliseconds):
        self.value += milliseconds / 1000


class FakePage:
    def __init__(self, clock):
        self.clock = clock
        self.closed = False
        self.navigations = []
        self.waits = []
        self.on_wait = None

    def goto(self, url, **options):
        self.navigations.append((url, options))

    def is_closed(self):
        return self.closed

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        self.clock.advance(milliseconds)
        callback, self.on_wait = self.on_wait, None
        if callback:
            callback()


class FakeContext:
    def __init__(self, page):
        self.pages = [page]
        self.new_page_count = 0
        self.close_count = 0

    def new_page(self):
        self.new_page_count += 1
        raise AssertionError("the existing receiver page must be reused")

    def close(self):
        self.close_count += 1
        self.pages[0].closed = True


class FakePlaywright:
    def __init__(self, context):
        self.context = context
        self.launches = []
        self.chromium = self
        self.exit_count = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.exit_count += 1

    def launch_persistent_context(self, profile, **options):
        self.launches.append((profile, options))
        return self.context


class ScriptedAdapter:
    def __init__(self, page, *, identities=(), notifications=(), interventions=(), diagnostic_error=None):
        self.page = page
        self.identities = deque(identities)
        self.notification_results = deque(notifications)
        self.intervention_results = deque(interventions)
        self.diagnostic_error = diagnostic_error
        self.stage = "fixture"
        self.open_method = "none"
        self.open_attempts = 0
        self.begin_count = 0
        self.identity_count = 0
        self.notification_count = 0
        self.diagnostic_count = 0
        self.intervention_check_count = 0

    def begin_cycle(self):
        self.begin_count += 1
        # A loop-bound regression fails promptly instead of hanging the suite.
        if self.begin_count > 10:
            raise AssertionError("the receiver exceeded the bounded fixture run")
        self.open_method = "none"
        self.open_attempts = 0

    def identity(self):
        self.identity_count += 1
        value = self.identities.popleft() if self.identities else ReceiverIdentity("123456", "助手样本")
        if isinstance(value, Exception):
            raise value
        return value

    def notifications(self):
        self.notification_count += 1
        self.stage = "fixture-notifications"
        self.open_method = "hover"
        self.open_attempts = 1
        value = self.notification_results.popleft() if self.notification_results else []
        if value == "close-window":
            self.page.closed = True
            raise BrowserError("fixture target closed")
        if isinstance(value, Exception):
            raise value
        return value

    def diagnostics(self):
        self.diagnostic_count += 1
        if self.diagnostic_error:
            raise self.diagnostic_error
        return {"stage": "fixture", "visible_labels": {}}

    def check_intervention(self):
        self.intervention_check_count += 1
        result = self.intervention_results.popleft() if self.intervention_results else None
        if result == "close-window":
            self.page.closed = True
            raise BrowserError("fixture target closed while checking verification")
        if isinstance(result, Exception):
            raise result

    def ui_state(self):
        return {"visibility_state": "visible", "focused": True,
                "panel_visible": True, "entry_hit": True, "page_count": 1}


class ReceiverLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inbox = SQLiteInbox(self.root / "inbox.sqlite3")
        self.history = RunHistory(self.root, "123456")
        self.addCleanup(self.history.finish)
        self.clock = FakeClock()
        self.page = FakePage(self.clock)
        self.context = FakeContext(self.page)
        self.playwright = FakePlaywright(self.context)
        self.config = {"receiver_public_number": "123456",
                       "receiver_profile_url": "https://www.douyin.com/user/receiver-fixture"}

    def run_receiver(self, adapter, *, max_cycles):
        args = SimpleNamespace(duration_minutes=0, poll_seconds=20, max_cycles=max_cycles)
        with ExitStack() as stack:
            stack.enter_context(patch.object(runner, "STATE_DIR", self.root))
            stack.enter_context(patch.object(runner, "STORAGE", self.root))
            stack.enter_context(patch.object(runner.time, "monotonic", self.clock.monotonic))
            stack.enter_context(patch.object(runner, "NormalPageReceiver", return_value=adapter))
            stack.enter_context(patch.object(runner, "browser_options", return_value={}))
            stack.enter_context(patch("playwright.sync_api.sync_playwright", return_value=self.playwright))
            runner.run_live(args, self.inbox, [], self.config, self.root / "browser_b", self.history)

    def final_run(self):
        return json.loads((self.root / "active-run.json").read_text(encoding="utf-8"))

    def cycles(self):
        path = self.root / "cycle-history.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assert_closed_once(self, reason):
        self.assertEqual(self.context.close_count, 1)
        self.assertEqual(self.playwright.exit_count, 1)
        self.assertEqual(len(self.playwright.launches), 1)
        self.assertEqual(self.context.new_page_count, 0)
        self.assertEqual(self.final_run()["status"], "closed")
        self.assertEqual(self.final_run()["reason"], reason)
        self.assertEqual(self.inbox.list_events(), [])

    def test_max_cycles_bounds_timeout_attempts_without_waiting_for_success(self):
        adapter = ScriptedAdapter(self.page, notifications=[PageTimeout("fixture timeout") for _ in range(3)])
        self.run_receiver(adapter, max_cycles=3)
        self.assertEqual(self.history.summary()["cycle_count"], 3)
        self.assertEqual(self.history.summary()["timeout_count"], 3)
        self.assertEqual(self.history.summary()["success_count"], 0)
        self.assertEqual(adapter.begin_count, 3)
        self.assertEqual(len(self.page.navigations), 3)
        self.assertEqual([row["outcome"] for row in self.cycles()], ["timeout"] * 3)
        self.assert_closed_once("completed")

    def test_failing_diagnostics_does_not_prevent_next_cycle_recovery(self):
        adapter = ScriptedAdapter(self.page, notifications=[PageTimeout("fixture timeout"), []],
                                  diagnostic_error=RuntimeError("fixture diagnostic failure"))
        self.run_receiver(adapter, max_cycles=2)
        self.assertEqual(adapter.diagnostic_count, 1)
        self.assertEqual([row["outcome"] for row in self.cycles()], ["timeout", "success"])
        self.assertEqual(len(self.page.navigations), 2)
        self.assertFalse((self.root / "last-page-probe.json").exists())
        self.assert_closed_once("completed")

    def test_successful_hover_retry_is_visible_in_cycle_recovery_count(self):
        adapter = ScriptedAdapter(self.page)
        notifications = adapter.notifications

        def notifications_after_retry():
            result = notifications()
            adapter.open_retries = 1
            adapter.open_attempts = 2
            return result

        with patch.object(adapter, "notifications", side_effect=notifications_after_retry):
            self.run_receiver(adapter, max_cycles=1)
        rows = self.cycles()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "success")
        self.assertEqual(rows[0]["open_method"], "hover")
        self.assertEqual(rows[0]["open_attempts"], 2)
        self.assertEqual(rows[0]["recovery_count"], 1)
        self.assertEqual(self.history.summary()["success_count"], 1)
        self.assert_closed_once("completed")

    def test_one_empty_fingerprint_rejects_entire_batch_before_any_candidate_is_saved(self):
        complete = notification_fixture()
        incomplete = notification_fixture(marker="DM-FP-INCOMPLETE", fingerprint=" \t ")
        adapter = ScriptedAdapter(self.page, notifications=[
            [complete, incomplete], [complete], [complete],
        ])
        checked_before_recovery = []

        def verify_first_cycle_left_no_partial_batch():
            self.assertEqual(self.inbox.list_candidates(), [])
            self.assertEqual(self.inbox.list_events(), [])
            self.assertEqual(self.history.summary()["error_count"], 1)
            checked_before_recovery.append(True)

        self.page.on_wait = verify_first_cycle_left_no_partial_batch
        with patch.object(adapter, "detail", return_value=detail_fixture(complete), create=True) as detail:
            self.run_receiver(adapter, max_cycles=2)
        self.assertEqual(checked_before_recovery, [True])
        self.assertEqual([row["outcome"] for row in self.cycles()], ["error", "success"])
        self.assertEqual(self.history.summary()["error_count"], 1)
        self.assertEqual(self.history.summary()["success_count"], 1)
        candidates = self.inbox.list_candidates()
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].exact_text, complete["exact_text"])
        self.assertTrue(candidates[0].semantic_fingerprint.strip())
        events = self.inbox.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].metadata["origin"], "live_normal_page")
        detail.assert_called_once_with(complete)
        self.assertEqual(self.context.close_count, 1)
        self.assertEqual(self.final_run()["reason"], "completed")

    def test_consume_live_detail_rejects_empty_fingerprint_before_persisting(self):
        for fingerprint in ("", " \t ", None):
            with self.subTest(fingerprint=fingerprint):
                notice = notification_fixture(fingerprint=fingerprint)
                with self.assertRaises(ReceiverPageError):
                    runner.consume_detail(self.inbox, notice, detail_fixture(notice), [])
                self.assertEqual(self.inbox.list_candidates(), [])
                self.assertEqual(self.inbox.list_events(), [])

    def test_consume_replay_detail_preserves_empty_fingerprint_as_review_sample(self):
        notice = notification_fixture(fingerprint="")
        candidate = runner.consume_detail(self.inbox, notice, detail_fixture(notice), [], origin="sample_replay")
        self.assertEqual(candidate.status, "needs_review")
        self.assertEqual(len(self.inbox.list_candidates()), 1)
        events = self.inbox.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].metadata["origin"], "sample_replay")
        self.assertEqual(events[0].metadata["receiver_identity_binding"], "not_checked_by_replay")

    def test_manual_verification_keeps_page_without_refresh_or_notification_input(self):
        adapter = ScriptedAdapter(self.page, identities=[
            ReceiverInterventionRequired("fixture requires manual verification"),
            ReceiverInterventionRequired("fixture still requires verification"),
            ReceiverIdentity("123456", "助手样本"),
        ])
        self.run_receiver(adapter, max_cycles=3)
        self.assertEqual(adapter.identity_count, 3)
        self.assertEqual(adapter.notification_count, 1)
        self.assertEqual(adapter.diagnostic_count, 0)
        # The initial load precedes the challenge; neither paused cycle reloads.
        self.assertEqual(len(self.page.navigations), 1)
        rows = self.cycles()
        self.assertEqual([row["outcome"] for row in rows], ["paused", "paused", "success"])
        self.assertEqual([row["ui_state"]["navigation"] for row in rows], [True, False, False])
        self.assert_closed_once("completed")

    def test_targeted_stop_during_poll_wait_closes_context_and_finishes_stopped(self):
        requests = []
        self.page.on_wait = lambda: requests.append(request_stop(self.root))
        adapter = ScriptedAdapter(self.page)
        self.run_receiver(adapter, max_cycles=0)
        self.assertEqual(requests, [True])
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual(self.history.summary()["success_count"], 1)
        request = json.loads((self.root / "stop-request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["target_run_id"], self.history.run_id)
        self.assert_closed_once("stopped")

    def test_verification_appearing_during_timeout_prevents_recovery_refresh(self):
        adapter = ScriptedAdapter(self.page, identities=[
            ReceiverIdentity("123456", "助手样本"),
            ReceiverInterventionRequired("fixture verification remains visible"),
            ReceiverIdentity("123456", "助手样本"),
        ], notifications=[PageTimeout("fixture timeout"), []], interventions=[
            ReceiverInterventionRequired("fixture verification appeared while waiting"),
        ])
        self.run_receiver(adapter, max_cycles=3)
        self.assertEqual(adapter.intervention_check_count, 1)
        self.assertEqual(adapter.notification_count, 2)
        self.assertEqual(adapter.diagnostic_count, 0)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual([row["outcome"] for row in self.cycles()], ["paused", "paused", "success"])
        self.assert_closed_once("completed")

    def test_normal_window_close_during_wait_does_not_reopen_browser(self):
        self.page.on_wait = lambda: setattr(self.page, "closed", True)
        adapter = ScriptedAdapter(self.page)
        self.run_receiver(adapter, max_cycles=0)
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual([row["outcome"] for row in self.cycles()], ["success"])
        self.assert_closed_once("window_closed")

    def test_window_close_during_notification_input_finishes_without_reopening(self):
        adapter = ScriptedAdapter(self.page, notifications=["close-window"])
        self.run_receiver(adapter, max_cycles=0)
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual([(row["outcome"], row["stage"]) for row in self.cycles()],
                         [("paused", "shutdown")])
        self.assert_closed_once("window_closed")

    def test_window_close_during_post_timeout_verification_check_finishes_normally(self):
        adapter = ScriptedAdapter(self.page, notifications=[PageTimeout("fixture timeout")],
                                  interventions=["close-window"])
        self.run_receiver(adapter, max_cycles=0)
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual(adapter.intervention_check_count, 1)
        self.assertEqual(adapter.diagnostic_count, 0)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual([(row["outcome"], row["stage"]) for row in self.cycles()],
                         [("paused", "shutdown")])
        self.assert_closed_once("window_closed")


if __name__ == "__main__":
    unittest.main()
