"""Optional agent mode with fake browser/handler and temporary files only."""
from contextlib import ExitStack, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import pytest
from unittest.mock import Mock, patch

from app.mention_receiver import __main__ as runner
from app.mention_receiver.browser import ReceiverInterventionRequired, ReceiverPageError
from app.mention_receiver.core import SQLiteInbox
from app.mention_receiver.runtime import RunHistory, request_stop
from tests import test_mention_receiver_loop as fixtures


@pytest.fixture(autouse=True)
def isolated_legacy_authority(monkeypatch):
    # Synthetic old-mode tests must not depend on the user's live migration.
    monkeypatch.setattr(runner,'legacy_testing_blocked',lambda:False)


SENDER = "https://www.douyin.com/user/sender-fixture"
QUESTION = "@助手样本 我库里的机会成本是什么意思"


def question_notice(**overrides):
    return {**fixtures.notification_fixture(), "exact_text": QUESTION, **overrides}


class FakeHandler:
    def __init__(self, *, maximum=1, created=True, state="answer_ready"):
        self.maximum = maximum
        self.created = created
        self.created_count = 0
        self.considered = []
        self.handled = []
        self.state = state
        self.service = SimpleNamespace(store=object(), binding=object())

    @property
    def exhausted(self):
        return self.created_count >= self.maximum

    def accepts_notice(self, notice):
        self.considered.append(notice)
        return notice.get("source_profile_url") == SENDER and bool(notice.get("exact_text", "").strip())

    def handle_detail(self, notice, detail):
        if self.exhausted:
            raise AssertionError("model task limit exceeded")
        self.handled.append((notice, detail))
        if self.created:
            self.created_count += 1
        return {"created": self.created, "task_id": "task-fixture",
                "state": notice.get("fixture_task_state", self.state)}


class FakePublisher:
    def __init__(self, state="b_view_visible", before_submit=None, error=None):
        self.state, self.before_submit, self.error = state, before_submit, error
        self.calls = []
        self.submits = 0
        self.should_stop = None

    def publish(self, task_id):
        self.calls.append(task_id)
        if self.before_submit:
            self.before_submit()
        if self.should_stop():
            return {"state": "preflight_blocked", "attempted": True}
        if self.error:
            raise self.error
        if self.state in {"b_view_visible", "delivery_unknown"}:
            self.submits += 1
        return {"state": self.state, "attempted": True}


class AgentReceiverModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.inbox = SQLiteInbox(self.root / "preview.sqlite3")
        self.history = RunHistory(self.root, "123456")
        self.addCleanup(self.history.finish)
        self.clock = fixtures.FakeClock()
        self.page = fixtures.FakePage(self.clock)
        self.context = fixtures.FakeContext(self.page)
        self.playwright = fixtures.FakePlaywright(self.context)
        self.config = {"receiver_public_number": "123456",
                       "receiver_profile_url": "https://www.douyin.com/user/receiver-fixture"}
        self.libraries = [{"id": 1, "name": "测试知识库"}]

    def run_mode(self, notices, *, handler=None, max_cycles=1, detail_error=None,
                 auto_reply=False, publisher=None, duration_minutes=0, adapter=None):
        adapter = adapter or fixtures.ScriptedAdapter(self.page, notifications=[notices])
        publisher = publisher or FakePublisher()

        def construct(*args, **kwargs):
            publisher.should_stop = kwargs["should_stop"]
            return publisher

        self.publisher_factory = Mock(side_effect=construct)
        self.publisher = publisher
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                "app.mention_agent.publisher": SimpleNamespace(ReplyPublisher=self.publisher_factory)}))
            stack.enter_context(patch.object(runner, "STATE_DIR", self.root))
            stack.enter_context(patch.object(runner, "STORAGE", self.root))
            stack.enter_context(patch.object(runner.time, "monotonic", self.clock.monotonic))
            stack.enter_context(patch.object(runner, "NormalPageReceiver", return_value=adapter))
            stack.enter_context(patch.object(runner, "browser_options", return_value={}))
            stack.enter_context(patch("playwright.sync_api.sync_playwright", return_value=self.playwright))
            detail = stack.enter_context(patch.object(
                adapter, "detail", side_effect=detail_error or fixtures.detail_fixture, create=True))
            args = SimpleNamespace(duration_minutes=duration_minutes, poll_seconds=20,
                                   max_cycles=max_cycles, agent_auto_reply=auto_reply,
                                   agent_max_tasks=1, agent_state=self.root / "agent.sqlite3")
            runner.run_live(args, self.inbox, self.libraries, self.config,
                            self.root / "browser_b", self.history, agent_handler=handler)
        return adapter, detail

    def final_run(self):
        return json.loads((self.root / "active-run.json").read_text(encoding="utf-8"))

    def test_default_preview_still_ignores_plain_question(self):
        _, detail = self.run_mode([question_notice()])
        detail.assert_not_called()
        self.assertEqual(self.inbox.list_events(), [])
        self.assertEqual(self.inbox.list_candidates(), [])
        self.assertEqual(self.final_run()["reason"], "completed")

    def test_default_preview_still_parses_import_and_skips_terminal_candidate(self):
        value = fixtures.notification_fixture()
        candidate = self.inbox.observe_notification(SENDER, value["exact_text"], value["semantic_fingerprint"])
        self.inbox.complete_notification(candidate.notification_key, sender_profile_url=SENDER,
            video_id=fixtures.detail_fixture(value)["video_id"], command_text=value["exact_text"],
            libraries=self.libraries)
        self.assertEqual(self.inbox.list_events()[0].status, "received")
        before = self.inbox.list_candidates()
        _, detail = self.run_mode([value])
        detail.assert_not_called()
        self.assertEqual(self.inbox.list_candidates(), before)
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_bound_ordinary_question_goes_to_independent_handler_and_stops_at_limit(self):
        handler = FakeHandler()
        value = question_notice()
        _, detail = self.run_mode([value], handler=handler, max_cycles=8)
        detail.assert_called_once_with(value)
        self.assertEqual(handler.handled[0][0]["exact_text"], QUESTION)
        self.assertEqual(self.inbox.list_events(), [])
        self.assertEqual(self.inbox.list_candidates(), [])
        self.assertEqual(self.final_run()["reason"], "completed")
        self.assertEqual(self.history.summary()["cycle_count"], 1)
        self.assertEqual(self.context.close_count, 1)
        status = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "agent")
        self.assertIn("已达到本次智能任务上限", status["message"])
        page = (self.root / "接收测试台.html").read_text(encoding="utf-8")
        self.assertIn("已启用 DeepSeek 工具", page)
        self.assertIn("不会自动发评或导入", page)
        self.assertNotIn("回放验证：", page)

    def test_stranger_never_reaches_detail_or_handler(self):
        handler = FakeHandler()
        _, detail = self.run_mode([question_notice(source_profile_url="https://www.douyin.com/user/stranger")],
                                  handler=handler)
        detail.assert_not_called()
        self.assertEqual(handler.handled, [])
        self.assertEqual(self.inbox.list_candidates(), [])

    def test_old_review_preview_is_unchanged_but_fresh_detail_is_required(self):
        value = question_notice()
        candidate = self.inbox.observe_notification(SENDER, value["exact_text"], value["semantic_fingerprint"])
        self.inbox.complete_notification(candidate.notification_key, sender_profile_url=SENDER,
            video_id=fixtures.detail_fixture(value)["video_id"], command_text=value["exact_text"], libraries=[])
        before_candidates, before_events = self.inbox.list_candidates(), self.inbox.list_events()
        self.assertEqual(before_events[0].status, "needs_review")
        handler = FakeHandler()
        _, detail = self.run_mode([value], handler=handler)
        detail.assert_called_once_with(value)
        self.assertEqual(len(handler.handled), 1)
        self.assertEqual(self.inbox.list_candidates(), before_candidates)
        self.assertEqual(self.inbox.list_events(), before_events)

    def test_old_preview_alone_cannot_schedule_agent_task(self):
        value = fixtures.notification_fixture()
        candidate = self.inbox.observe_notification(SENDER, value["exact_text"], value["semantic_fingerprint"])
        self.inbox.complete_notification(candidate.notification_key, sender_profile_url=SENDER,
            video_id=fixtures.detail_fixture(value)["video_id"], command_text=value["exact_text"],
            libraries=self.libraries)
        handler = FakeHandler()
        _, detail = self.run_mode([], handler=handler)
        detail.assert_not_called()
        self.assertEqual(handler.handled, [])
        self.assertEqual(len(self.inbox.list_events()), 1)

    def test_unverified_fresh_detail_never_uses_old_preview_as_fallback(self):
        value = fixtures.notification_fixture()
        candidate = self.inbox.observe_notification(SENDER, value["exact_text"], value["semantic_fingerprint"])
        self.inbox.complete_notification(candidate.notification_key, sender_profile_url=SENDER,
            video_id=fixtures.detail_fixture(value)["video_id"], command_text=value["exact_text"],
            libraries=self.libraries)
        before = self.inbox.list_candidates()
        handler = FakeHandler()
        self.run_mode([value], handler=handler, detail_error=ReceiverPageError("fixture incomplete"))
        self.assertEqual(handler.handled, [])
        self.assertEqual(self.inbox.list_candidates(), before)

    def test_task_limit_is_checked_between_two_visible_notices(self):
        values = [question_notice(), question_notice(exact_text=QUESTION + "，再解释一下")]
        handler = FakeHandler(maximum=1)
        _, detail = self.run_mode(values, handler=handler, max_cycles=8)
        self.assertEqual(detail.call_count, 1)
        self.assertEqual(len(handler.handled), 1)
        self.assertEqual(handler.created_count, 1)
        self.assertEqual(self.final_run()["reason"], "completed")

    def test_duplicate_existing_task_does_not_consume_model_limit(self):
        handler = FakeHandler(created=False)
        self.run_mode([question_notice()], handler=handler)
        self.assertEqual(len(handler.handled), 1)
        self.assertFalse(handler.exhausted)
        self.assertEqual(self.final_run()["reason"], "completed")

    def test_default_agent_draft_never_constructs_publisher(self):
        self.run_mode([question_notice()], handler=FakeHandler())
        self.publisher_factory.assert_not_called()
        self.assertEqual(self.publisher.calls, [])

    def test_ready_reply_is_published_before_model_limit_break(self):
        handler = FakeHandler()
        adapter, _ = self.run_mode([question_notice()], handler=handler, auto_reply=True, max_cycles=8)
        self.assertTrue(handler.exhausted)
        self.assertEqual(self.publisher.calls, ["task-fixture"])
        self.assertEqual(self.publisher.submits, 1, "model exhaustion is not the stop callback")
        args, kwargs = self.publisher_factory.call_args
        self.assertIs(args[0], handler.service.store)
        self.assertIs(args[1], handler.service.binding)
        self.assertIs(args[2], adapter)
        self.assertEqual(args[3], self.root / "reply-evidence")
        self.assertTrue(callable(kwargs["should_stop"]))
        status = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["mode"], "agent_auto_reply")
        self.assertIn("B 回读原评论可见", status["message"])
        self.assertIn("A 或访客", status["message"])

    def test_restart_ready_task_can_publish_once_through_fresh_notice(self):
        handler = FakeHandler(created=False)
        values = [question_notice(), question_notice(exact_text=QUESTION + "，第二条")]
        _, detail = self.run_mode(values, handler=handler, auto_reply=True, max_cycles=8)
        detail.assert_called_once()
        self.assertEqual(handler.created_count, 0)
        self.assertEqual(self.publisher.calls, ["task-fixture"])
        self.assertEqual(self.publisher.submits, 1)
        self.assertEqual(self.history.summary()["cycle_count"], 1)

    def test_reserved_terminal_and_review_tasks_never_reach_publisher(self):
        states = ["send_reserved", "b_view_visible", "delivery_unknown",
                  "verification_required", "failed_no_submit", "needs_review", "queued"]
        values = [question_notice(exact_text=QUESTION + str(i), fixture_task_state=state)
                  for i, state in enumerate(states)]
        handler = FakeHandler(created=False)
        self.run_mode(values, handler=handler, auto_reply=True)
        self.assertEqual(len(handler.handled), len(states))
        self.assertEqual(self.publisher.calls, [])
        self.assertEqual(self.publisher.submits, 0)

    def test_user_stop_is_rechecked_at_pre_submit(self):
        publisher = FakePublisher(before_submit=lambda: request_stop(self.root))
        self.run_mode([question_notice()], handler=FakeHandler(), auto_reply=True, publisher=publisher)
        self.assertEqual(publisher.calls, ["task-fixture"])
        self.assertEqual(publisher.submits, 0)

    def test_duration_is_rechecked_at_pre_submit(self):
        publisher = FakePublisher(before_submit=lambda: self.clock.advance(61000))
        self.run_mode([question_notice()], handler=FakeHandler(), auto_reply=True,
                      publisher=publisher, duration_minutes=1)
        self.assertEqual(publisher.calls, ["task-fixture"])
        self.assertEqual(publisher.submits, 0)

    def assert_pause_stays_until_stop(self, state, *, error=None, detail_error=None):
        publisher = FakePublisher(state=state, error=error)
        observed = []
        adapter = fixtures.ScriptedAdapter(self.page, notifications=[[question_notice()]])

        def human_waiting():
            # max1 and max_cycles=1 must not close the verification scene.
            self.assertEqual(self.context.close_count, 0)
            observed.append((adapter.identity_count, adapter.notification_count, adapter.begin_count))

            def stop_on_next_wait():
                self.assertEqual(observed[0], (adapter.identity_count, adapter.notification_count, adapter.begin_count))
                self.assertEqual(self.context.close_count, 0)
                request_stop(self.root)

            self.page.on_wait = stop_on_next_wait

        self.page.on_wait = human_waiting
        self.run_mode([question_notice()], handler=FakeHandler(), auto_reply=True,
                      publisher=publisher, detail_error=detail_error, adapter=adapter)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual(adapter.diagnostic_count, 0)
        self.assertEqual(self.context.close_count, 1)
        self.assertEqual(self.final_run()["reason"], "stopped")
        status = json.loads((self.root / "status.json").read_text(encoding="utf-8"))
        self.assertIn("仍待本人核对", status["state"])
        self.assertIn("锁定暂停", status["message"])
        return publisher

    def test_verification_result_latches_pause_even_at_model_and_cycle_limits(self):
        publisher = self.assert_pause_stays_until_stop("verification_required")
        self.assertEqual(publisher.calls, ["task-fixture"])
        self.assertEqual(publisher.submits, 0)

    def test_uncertain_result_latches_pause_without_another_send(self):
        publisher = self.assert_pause_stays_until_stop("delivery_unknown")
        self.assertEqual(publisher.calls, ["task-fixture"])
        self.assertEqual(publisher.submits, 1)

    def test_verification_before_publisher_also_latches_without_more_page_actions(self):
        publisher = self.assert_pause_stays_until_stop(
            "verification_required", detail_error=ReceiverInterventionRequired("fixture verification"))
        self.assertEqual(publisher.calls, [])

    def test_unexpected_publisher_exception_is_not_retried_or_exported(self):
        publisher = self.assert_pause_stays_until_stop(
            "delivery_unknown", error=RuntimeError("fixture-private-error-not-for-dashboard"))
        self.assertEqual(publisher.calls, ["task-fixture"])
        self.assertNotIn("fixture-private-error-not-for-dashboard",
                         (self.root / "status.json").read_text(encoding="utf-8"))

    def test_paused_scene_can_close_normally_at_duration_deadline(self):
        publisher = FakePublisher("verification_required")

        def expire_during_pause():
            self.assertEqual(self.context.close_count, 0)
            self.clock.advance(61000)

        self.page.on_wait = expire_during_pause
        adapter, _ = self.run_mode([question_notice()], handler=FakeHandler(), auto_reply=True,
                                   publisher=publisher, duration_minutes=1)
        self.assertEqual(self.final_run()["reason"], "duration_elapsed")
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual(self.context.close_count, 1)

    def test_user_can_close_a_latched_verification_window_without_reopening(self):
        publisher = FakePublisher("verification_required")
        self.page.on_wait = lambda: setattr(self.page, "closed", True)
        adapter, _ = self.run_mode([question_notice()], handler=FakeHandler(), auto_reply=True,
                                   publisher=publisher)
        self.assertEqual(self.final_run()["reason"], "window_closed")
        self.assertEqual(adapter.begin_count, 1)
        self.assertEqual(len(self.page.navigations), 1)
        self.assertEqual(len(self.playwright.launches), 1)
        self.assertEqual(self.context.close_count, 1)

    def test_blocked_preflight_spends_only_one_publisher_attempt(self):
        publisher = FakePublisher("preflight_blocked")
        self.run_mode([question_notice(), question_notice(exact_text=QUESTION + "第二条")],
                      handler=FakeHandler(created=False), auto_reply=True,
                      publisher=publisher, max_cycles=8)
        self.assertEqual(publisher.calls, ["task-fixture"])
        self.assertEqual(publisher.submits, 0)


