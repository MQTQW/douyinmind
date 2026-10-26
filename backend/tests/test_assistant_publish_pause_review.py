"""Real Bridge/Publisher + fake UI/model in preserved temporary ledgers."""
import json
from pathlib import Path
import tempfile

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from app.assistant.queue import AssistantQueue, QueueError
from app.mention_agent.publisher import ReplyPublisher
from app.mention_agent.store import AgentStore
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel
from tests.test_mention_agent_import_end_to_end import Adapter


def prepared_case(adapter_factory):
    root = Path(tempfile.mkdtemp(prefix="dm-publish-pause-review-")).resolve()
    binding, agent, envelope, registry, migrated, queue, history, control = system(root)
    task = receive(queue, history, registry, {**envelope, "comment_text": "@助手 请根据编程知识库说明Git。"})
    model = QueryModel()
    adapter = adapter_factory(control)
    bridge = LegacyBridge(registry, queue, control, agent_path=root / "agent.sqlite3", conversation=history,
        database=root / "never-open-business.sqlite3", model_factory=lambda: model,
        knowledge_factory=lambda _: QueryKnowledge(), adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish,
                               reconcile=bridge.reconcile, resume_adapter=bridge.operator_resume)
    ready = runtime.execute_once()
    assert ready["state"] == "ready_to_publish" and len(model.requests) == 2
    return root, agent, queue, control, adapter, model, bridge, runtime, ready


class PauseDuringPrepare(Adapter):
    def __init__(self, control):
        super().__init__(); self.control = control; self.cleared = 0; self.own_draft = None
        self.prepares = 0; self.pause_once = True
    def prepare_reply(self, target, text):
        self.prepares += 1
        verification = super().prepare_reply(target, text)
        self.own_draft = (target, text)
        if self.pause_once:
            self.control.desired = "paused"
            self.pause_once = False
        return verification
    def clear_prepared_reply(self, target, text):
        assert self.own_draft == (target, text)
        self.own_draft = None; self.cleared += 1


def test_pause_during_prepare_cannot_reserve_or_click_a_new_publication():
    root, agent, queue, control, adapter, model, bridge, runtime, ready = prepared_case(PauseDuringPrepare)
    outcome = runtime.publish_once()
    reply = agent.get_reply(ready["agent_task_id"])
    manifest = {"fixture": str(root), "control_desired": control.desired, "fake_ui_clicks": adapter.submits,
        "draft_cleared": adapter.cleared, "send_token_present": bool(reply["send_token"]),
        "reply_state": reply["state"], "queue_state": outcome["state"], "model_calls": len(model.requests),
        "business_file_exists": (root / "never-open-business.sqlite3").exists()}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    print("preserved-pause-fixture=" + str(root))
    assert adapter.submits == 0, "paused prepare reached a new click; see " + str(root)
    assert reply["send_token"] is None
    assert adapter.cleared == 1 and adapter.own_draft is None
    assert agent.get_task(ready["agent_task_id"])["state"] == "answer_ready"
    assert reply["state"] == "ready"
    assert outcome["state"] == "needs_review" and outcome["reason_code"] == "publication_paused"


class TrackingAdapter(PauseDuringPrepare):
    def __init__(self, control):
        super().__init__(control); self.pause_once = False


def assert_reserved_never_retried(case, expected_clicks):
    root, agent, queue, control, adapter, model, bridge, runtime, ready = case
    task_id = ready["agent_task_id"]
    token = agent.get_reply(task_id)["send_token"]
    assert token is not None
    control.desired = "running"
    runtime.reconcile_task(ready["id"])
    with pytest.raises(QueueError, match="resume_not_safe"):
        runtime.resume_task(ready["id"])
    assert runtime.publish_once() is None
    assert bridge.publish(queue.get_task(ready["id"]))["safe_to_publish"] is False
    # Reopening both ledgers must not turn a retained reservation into a new send.
    fresh = AgentStore(agent.path)
    assert fresh.get_reply(task_id)["send_token"] == token
    assert AssistantQueue(queue.path).claim_next("publish") is None
    assert ReplyPublisher(fresh, bridge._binding(ready), adapter, root / "reply-evidence").publish(task_id)["state"] == "not_sendable"
    assert adapter.submits == expected_clicks and len(model.requests) == 2


def test_pause_just_before_prepare_does_not_open_or_reserve():
    case = prepared_case(TrackingAdapter)
    root, agent, queue, control, adapter, model, bridge, runtime, ready = case
    def pause_then_publish(task):
        control.desired = "paused"
        return bridge.publish(task)
    runtime.publish_adapter = pause_then_publish
    result = runtime.publish_once()
    assert result["state"] == "needs_review" and result["reason_code"] == "publication_paused"
    assert adapter.prepares == adapter.submits == adapter.cleared == 0
    assert agent.get_reply(ready["agent_task_id"])["send_token"] is None


