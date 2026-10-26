"""Publication barriers using a real temporary ledger and a fake UI adapter.

These checks do not start a browser, call a model, connect to a platform or use
the business knowledge database. A fake click is only an in-memory counter.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.mention_agent.contracts import Binding
from app.mention_agent.publisher import ReplyPreflightError, ReplyPublisher
from app.mention_agent.store import AgentStore
from app.mention_receiver.browser import ReceiverInterventionRequired


SENDER = "https://www.douyin.com/user/publisher-fictional-A"
RECEIVER = "https://www.douyin.com/user/publisher-fictional-B"


def utc():
    return datetime.now(timezone.utc).isoformat()


def binding_value(**changes):
    return {"binding_id": "publisher-fixture", "instance_id": "fixture-pc",
            "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
            "knowledge_base_id": 7, "public_reply_allowed": True, "active": True, **changes}


def envelope_value(**changes):
    return {"command_id": "publisher-command", "reference_id": "publisher-reference",
            "reference_quality": "native", "native_comment_id": "7999999000000000002",
            "sender_profile_url": SENDER, "receiver_profile_url": RECEIVER,
            "video_id": "7999999000000000001", "comment_text": "@虚构B 请根据测试库回答问题。",
            "source_mode": "live_ui", "evidence": {"real_mention": True, "unique_count": 1,
                "sender_verified": True, "video_verified": True, "observed_at_utc": utc()}, **changes}


def proposal_value():
    return {"text": "虚构正文说明了测试步骤。\n[来源：资料1]", "source_ids": ["资料1"], "kind": "answer",
            "sources": [{"source_id": "资料1", "platform_item_id": "7999999000000000003",
                         "chunk_id": "fictional-chunk", "title": "虚构来源",
                         "url": "https://www.douyin.com/video/7999999000000000003"}]}


class FakeAdapter:
    def __init__(self, *, behavior="visible", prepare_error=None, prepare_hook=None,
                 before_gate_hook=None, verification_changes=None, screenshot_error=False):
        self.behavior = behavior
        self.prepare_error = prepare_error
        self.prepare_hook = prepare_hook
        self.before_gate_hook = before_gate_hook
        self.verification_changes = verification_changes or {}
        self.screenshot_error = screenshot_error
        self.prepares = self.submit_entries = self.gates = self.clicks = self.screenshots = 0
        self.observed_target = self.observed_text = None

    def prepare_reply(self, target, text):
        self.prepares += 1
        self.observed_target, self.observed_text = deepcopy(target), text
        if self.prepare_hook:
            self.prepare_hook()
        if self.prepare_error:
            raise self.prepare_error
        proof = {key: target[key] for key in ("sender_profile_url", "receiver_profile_url", "video_id",
                                             "comment_text", "reference_id", "native_comment_id")}
        proof.update(unique_count=1, real_mention=True, current_receiver_verified=True, observed_at_utc=utc())
        proof.update(self.verification_changes)
        return proof

    def submit_reply(self, target, text, *, before_submit):
        self.submit_entries += 1
        assert target == self.observed_target
        assert text == self.observed_text
        if self.behavior == "before_error":
            raise RuntimeError("synthetic pre-click failure")
        if self.behavior == "challenge_before_gate":
            raise ReceiverInterventionRequired("synthetic verification, no screenshot")
        if self.behavior == "success_without_gate":
            return {"outcome": "b_view_visible", "exact_body_in_original_thread": True}
        if self.before_gate_hook:
            self.before_gate_hook()
        self.gates += 1
        before_submit()
        if self.behavior == "after_gate_error_before_click":
            raise RuntimeError("synthetic failure after permit")
        self.clicks += 1
        if self.behavior == "click_timeout":
            raise TimeoutError("synthetic click may already have succeeded")
        if self.behavior == "challenge_after_click":
            raise ReceiverInterventionRequired("synthetic human verification")
        if self.behavior == "gate_twice":
            self.gates += 1
            before_submit()
            self.clicks += 1
        if self.behavior == "success_without_exact":
            return {"outcome": "b_view_visible"}
        if self.behavior == "success_exact_false":
            return {"outcome": "b_view_visible", "exact_body_in_original_thread": False}
        if self.behavior == "reports_no_submit_after_click":
            return {"outcome": "failed_no_submit"}
        if self.behavior == "invalid_observation":
            return None
        if self.behavior == "unknown_outcome":
            return {"outcome": "posted_somewhere"}
        return {"outcome": "b_view_visible", "exact_body_in_original_thread": True}

    def save_reply_evidence(self, target, text, path):
        self.screenshots += 1
        if self.screenshot_error:
            raise RuntimeError("synthetic screenshot failure")


@pytest.fixture
def make_context(tmp_path):
    sequence = 0

    def make(*, source_mode="live_ui", public_reply_allowed=True, adapter=None, should_stop=None):
        nonlocal sequence
        sequence += 1
        root = tmp_path / f"case-{sequence}"
        store = AgentStore(root / "agent.sqlite3")
        binding = Binding.from_dict(binding_value(public_reply_allowed=public_reply_allowed))
        store.put_binding(binding.to_dict())
        task = store.enqueue(binding.binding_id, envelope_value(source_mode=source_mode))
        claim = store.claim(task["task_id"])
        assert claim is not None
        reply = store.complete(task["task_id"], claim["claim_token"], proposal_value())
        adapter = adapter or FakeAdapter()
        publisher = ReplyPublisher(store, binding, adapter, root / "evidence", should_stop=should_stop)
        return SimpleNamespace(store=store, binding=binding, task_id=task["task_id"], reply=reply,
                               adapter=adapter, publisher=publisher, root=root)

    return make


def assert_no_restart_submission(context, expected_clicks):
    fresh_store = AgentStore(context.store.path)
    fresh_adapter = FakeAdapter()
    publisher = ReplyPublisher(fresh_store, context.binding, fresh_adapter, context.root / "evidence")
    result = publisher.publish(context.task_id)
    assert result["state"] == "not_sendable"
    assert fresh_adapter.prepares == fresh_adapter.gates == fresh_adapter.clicks == 0
    assert context.adapter.clicks == expected_clicks


def test_one_durable_reservation_one_gate_one_click_and_no_repeat_after_restart(make_context, monkeypatch):
    context = make_context()
    original = context.store.reserve_send
    reservations = []

    def reserve(*args, **kwargs):
        result = original(*args, **kwargs)
        reservations.append(result)
        return result

    monkeypatch.setattr(context.store, "reserve_send", reserve)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "b_view_visible"
    assert len(reservations) == 1 and reservations[0]["state"] == "reserved"
    assert context.adapter.gates == context.adapter.clicks == 1
    assert context.store.get_task(context.task_id)["state"] == "b_view_visible"
    assert context.publisher.publish(context.task_id)["state"] == "not_sendable"
    assert context.adapter.prepares == context.adapter.submit_entries == context.adapter.clicks == 1
    token = reservations[0]["send_token"]
    diagnostics = json.dumps(result, ensure_ascii=False) + "".join(
        path.read_text(encoding="utf-8") for path in context.root.glob("evidence/**/*.json"))
    assert token not in diagnostics
    assert_no_restart_submission(context, 1)


@pytest.mark.parametrize("behavior,clicks", [
    ("click_timeout", 1), ("after_gate_error_before_click", 0),
    ("reports_no_submit_after_click", 1), ("invalid_observation", 1),
    ("unknown_outcome", 1), ("gate_twice", 1),
])
def test_gate_or_click_uncertainty_stays_unknown_and_is_never_retried(make_context, behavior, clicks):
    context = make_context(adapter=FakeAdapter(behavior=behavior))
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "delivery_unknown"
    assert context.store.get_reply(context.task_id)["state"] == "delivery_unknown"
    assert context.adapter.clicks == clicks
    assert context.adapter.screenshots == 0
    assert_no_restart_submission(context, clicks)


def test_exception_before_gate_records_no_submit_without_any_click(make_context):
    context = make_context(adapter=FakeAdapter(behavior="before_error"))
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "failed_no_submit"
    assert context.adapter.gates == context.adapter.clicks == 0
    assert context.store.get_reply(context.task_id)["state"] == "failed_no_submit"
    assert_no_restart_submission(context, 0)


def test_verification_during_prepare_holds_draft_without_reservation_or_screenshot(make_context):
    adapter = FakeAdapter(prepare_error=ReceiverInterventionRequired("synthetic verification"))
    context = make_context(adapter=adapter)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "verification_required" and result["submitted"] is False
    stored = context.store.get_reply(context.task_id)
    assert stored["state"] == "needs_review" and stored["send_token"] is None
    assert adapter.submit_entries == adapter.gates == adapter.clicks == adapter.screenshots == 0
    assert_no_restart_submission(context, 0)


@pytest.mark.parametrize("behavior,clicks", [("challenge_before_gate", 0), ("challenge_after_click", 1)])
def test_verification_before_or_after_submit_is_terminal_without_automatic_resend(make_context, behavior, clicks):
    context = make_context(adapter=FakeAdapter(behavior=behavior))
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "verification_required"
    assert context.store.get_reply(context.task_id)["state"] == "verification_required"
    assert context.adapter.screenshots == 0
    assert_no_restart_submission(context, clicks)


@pytest.mark.parametrize("source_mode,allowed", [("replay", True), ("isolated", True), ("live_ui", False)])
def test_nonlive_or_publicly_unauthorized_draft_never_reaches_adapter(make_context, source_mode, allowed):
    context = make_context(source_mode=source_mode, public_reply_allowed=allowed)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "preflight_blocked"
    assert context.adapter.prepares == context.adapter.submit_entries == context.adapter.clicks == 0
    assert context.store.get_reply(context.task_id)["send_token"] is None


def test_revocation_after_prepare_prevents_reservation_and_submission(make_context):
    context = make_context()
    context.adapter.prepare_hook = lambda: context.store.deactivate(context.binding.binding_id)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "preflight_blocked"
    assert context.adapter.submit_entries == context.adapter.clicks == 0
    assert context.store.get_reply(context.task_id)["send_token"] is None


def test_revocation_at_last_gate_prevents_click_and_leaves_nonretryable_result(make_context):
    context = make_context()
    context.adapter.before_gate_hook = lambda: context.store.deactivate(context.binding.binding_id)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "failed_no_submit"
    assert context.adapter.gates == 1 and context.adapter.clicks == 0
    assert_no_restart_submission(context, 0)


def test_existing_matching_reply_in_original_parent_is_held_not_sent_again(make_context):
    context = make_context(adapter=FakeAdapter(prepare_error=ReplyPreflightError("existing_matching_reply")))
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "preflight_blocked" and result["reason_code"] == "existing_matching_reply"
    assert context.store.get_task(context.task_id)["state"] == "needs_review"
    assert context.adapter.submit_entries == context.adapter.clicks == 0
    assert_no_restart_submission(context, 0)


@pytest.mark.parametrize("behavior", ["success_without_gate", "success_without_exact", "success_exact_false"])
def test_adapter_cannot_report_visible_success_without_gate_and_exact_readback(make_context, behavior):
    context = make_context(adapter=FakeAdapter(behavior=behavior))
    result = context.publisher.publish(context.task_id)
    assert result["state"] != "b_view_visible"
    assert context.adapter.screenshots == 0
    assert_no_restart_submission(context, context.adapter.clicks)


@pytest.mark.parametrize("stop_stage", ["initial", "after_prepare", "before_gate"])
def test_stop_before_gate_prevents_every_click(make_context, stop_stage):
    state = {"stopped": stop_stage == "initial"}
    context = make_context(should_stop=lambda: state["stopped"])
    stop = lambda: state.update(stopped=True)
    if stop_stage == "after_prepare":
        context.adapter.prepare_hook = stop
    elif stop_stage == "before_gate":
        context.adapter.before_gate_hook = stop
    result = context.publisher.publish(context.task_id)
    assert result["state"] != "b_view_visible"
    assert context.adapter.clicks == 0
    if stop_stage == "initial":
        assert context.adapter.prepares == 0
    if stop_stage == "before_gate":
        assert_no_restart_submission(context, 0)


def test_terminal_result_commit_failure_keeps_reservation_across_restart(make_context, monkeypatch):
    context = make_context()

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic result disk failure")

    monkeypatch.setattr(context.store, "finish_send", fail)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "delivery_unknown"
    assert result["reason_code"] == "result_record_failed_reservation_retained"
    assert context.store.get_reply(context.task_id)["state"] == "reserved"
    assert context.store.get_task(context.task_id)["state"] == "send_reserved"
    assert context.adapter.clicks == 1 and context.adapter.screenshots == 0
    assert context.task_id in AgentStore(context.store.path).recovery_snapshot()["reserved_unconfirmed_task_ids"]
    assert_no_restart_submission(context, 1)


def test_screenshot_failure_cannot_reverse_success_or_cause_a_second_click(make_context):
    context = make_context(adapter=FakeAdapter(screenshot_error=True))
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "b_view_visible"
    assert context.adapter.clicks == context.adapter.screenshots == 1
    assert_no_restart_submission(context, 1)


def test_optional_result_json_failure_does_not_undo_durable_outcome(make_context, monkeypatch):
    context = make_context()
    original = context.publisher._write

    def fail_result(path, name, value):
        if name == "result.json":
            raise OSError("synthetic optional evidence write failure")
        return original(path, name, value)

    monkeypatch.setattr(context.publisher, "_write", fail_result)
    assert context.publisher.publish(context.task_id)["state"] == "b_view_visible"
    assert context.store.get_reply(context.task_id)["state"] == "b_view_visible"
    assert_no_restart_submission(context, 1)


def test_preflight_evidence_write_failure_never_reserves_or_submits(make_context, monkeypatch):
    context = make_context()

    def fail(*args, **kwargs):
        raise OSError("synthetic preflight evidence disk failure")

    monkeypatch.setattr(context.publisher, "_write", fail)
    result = context.publisher.publish(context.task_id)
    assert result["state"] == "preflight_blocked"
    assert context.store.get_reply(context.task_id)["send_token"] is None
    assert context.adapter.submit_entries == context.adapter.clicks == 0


def test_concurrent_publishers_get_only_one_successful_reservation_and_click(make_context):
    context = make_context()
    gate = Barrier(2)
    first = FakeAdapter(prepare_hook=lambda: gate.wait(timeout=5))
    second = FakeAdapter(prepare_hook=lambda: gate.wait(timeout=5))
    publishers = [ReplyPublisher(AgentStore(context.store.path), context.binding, adapter, context.root / "evidence")
                  for adapter in (first, second)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda publisher: publisher.publish(context.task_id), publishers))
    assert sum(outcome["state"] == "b_view_visible" for outcome in outcomes) == 1
    assert first.clicks + second.clicks == first.gates + second.gates == 1
    assert context.store.get_reply(context.task_id)["state"] == "b_view_visible"