class AgentCLIHookTests(unittest.TestCase):
    def test_explicit_agent_flags_build_handler_without_starting_application(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "business.db").touch()
            handler = FakeHandler()
            factory = Mock(return_value=handler)
            preview = SimpleNamespace(quarantine_incomplete_candidates=Mock())
            settings = SimpleNamespace(database_url="sqlite:///business.db",
                                       playwright_user_data_dir=str(root / "original"))
            config = {"receiver_public_number": "123456",
                      "receiver_profile_url": "https://www.douyin.com/user/receiver-fixture"}
            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {
                    "app.core.config": SimpleNamespace(settings=settings),
                    "app.mention_agent.service": SimpleNamespace(build_receiver_handler=factory)}))
                stack.enter_context(patch.object(runner, "STATE_DIR", root / "receiver"))
                stack.enter_context(patch.object(runner, "STORAGE", root))
                stack.enter_context(patch.object(runner, "BACKEND", root))
                stack.enter_context(patch.object(runner, "configuration", return_value=config))
                stack.enter_context(patch.object(runner, "SQLiteInbox", return_value=preview))
                libraries = stack.enter_context(patch.object(runner, "read_libraries", side_effect=AssertionError("agent does not read preview catalogue")))
                live = stack.enter_context(patch.object(runner, "run_live"))
                code = runner.main(["--agent-binding", str(root / "binding.json"),
                                    "--agent-marker", " DM-1021-AGENT-01 ",
                                    "--agent-state", str(root / "agent.sqlite3"), "--agent-max-tasks", "3"])
            self.assertEqual(code, 0)
            factory.assert_called_once_with(binding_path=root / "binding.json",
                                            state_path=root / "agent.sqlite3", max_tasks=3,
                                            required_marker="DM-1021-AGENT-01")
            self.assertIs(live.call_args.kwargs["agent_handler"], handler)
            self.assertIs(live.call_args.args[0].agent_auto_reply, False)
            preview.quarantine_incomplete_candidates.assert_not_called()
            libraries.assert_not_called()

    def test_agent_cannot_promote_old_replay_mode(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            runner.main(["--agent-binding", "fixture.json", "--replay", "saved-preview.json"])
        self.assertEqual(error.exception.code, 2)

    def test_zero_agent_limit_is_rejected_before_service_or_browser(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            runner.main(["--agent-binding", "fixture.json", "--agent-max-tasks", "0"])
        self.assertEqual(error.exception.code, 2)

    def test_agent_requires_explicit_nonempty_marker_before_service_or_browser(self):
        for marker in (None, "", " \t ", "x" * 81):
            arguments = ["--agent-binding", "fixture.json"]
            if marker is not None:
                arguments += ["--agent-marker", marker]
            with self.subTest(marker=marker), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                runner.main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_auto_reply_requires_agent_binding_marker_and_maximum_one(self):
        invalid = [
            ["--agent-auto-reply"],
            ["--agent-auto-reply", "--agent-marker", "DM-AUTO-FIXTURE"],
            ["--agent-auto-reply", "--agent-binding", "fixture.json"],
            ["--agent-auto-reply", "--agent-binding", "fixture.json",
             "--agent-marker", "DM-AUTO-FIXTURE", "--agent-max-tasks", "2"],
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                runner.main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_default_preview_needs_no_agent_marker_or_model_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "business.db").touch()
            factory = Mock(side_effect=AssertionError("preview must not build a model handler"))
            preview = SimpleNamespace(quarantine_incomplete_candidates=Mock())
            settings = SimpleNamespace(database_url="sqlite:///business.db",
                                       playwright_user_data_dir=str(root / "original"))
            config = {"receiver_public_number": "123456",
                      "receiver_profile_url": "https://www.douyin.com/user/receiver-fixture"}
            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {
                    "app.core.config": SimpleNamespace(settings=settings),
                    "app.mention_agent.service": SimpleNamespace(build_receiver_handler=factory)}))
                stack.enter_context(patch.object(runner, "STATE_DIR", root / "receiver"))
                stack.enter_context(patch.object(runner, "STORAGE", root))
                stack.enter_context(patch.object(runner, "BACKEND", root))
                stack.enter_context(patch.object(runner, "configuration", return_value=config))
                stack.enter_context(patch.object(runner, "SQLiteInbox", return_value=preview))
                libraries = stack.enter_context(patch.object(runner, "read_libraries", return_value=[]))
                live = stack.enter_context(patch.object(runner, "run_live"))
                code = runner.main([])
            self.assertEqual(code, 0)
            self.assertIsNone(live.call_args.kwargs["agent_handler"])
            preview.quarantine_incomplete_candidates.assert_called_once()
            libraries.assert_called_once()
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