def test_start_alone_does_not_send_paused_draft_but_explicit_resume_uses_same_model_result():
    root, agent, queue, control, adapter, model, bridge, runtime, ready = prepared_case(PauseDuringPrepare)
    assert runtime.publish_once()["state"] == "needs_review"
    proposal = agent.get_reply(ready["agent_task_id"])["text"]
    control.desired = "running"
    assert runtime.publish_once() is None
    checked = runtime.reconcile_task(ready["id"])
    assert checked["state"] == "needs_review" and queue.public_task(checked)["actions"]["can_resume"] is True
    assert runtime.resume_task(ready["id"])["state"] == "ready_to_publish"
    assert runtime.publish_once()["state"] == "b_view_visible"
    assert adapter.prepares == 2 and adapter.cleared == 1 and adapter.submits == 1
    assert agent.get_reply(ready["agent_task_id"])["text"] == proposal
    assert len(model.requests) == 2 and runtime.execute_once() is None and runtime.publish_once() is None


class PauseBeforeGate(TrackingAdapter):
    def submit_reply(self, target, text, *, before_submit):
        self.control.desired = "paused"
        return super().submit_reply(target, text, before_submit=before_submit)


def test_pause_after_reservation_before_submit_keeps_token_and_cannot_resume():
    case = prepared_case(PauseBeforeGate)
    root, agent, queue, control, adapter, model, bridge, runtime, ready = case
    result = runtime.publish_once()
    assert result["state"] == "failed_no_submit" and adapter.submits == 0
    assert agent.get_reply(ready["agent_task_id"])["state"] == "failed_no_submit"
    assert_reserved_never_retried(case, 0)


def test_pause_while_last_reserved_token_read_waits_still_blocks_the_click(monkeypatch):
    case = prepared_case(TrackingAdapter)
    root, agent, queue, control, adapter, model, bridge, runtime, ready = case
    original = bridge.agent.get_reply
    observed_reserved = False
    def read_then_pause(task_id):
        nonlocal observed_reserved
        result = original(task_id)
        if result and result["state"] == "reserved" and not observed_reserved:
            observed_reserved = True
            control.desired = "paused"
        return result
    monkeypatch.setattr(bridge.agent, "get_reply", read_then_pause)
    result = runtime.publish_once()
    print("preserved-last-read-pause-fixture=" + str(root))
    assert observed_reserved
    assert adapter.submits == 0, "pause during the last durable-token read passed the click gate"
    assert result["state"] == "failed_no_submit"
    assert_reserved_never_retried(case, 0)


class PauseAfterClick(TrackingAdapter):
    failure = TimeoutError
    def submit_reply(self, target, text, *, before_submit):
        before_submit(); self.submits += 1
        self.control.desired = "paused"
        raise self.failure("synthetic submit outcome unavailable")


class PauseThenVerification(PauseAfterClick):
    failure = ReceiverInterventionRequired


@pytest.mark.parametrize("adapter_type,state", [
    (PauseAfterClick, "delivery_unknown"), (PauseThenVerification, "verification_required")])
def test_pause_after_click_cannot_make_unknown_or_verification_retryable(adapter_type, state):
    case = prepared_case(adapter_type)
    root, agent, queue, control, adapter, model, bridge, runtime, ready = case
    assert runtime.publish_once()["state"] == state
    assert_reserved_never_retried(case, 1)


def test_running_positive_path_still_publishes_once_and_reuses_existing_answer():
    case = prepared_case(TrackingAdapter)
    root, agent, queue, control, adapter, model, bridge, runtime, ready = case
    assert runtime.publish_once()["state"] == "b_view_visible"
    assert adapter.prepares == 1 and adapter.cleared == 0
    assert_reserved_never_retried(case, 1)


def test_pause_after_preflight_evidence_before_reservation_clears_exact_draft(monkeypatch):
    root, agent, queue, control, adapter, model, bridge, runtime, ready = prepared_case(TrackingAdapter)
    original = ReplyPublisher._write
    def write_then_pause(path, name, value):
        original(path, name, value)
        if name == "preflight.json":
            control.desired = "paused"
    monkeypatch.setattr(ReplyPublisher, "_write", staticmethod(write_then_pause))
    assert runtime.publish_once()["state"] == "needs_review"
    assert adapter.cleared == 1 and adapter.own_draft is None and adapter.submits == 0
    assert agent.get_reply(ready["agent_task_id"])["send_token"] is None


@pytest.mark.parametrize("bad_gate", [False, True, 1, "running"])
def test_publication_gate_requires_a_callable(bad_gate):
    with pytest.raises(ValueError, match="invalid_publication_gate"):
        ReplyPublisher(None, None, None, Path("unused"), publication_allowed=bad_gate)


@pytest.mark.parametrize("fail_on,expected_state,has_token", [
    (1, "preflight_blocked", False), (4, "failed_no_submit", True)])
def test_publication_gate_errors_fail_closed_without_erasing_reservations(fail_on, expected_state, has_token):
    root, agent, queue, control, adapter, model, bridge, runtime, ready = prepared_case(TrackingAdapter)
    calls = 0
    def broken_gate():
        nonlocal calls
        calls += 1
        if calls == fail_on:
            raise RuntimeError("synthetic private diagnostics must not escape")
        return True
    publisher = ReplyPublisher(agent, bridge._binding(ready), adapter, root / "reply-evidence",
        publication_allowed=broken_gate)
    result = publisher.publish(ready["agent_task_id"])
    assert result["state"] == expected_state and adapter.submits == 0
    reply = agent.get_reply(ready["agent_task_id"])
    assert bool(reply["send_token"]) is has_token
    assert "private diagnostics" not in json.dumps(result)
    if has_token:
        assert publisher.publish(ready["agent_task_id"])["state"] == "not_sendable"
