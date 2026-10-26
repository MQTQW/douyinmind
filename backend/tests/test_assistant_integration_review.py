"""Independent regression probes: temp ledgers, fake model/backend/UI only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import os
import time
from types import SimpleNamespace

import pytest

from app.assistant.browser_lane import BrowserLane, notice_fingerprint
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from app.assistant.queue import AssistantQueue, QueueError
from app.mention_agent.store import AgentStoreError
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_product_bridge import system, receive, CaptureAdapter
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel
from tests.test_mention_agent_import_end_to_end import Backend, Model, Adapter
from tests.test_mention_agent_knowledge import FakeRag, hit


def focused_database(tmp_path, *, focus_status="done"):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db.base import Base
    from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
    path = tmp_path / "focused-synthetic-business.sqlite3"
    focus, other = "7999999000000000011", "7999999000000000022"
    engine = create_engine("sqlite:///" + str(path))
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(KnowledgeBase(id=7, name="合成测试库", name_key="合成测试库"))
            db.add_all([VideoCache(platform_item_id=focus, title="当前合成视频", status=focus_status),
                        VideoCache(platform_item_id=other, title="同库其他视频", status="done")])
            db.flush()
            db.add_all([KnowledgeBaseVideo(knowledge_base_id=7, platform_item_id=focus),
                        KnowledgeBaseVideo(knowledge_base_id=7, platform_item_id=other)])
            db.commit()
    finally:
        engine.dispose()
    return path, focus, other


def test_whitespace_alias_admission_remembers_fresh_notice_even_if_queue_returns_old_envelope(tmp_path):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    adapter = CaptureAdapter(binding)
    lane = BrowserLane(adapter, registry, queue, history)
    assert lane.collect() == []  # First verified empty snapshot establishes history baseline.
    first_notice = {"source_profile_url": binding.sender_profile_url, "exact_text": "@助手 解释Git",
                    "semantic_fingerprint": "synthetic-original-cover-path"}
    adapter.notices = [first_notice]
    initial = lane.collect()
    first_task = queue.enqueue(**initial[0])
    lane.admitted([first_task])
    # A refreshed cover path and a whitespace-only rendering change must not
    # create new work or leave capture permanently reopening the old command.
    refreshed = {**first_notice, "exact_text": "@助手 解释 Git", "semantic_fingerprint": "synthetic-refreshed-cover-path"}
    adapter.notices = [refreshed]
    aliases = lane.collect()
    assert len(aliases) == 1
    assert aliases[0]["envelope"]["command_id"] != first_task["envelope"]["command_id"]
    same_task = queue.enqueue(**aliases[0])
    assert same_task["id"] == first_task["id"]
    lane.admitted([same_task])
    assert queue.has_seen_notice(lane.capture_id, notice_fingerprint(refreshed))
    assert lane.pending == {}
    assert lane.collect() == []
    assert queue.list_tasks()["total"] == 1


def test_transient_receipt_get_before_publish_returns_to_readonly_wait_without_paid_replay(tmp_path, monkeypatch):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    class FlakyReceipt(Backend):
        done = False
        fail_get = False
        def _response(self):
            return dict(self.body, state="succeeded" if self.done else "processing", membership_exists=True,
                        cache_status="done" if self.done else "transcribing", indexed_content_chunks=2 if self.done else 0)
        def status(self, request_id):
            self.gets += 1
            if self.fail_get:
                raise TimeoutError("fictional transient GET failure, never a submission retry")
            assert request_id == self.body["request_id"]
            return self._response()
    backend = FlakyReceipt()
    model = Model([("save_current_video", {"knowledge_base_id": 7}), ("propose_import_status", {})])
    adapter = Adapter()
    task = receive(queue, history, registry, {**env, "comment_text": "@助手 存到编程库"})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / "agent.sqlite3", conversation=history,
        database=tmp_path / "unused-business.db", import_client=backend, model_factory=lambda: model,
        knowledge_factory=lambda _: QueryKnowledge(), adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish, reconcile=bridge.reconcile)
    waiting = runtime.execute_once()
    assert waiting["state"] == "waiting_ingestion" and backend.posts == 1 and model.count == 1
    initial_steps = agent.list_steps(waiting["agent_task_id"])
    assert [step["metadata"]["tool_name"] for step in initial_steps if step["kind"] == "tool_call"] == ["save_current_video"]
    assert sum(step["kind"] == "model_request" for step in initial_steps) == 1
    assert sum(step["kind"] == "model_response" for step in initial_steps) == 1
    backend.done = True
    now = time.time()
    monkeypatch.setattr("app.assistant.queue.time.time", lambda: now + 20)
    assert runtime.poll_waiting_once()["state"] == "ready_to_publish"
    backend.fail_get = True
    deferred = runtime.publish_once()
    assert deferred["state"] == "waiting_ingestion"
    assert adapter.submits == 0 and backend.posts == 1 and model.count == 1
    backend.fail_get = False
    monkeypatch.setattr("app.assistant.queue.time.time", lambda: now + 40)
    assert runtime.poll_waiting_once()["state"] == "ready_to_publish"
    assert runtime.publish_once()["state"] == "b_view_visible"
    assert adapter.submits == 1 and backend.posts == 1 and model.count == 1
    assert agent.list_steps(waiting["agent_task_id"]) == initial_steps
    assert queue.get_task(task["id"])["state"] == "b_view_visible"


def test_verified_failed_import_reports_failure_once_without_model_or_post_replay(tmp_path, monkeypatch):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    class FailedReceipt(Backend):
        failed = False
        def _response(self):
            return dict(self.body, state="failed" if self.failed else "processing", membership_exists=True,
                        cache_status="failed" if self.failed else "transcribing", indexed_content_chunks=0)
    backend = FailedReceipt()
    model = Model([("save_current_video", {"knowledge_base_id": 7}), ("propose_import_status", {})])
    adapter = Adapter()
    task = receive(queue, history, registry, {**env, "comment_text": "@助手 存到编程库"})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / "agent.sqlite3", conversation=history,
        database=tmp_path / "unused-business.db", import_client=backend, model_factory=lambda: model,
        knowledge_factory=lambda _: QueryKnowledge(), adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish, reconcile=bridge.reconcile)
    assert runtime.execute_once()["state"] == "waiting_ingestion"
    backend.failed = True
    now = time.time()
    monkeypatch.setattr("app.assistant.queue.time.time", lambda: now + 20)
    ready = runtime.poll_waiting_once()
    assert ready["state"] == "ready_to_publish" and ready["import_state"] == "failed"
    assert "没有完成" in ready["details"]["reply_text"]
    assert runtime.publish_once()["state"] == "b_view_visible"
    assert runtime.publish_once() is None
    assert backend.posts == 1 and model.count == 1 and adapter.submits == 1
    assert queue.get_task(task["id"])["import_state"] == "failed"


def test_preflight_challenge_requires_owner_fresh_prepare_and_one_release_without_paid_replay(tmp_path):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    health = {"receiver_state": "ready"}
    control.snapshot = lambda: {"desired_state": "running", "run_id": "run-test", **health}
    class VerificationAdapter(Adapter):
        challenge = True
        prepares = 0
        clears = 0
        def check_intervention(self):
            if self.challenge:
                raise ReceiverInterventionRequired("synthetic verification, no real account")
        def prepare_reply(self, target, text):
            self.prepares += 1
            self.check_intervention()
            return super().prepare_reply(target, text)
        def clear_prepared_reply(self, target, text):
            self.check_intervention()
            self.clears += 1
    adapter, model = VerificationAdapter(), QueryModel()
    task = receive(queue, history, registry, {**env, "comment_text": "@助手 请根据编程知识库说明Git。"})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / "agent.sqlite3", conversation=history,
        database=tmp_path / "unused-business.db", model_factory=lambda: model,
        knowledge_factory=lambda _: QueryKnowledge(), adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish,
                               reconcile=bridge.reconcile, resume_adapter=bridge.operator_resume)
    assert runtime.execute_once()["state"] == "ready_to_publish"
    held = runtime.publish_once()
    assert held["state"] == "needs_review" and held["reason_code"] == "preflight_verification_required"
    old_id = held["agent_task_id"]
    assert agent.get_reply(old_id)["send_token"] is None and adapter.submits == 0
    health["receiver_state"] = "verification_required"
    assert bridge.reconcile(queue.get_task(task["id"]))["can_operator_resume"] is False
    with pytest.raises(AgentStoreError):
        agent.release_held_reply(old_id, "operator_without_fresh_ui_proof")
    health["receiver_state"] = "ready"
    with pytest.raises(QueueError, match="operator_resume_unavailable"):
        runtime.resume_task(task["id"])
    assert agent.list_reply_hold_releases(old_id) == [] and adapter.clears == 0
    adapter.challenge = False
    # A non-owner action cannot borrow this page or release its old draft.
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(QueueError, match="operator_resume_unavailable"):
            pool.submit(runtime.resume_task, task["id"]).result(timeout=3)
    assert agent.list_reply_hold_releases(old_id) == []
    resumed = runtime.resume_task(task["id"])
    assert resumed["state"] == "ready_to_publish" and adapter.clears == 1
    assert len(agent.list_reply_hold_releases(old_id)) == 1
    assert agent.get_reply(old_id)["send_token"] is None and adapter.submits == 0
    assert len(model.requests) == 2
    assert runtime.publish_once()["state"] == "b_view_visible"
    assert len(model.requests) == 2 and adapter.submits == 1


def test_submitted_verification_remains_terminal_and_cannot_use_preflight_release(tmp_path):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    control.snapshot = lambda: {"desired_state": "running", "run_id": "run-test", "receiver_state": "ready"}
    class SubmittedVerification(Adapter):
        def submit_reply(self, target, text, *, before_submit):
            before_submit()
            self.submits += 1
            return {"outcome": "verification_required"}
    adapter, model = SubmittedVerification(), QueryModel()
    task = receive(queue, history, registry, {**env, "comment_text": "@助手 请根据编程知识库说明Git。"})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / "agent.sqlite3", conversation=history,
        database=tmp_path / "unused-business.db", model_factory=lambda: model,
        knowledge_factory=lambda _: QueryKnowledge(), adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish,
                               reconcile=bridge.reconcile, resume_adapter=bridge.operator_resume)
    runtime.execute_once()
    result = runtime.publish_once()
    assert result["state"] == "verification_required"
    assert agent.get_reply(result["agent_task_id"])["send_token"]
    with pytest.raises(QueueError, match="resume_not_safe"):
        runtime.resume_task(task["id"])
    assert agent.list_reply_hold_releases(result["agent_task_id"]) == []
    assert runtime.publish_once() is None and adapter.submits == 1


def test_registry_revoked_during_model_return_blocks_retrieval_and_publication(tmp_path):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    knowledge = QueryKnowledge()
    class RevokeOnReturn(QueryModel):
        def complete(self, messages, tools):
            turn = super().complete(messages, tools)
            registry.revoke_grant(migrated["grant_id"])
            return turn
    model, adapter = RevokeOnReturn(), Adapter()
    task = receive(queue, history, registry, {**env, "comment_text": "@助手 请根据编程知识库说明Git。"})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / "agent.sqlite3", conversation=history,
        database=tmp_path / "unused-business.db", model_factory=lambda: model, knowledge_factory=lambda _: knowledge, adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish, reconcile=bridge.reconcile)
    result = runtime.execute_once()
    assert result["state"] == "needs_review" and result["reason_code"] == "binding_changed"
    assert len(model.requests) == 1 and knowledge.queries == []
    assert runtime.publish_once() is None and adapter.submits == 0
    assert queue.get_task(task["id"])["state"] == "needs_review"


def test_registry_revoked_after_prepare_preserves_draft_without_send_reservation(tmp_path):
    binding, agent, env, registry, migrated, queue, history, control = system(tmp_path)
    class RevokeAfterPrepare(Adapter):
        prepares = 0
        def prepare_reply(self, target, text):
            self.prepares += 1
            result = super().prepare_reply(target, text)
            registry.revoke_grant(migrated["grant_id"])
            return result
    model, adapter = QueryModel(), RevokeAfterPrepare()
    task = receive(queue, history, registry, {**env, "comment_text": "@助手 请根据编程知识库说明Git。"})
    bridge = LegacyBridge(registry, queue, control, agent_path=tmp_path / "agent.sqlite3", conversation=history,
        database=tmp_path / "unused-business.db", model_factory=lambda: model,
        knowledge_factory=lambda _: QueryKnowledge(), adapter=adapter)
    runtime = AssistantRuntime(queue, control, execute=bridge.execute, publish=bridge.publish, reconcile=bridge.reconcile)
    assert runtime.execute_once()["state"] == "ready_to_publish"
    result = runtime.publish_once()
    assert result["state"] == "needs_review" and result["reason_code"] == "binding_changed"
    assert adapter.prepares == 1 and adapter.submits == 0 and len(model.requests) == 2
    old_reply = agent.get_reply(result["agent_task_id"])
    assert old_reply["state"] == "needs_review" and old_reply["send_token"] is None and old_reply["text"]
    assert runtime.publish_once() is None


def test_focused_current_video_not_ready_never_falls_back_to_other_library_video(tmp_path):
    from app.mention_agent.knowledge import LocalKnowledgeTools
    path, focus, other = focused_database(tmp_path, focus_status="embedding")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rag = FakeRag([hit(other, "other:1", body="同库其他视频的合成内容，不能冒充当前视频。")])
    result = LocalKnowledgeTools(path, 7, rag=rag, focus_video_id=focus).query("这个视频讲了什么？")
    assert result["status"] == "empty" and result["evidence"] == []
    assert rag.calls == []
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_focused_current_video_filters_intruding_hits_both_before_and_after_retrieval(tmp_path):
    from app.mention_agent.knowledge import LocalKnowledgeTools
    path, focus, other = focused_database(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    good = hit(focus, "focus:1", body="语音文字稿：\n当前视频的合成事实。")
    intruder = hit(other, "other:1", body="同库其他视频的合成事实，不应成为当前视频的依据。")
    rag = FakeRag([intruder, good], selected=[intruder, good])
    result = LocalKnowledgeTools(path, 7, rag=rag, focus_video_id=focus).query("这个视频的要点？")
    assert rag.calls == [("这个视频的要点？", {focus})]
    assert result["status"] == "ok"
    assert [source["platform_item_id"] for source in result["evidence"]] == [focus]
    assert "其他视频" not in json_string(result)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def json_string(value):
    import json
    return json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize('native_armed',[False,True])
def test_actual_owner_loop_latches_challenge_even_when_resume_action_returns_fixed_failure(tmp_path, monkeypatch,native_armed):
    """Run the real owner loop with fake Playwright objects, never a browser."""
    from app.assistant import __main__ as entry
    from app.assistant import browser_lane, conversation, legacy_bridge, runtime as runtime_module
    from app.mention_receiver import __main__ as receiver_entry
    from app.mention_receiver import browser
    from app.mention_agent import service as agent_service
    import playwright.sync_api

    events = []
    run_id = "e" * 32
    class Page:
        ticks = 0
        challenge = False
        url = 'https://www.douyin.com/user/self'
        keyboard = SimpleNamespace(press=lambda key: events.append(("key", key)))
        def is_closed(self): return False
        def wait_for_timeout(self, milliseconds): self.ticks += 1
        def goto(self, *args, **kwargs): events.append(("goto", self.challenge))
    page = Page()
    class Control:
        state = "starting"
        desired = "running"
        def snapshot(self): return {"state": self.state, "desired_state": self.desired, "run_id": run_id}
        def register(self, value): assert value == run_id; self.state = "running"
        def heartbeat(self, value): pass
        def record_capture(self, value, *, admitted_count, baseline_created, unavailable_details=0):
            assert value == run_id and admitted_count == 0 and baseline_created is False
            assert unavailable_details == 0
            events.append(("capture", admitted_count))
        def pause(self): self.desired = "paused"; events.append(("paused", page.challenge))
        def set_receiver_health(self, value, state, reason_code=None): events.append(("receiver", state))
        def set_worker_health(self, *args, **kwargs): pass
        def should_stop(self, value): return page.ticks >= 3
        def finish(self, value, reason): events.append(("finished", reason))
    control = Control()
    class FakeAdapter:
        def __init__(self, page, *args, **kwargs): self.page = page
        def check_intervention(self):
            events.append(("check", self.page.challenge))
            if self.page.challenge:
                raise ReceiverInterventionRequired("synthetic challenge")
    class Lane:
        def __init__(self, adapter, *args): self.adapter = adapter
        def touch_devices(self): pass
        def poll_native(self):
            events.append(('native_poll', page.challenge))
            return {'received':0,'status':{'armed':native_armed}}
        def recover(self): events.append(('native_recover', page.challenge))
        def authority_current(self, task): return True
        def collect(self): page.goto("synthetic-local-snapshot"); return []
        def admitted(self, tasks): pass
    class Bridge:
        choices = SimpleNamespace()
        def __init__(self, *args, **kwargs):
            assert kwargs['usage_budget'].path == tmp_path/'usage-budget.sqlite3'
            assert kwargs['usage_budget'].summary()['all_time']['request_count'] == 0
            assert kwargs['source_authorizer'].__self__.__class__ is Lane
        def execute(self, task): pass
        def publish(self, task): pass
        def reconcile(self, task): pass
        def operator_resume(self, task): pass
        def operator_retry_query(self, task): pass
        def observe_existing_delivery(self, task): pass
        def inspect_existing_reply(self, task_id): pass
        def get_recorded_delivery(self, task): pass
    class Runtime:
        def __init__(self, *args, **kwargs):
            self.intake = kwargs["intake"]
            assert kwargs['retry_adapter'].__self__ is kwargs['execute'].__self__
            assert kwargs['retry_adapter'].__func__ is Bridge.operator_retry_query
        def recover(self): pass
        def receive_once(self): return self.intake()
        def execute_once(self): return None
        def poll_waiting_once(self): return None
        def process_action_once(self):
            page.challenge = True
            events.append(("action_failed", True))
            return {"state": "failed", "reason_code": "operator_resume_unavailable"}
        def reconcile_delivery_once(self): return None
        def publish_once(self): events.append(("publish", page.challenge)); return None
    context = SimpleNamespace(pages=[page], close=lambda: events.append(("context_closed", True)))
    page.context = context
    control.queue = SimpleNamespace(update_control=lambda fn: fn(control.snapshot()))
    # This owner-loop probe has no manual session. Keep the new companion
    # server protocol socket-free rather than starting a real local listener.
    class NoManualServer:
        session_active = owner_finalization_pending = False
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
        def close(self): pass
        def safe_status(self): return {'state':'idle'}
    from app.assistant import manual_verification
    monkeypatch.setattr(manual_verification, 'ManualVerificationServer', NoManualServer)
    def launch(*args, **kwargs):
        assert kwargs['headless'] is True
        assert kwargs['viewport'] == {'width':1100,'height':800}
        events.append(('background_context', True))
        return context
    fake_playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: nullcontext(fake_playwright))
    monkeypatch.setattr(entry, "registry_if_configured", lambda *args: SimpleNamespace(path=tmp_path/'registry.sqlite3'))
    monkeypatch.setattr(agent_service, "configured_database", lambda: tmp_path / "never-open-business.db")
    monkeypatch.setattr(receiver_entry, "configuration", lambda *args: {"receiver_public_number": "1", "receiver_profile_url": "https://www.douyin.com/user/fake-B"})
    monkeypatch.setattr(receiver_entry, "preflight_state", lambda *args: tmp_path / "never-open-profile")
    monkeypatch.setattr(receiver_entry, "receiver_lock", lambda *args: nullcontext())
    monkeypatch.setattr(browser, "NormalPageReceiver", FakeAdapter)
    monkeypatch.setattr(browser, "browser_options", lambda *args: {})
    monkeypatch.setattr(browser_lane, "BrowserLane", Lane)
    monkeypatch.setattr(entry, "build_intake_lane", lambda adapter, *args: Lane(adapter))
    monkeypatch.setattr(conversation, "ConversationStore", lambda *args, **kwargs:
                        SimpleNamespace(path=tmp_path / 'conversation.sqlite3', prepare_scope_v2=lambda: None))
    monkeypatch.setattr(legacy_bridge, "LegacyBridge", Bridge)
    monkeypatch.setattr(runtime_module, "AssistantRuntime", Runtime)
    queue = AssistantQueue(tmp_path / "isolated-owner-queue.sqlite3")
    entry.run_registered(queue, control)
    assert ("action_failed", True) in events and ("paused", True) in events
    assert ("receiver", "verification_required") in events
    assert ('receiver','ready' if native_armed else 'starting') in events
    assert [event for event in events if event[0] == "goto"] == [("goto", False)]
    assert [event for event in events if event[0] == "capture"] == [("capture", 0)]
    assert [event for event in events if event[0] == 'native_poll'] == [('native_poll', False)]
    assert events.index(('native_poll', False)) < events.index(('goto', False))
    assert events.index(('native_recover', False)) < events.index(('native_poll', False))
    assert not any(event[0] in {"publish", "key"} for event in events)
    assert ("context_closed", True) in events
    assert not (tmp_path / "never-open-profile").exists()
    assert not (tmp_path / "never-open-business.db").exists()


@pytest.mark.parametrize("state", ["running", "starting"])
def test_duplicate_or_unregistered_entry_failure_never_finishes_another_registered_pid(tmp_path, monkeypatch, state):
    from app.assistant import __main__ as entry
    snapshot = {"state": state, "desired_state": "running", "run_id": "f" * 32,
                "pid": os.getpid() + os.getppid() + 999999}
    calls = []
    class FakeControl:
        def __init__(self, *args, **kwargs):
            pass
        def snapshot(self):
            return dict(snapshot)
        def finish(self, *args):
            calls.append(args)
    monkeypatch.setattr(entry, "STATE", tmp_path)
    monkeypatch.setattr(entry, "STORAGE", tmp_path / "storage")
    monkeypatch.setattr(entry, "RECEIVER_STATE", tmp_path / "receiver")
    monkeypatch.setattr(entry, "AssistantControl", FakeControl)
    monkeypatch.setattr(entry, "start_guard", lambda: True)
    monkeypatch.setattr(entry, "run_registered", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("synthetic lock/start failure")))
    assert entry.main(["--state-dir", str(tmp_path), "--run-registered"]) == 1
    assert calls == []
