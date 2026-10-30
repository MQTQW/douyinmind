"""Durable workflow behavior using invented scopes and in-process fake tools only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.assistant.workflow_engine import WorkflowEngine
from app.assistant.workflow_store import WorkflowClaim, WorkflowError, WorkflowStore, validate_plan


def scope(**changes):
    return {"user_id": "fictional-user", "device_id": "fictional-device",
        "instance_id": "fictional-instance", "sender_profile_url": "https://www.douyin.com/user/sender-fixture",
        "receiver_profile_url": "https://www.douyin.com/user/receiver-fixture",
        "video_id": "7999999000000000011", "root_reference_id": "fixture-root",
        "reference_id": "fixture-comment", "knowledge_base_id": 7,
        "query_scope": "library", **changes}


def source(**changes):
    return {"source_key": "fixture-source", "origin_task_id": "1" * 32,
        "created_at": 1000.0, "ui_digest": "a" * 64, "effects_state": "none", **changes}


def amendment(**changes):
    return {"source_key": "fixture-amendment", "created_at": 1000.0, "ui_digest": "c" * 64, **changes}


def plan(goal="save_then_query", question="概括合成资料的三个要点"):
    steps = []
    if goal != "query":
        steps.extend([{"tool": "import_current_video", "args": {}}, {"tool": "await_import", "args": {}}])
    if goal != "save":
        steps.append({"tool": "query_current_video", "args": {"question": question}})
    steps.append({"tool": "propose_reply", "args": {"purpose": "import_status" if goal == "save" else "answer"}})
    return {"goal": goal, "steps": steps}


class Harness:
    def __init__(self, tmp_path, *, store=None, clock=None, budget_limit=100, goal="save_then_query"):
        self.time = clock or [1000.0]
        self.store = store or WorkflowStore.prepare(tmp_path / "workflow.sqlite3",
            activated_at=1000.0, clock=lambda: self.time[0])
        self.calls, self.auth_calls, self.estimates = [], [], []
        self.allowed, self.block_phase, self.processing = True, None, False
        self.mutate_result = lambda tool, result: result
        self.raise_tool = None
        self.query_tokens, self.proposal_tokens = 6, 4
        self.publication, self.publication_reads = None, 0
        self.valid_proposal = True
        self.before_checkpoint = lambda name: None
        self.engine = WorkflowEngine(self.store, tools={name: self.tool(name) for name in (
            "import_current_video", "await_import", "query_current_video", "propose_reply")},
            authorize=self.authorize, estimate_tokens=self.estimate,
            validate_proposal=lambda workflow, proposal, evidence: self.valid_proposal,
            read_publication=self.read_publication, poll_seconds=5)
        self.workflow = self.engine.start(source=source(), scope=scope(), plan=plan(goal),
            budget_limit=budget_limit, initial_tokens=3)
        self.id = self.workflow["workflow_id"]

    def authorize(self, workflow, phase):
        self.auth_calls.append(phase)
        return self.allowed and self.block_phase != phase

    def estimate(self, workflow, tool):
        self.estimates.append(tool)
        return 5

    def read_publication(self, workflow):
        self.publication_reads += 1
        return deepcopy(self.publication)

    def tool(self, name):
        def run(context, *, checkpoint):
            self.before_checkpoint(name)
            assert checkpoint() is True
            self.calls.append((name, deepcopy(context)))
            if self.raise_tool == name:
                raise RuntimeError("unrecorded fake provider detail")
            common = {"operation_key": context["request_key"], "video_id": context["scope"]["video_id"],
                "knowledge_base_id": context["scope"]["knowledge_base_id"], "usage": {"total_tokens": 0}}
            if name == "import_current_video":
                result = {**common, "status": "accepted", "receipt_id": "fixture-receipt"}
            elif name == "await_import":
                result = {**common, "status": "processing" if self.processing else "succeeded",
                    "receipt_id": context["import_receipt"]["receipt_id"], "readable": not self.processing}
            elif name == "query_current_video":
                result = {**common, "status": "ok", "sources": [{"source_id": "fixture-evidence",
                    "platform_item_id": context["scope"]["video_id"], "chunk_id": "fictional-chunk",
                    "text": "这份合成资料介绍先分类，再回看原文。"}], "usage": {"total_tokens": self.query_tokens}}
            else:
                purpose = context["args"]["purpose"]
                result = {**common, "status": "ready", "proposal": {"kind": purpose,
                    "text": "合成资料可先分类，再回看原文。" if purpose == "answer" else "合成视频已加入指定知识库。",
                    "source_ids": ["fixture-evidence"] if purpose == "answer" else []},
                    "usage": {"total_tokens": self.proposal_tokens}}
            return self.mutate_result(name, result)
        return run

    def tick(self):
        return self.engine.tick(self.id)

    def ready(self):
        for _ in range(5):
            row = self.store.get(self.id)
            if row["state"] == "proposal_ready":
                return row
            self.tick()
        raise AssertionError("fake workflow did not become proposal ready")

    def publication_receipt(self, state="b_view_visible"):
        row = self.store.get(self.id)
        text = row["steps"][len(row["plan"]["steps"]) - 1]["result"]["proposal"]["text"]
        return {"workflow_id": self.id, "origin_task_id": source()["origin_task_id"],
            "state": state, "reply_id": "fixture-reply",
            "text_sha256": hashlib.sha256(text.encode()).hexdigest()}


def test_constructor_does_no_io_or_implicit_initialization(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: calls.append((a, k)))
    store = WorkflowStore(tmp_path / "absent" / "workflow.sqlite3")
    assert calls == [] and not store.path.parent.exists()
    with pytest.raises(WorkflowError, match="not_open"):
        store.get("workflow-missing")
    assert calls == []


def test_save_wait_query_proposal_and_actual_publication_are_distinct(tmp_path):
    c = Harness(tmp_path)
    assert c.tick()["state"] == "running"
    c.processing = True
    waiting = c.tick()
    assert waiting["state"] == "waiting_ingestion"
    assert [name for name, _ in c.calls] == ["import_current_video", "await_import"]
    assert "await_import" not in c.estimates
    before = c.store.path.read_bytes()
    assert c.tick()["revision"] == waiting["revision"]
    assert c.store.path.read_bytes() == before and len(c.calls) == 2
    c.time[0] += 5
    c.processing = False
    assert c.tick()["steps"][1]["state"] == "succeeded"
    assert c.tick()["steps"][2]["state"] == "succeeded"
    ready = c.tick()
    assert ready["state"] == "proposal_ready" and ready["spent_tokens"] == 13
    assert ready["held_tokens"] == 0
    c.tick()
    assert c.publication_reads == 1 and len(c.calls) == 5
    c.publication = c.publication_receipt()
    done = c.tick()
    assert done["state"] == "completed"
    c.tick()
    assert len(c.calls) == 5
    assert all("claim_token" not in step for step in done["steps"])


def test_reopen_resumes_after_import_without_submitting_it_again(tmp_path):
    c = Harness(tmp_path)
    c.tick()
    reopened = WorkflowStore.open(c.store.path, clock=lambda: c.time[0])
    other = Harness(tmp_path, store=reopened, clock=c.time)
    ready = other.ready()
    assert ready["state"] == "proposal_ready"
    assert [name for name, _ in other.calls] == ["await_import", "query_current_video", "propose_reply"]
    assert len(c.calls) == 1


def test_duplicate_source_returns_existing_progress_and_changed_source_conflicts(tmp_path):
    c = Harness(tmp_path)
    c.tick()
    same = c.engine.start(source=source(), scope=scope(), plan=plan(), budget_limit=100, initial_tokens=3)
    assert same["workflow_id"] == c.id and same["steps"][0]["state"] == "succeeded"
    for change in ({"source_key": "another-source"}, {"ui_digest": "b" * 64}):
        with pytest.raises(WorkflowError, match="source_conflict"):
            c.engine.start(source=source(**change), scope=scope(), plan=plan(), budget_limit=100, initial_tokens=3)


@pytest.mark.parametrize("changed", [{"created_at": 999.0}, {"effects_state": "sent"}, {"effects_state": "unknown"}])
def test_old_or_consumed_sources_do_not_start(tmp_path, changed):
    store = WorkflowStore.prepare(tmp_path / "work.sqlite3", activated_at=1000, clock=lambda: 1000)
    with pytest.raises(WorkflowError):
        store.create(source=source(**changed), scope=scope(), plan=plan(), budget_limit=100)
    assert store.events("workflow-" + "a" * 64) == []


def test_unfinished_claim_expires_to_unknown_and_is_never_replayed(tmp_path):
    c = Harness(tmp_path)
    claim = c.store.reserve_next(c.id, expected_revision=c.workflow["revision"],
        estimated_tokens=7, lease_seconds=5)
    assert c.tick()["steps"][0]["state"] == "reserved" and c.calls == []
    c.time[0] += 6
    result = c.tick()
    assert result["state"] == "needs_review" and result["steps"][0]["state"] == "unknown"
    assert result["held_tokens"] == 7 and c.calls == []
    c.tick()
    assert c.calls == []
    with pytest.raises(WorkflowError):
        c.store.finish(claim, state="succeeded", actual_tokens=0)


def test_two_executors_cannot_reserve_the_same_step_and_claims_are_issuer_bound(tmp_path):
    c = Harness(tmp_path)
    claim = c.store.reserve_next(c.id, expected_revision=1, estimated_tokens=5)
    with pytest.raises(WorkflowError, match="revision_changed"):
        c.store.reserve_next(c.id, expected_revision=1, estimated_tokens=5)
    other = WorkflowStore.open(c.store.path, clock=lambda: c.time[0])
    with pytest.raises(WorkflowError, match="claim_invalid"):
        other.finish(claim, state="failed", actual_tokens=0)
    forged = WorkflowClaim(claim.workflow_id, claim.ordinal, claim.token, claim.revision, claim.request_key, "0" * 64)
    with pytest.raises(WorkflowError, match="claim_invalid"):
        c.store.finish(forged, state="failed", actual_tokens=0)


@pytest.mark.parametrize("tool", ["import_current_video", "query_current_video", "propose_reply"])
def test_unknown_tool_results_hold_budget_and_stop_without_retry(tmp_path, tool):
    c = Harness(tmp_path)
    c.raise_tool = tool
    for _ in range(5):
        row = c.tick()
        if row["state"] == "needs_review":
            break
    assert row["reason_code"] == "workflow_tool_result_unknown" and row["held_tokens"] == 5
    count = len(c.calls)
    c.tick()
    assert len(c.calls) == count


def test_missing_usage_is_unknown_not_zero_cost(tmp_path):
    c = Harness(tmp_path, goal="query")
    c.mutate_result = lambda tool, result: {k: v for k, v in result.items() if k != "usage"}
    result = c.tick()
    assert result["state"] == "needs_review" and result["steps"][0]["state"] == "unknown"
    assert result["spent_tokens"] == 3 and result["held_tokens"] == 5


def test_entire_workflow_budget_includes_initial_understanding_and_actual_usage(tmp_path):
    c = Harness(tmp_path, budget_limit=15)
    c.query_tokens = 12
    c.tick(); c.tick()
    queried = c.tick()
    assert queried["spent_tokens"] == 15 and queried["state"] == "running"
    blocked = c.tick()
    assert blocked["state"] == "needs_review" and blocked["reason_code"] == "workflow_budget_exceeded"
    assert all(name != "propose_reply" for name, _ in c.calls)


def test_provider_overrun_is_recorded_and_does_not_start_next_step(tmp_path):
    c = Harness(tmp_path, budget_limit=15)
    c.query_tokens = 14
    c.tick(); c.tick()
    row = c.tick()
    assert row["spent_tokens"] == 17 and row["reason_code"] == "workflow_budget_overrun"
    assert c.tick()["state"] == "needs_review"
    assert all(name != "propose_reply" for name, _ in c.calls)


@pytest.mark.parametrize("phase", ["before:import_current_video", "dispatch:import_current_video"])
def test_fresh_revocation_prevents_dispatch(tmp_path, phase):
    c = Harness(tmp_path)
    c.block_phase = phase
    row = c.tick()
    assert row["state"] == "needs_review" and row["reason_code"] == "workflow_authorization_changed"
    assert c.calls == [] and row["held_tokens"] == 0


def test_revocation_after_effect_keeps_real_receipt_and_prevents_further_work(tmp_path):
    c = Harness(tmp_path)
    c.block_phase = "after:import_current_video"
    row = c.tick()
    assert row["state"] == "needs_review" and row["steps"][0]["state"] == "unknown"
    assert row["steps"][0]["result"]["receipt_id"] == "fixture-receipt"
    c.tick()
    assert len(c.calls) == 1


@pytest.mark.parametrize("key,value", [("video_id", "7999999000000000099"), ("knowledge_base_id", 88), ("operation_key", "wrong-key")])
def test_tool_result_scope_mismatch_never_advances(tmp_path, key, value):
    c = Harness(tmp_path)
    c.mutate_result = lambda tool, result: {**result, key: value}
    row = c.tick()
    assert row["state"] == "needs_review" and row["steps"][0]["state"] == "unknown"
    assert row["reason_code"] == "workflow_result_scope_mismatch"
    assert row["held_tokens"] == 5 and row["spent_tokens"] == 3
    assert row["steps"][0]["actual_tokens"] is None


def test_receipt_poll_can_never_switch_to_another_import(tmp_path):
    c = Harness(tmp_path)
    c.tick()
    c.mutate_result = lambda tool, result: {**result, "receipt_id": "different-receipt"} if tool == "await_import" else result
    row = c.tick()
    assert row["reason_code"] == "workflow_import_receipt_changed"
    assert [name for name, _ in c.calls] == ["import_current_video", "await_import"]


def test_only_unexecuted_tail_can_change_and_summary_can_be_removed(tmp_path):
    c = Harness(tmp_path)
    c.tick(); row = c.tick()
    revised = c.engine.revise(c.id, expected_revision=row["revision"], plan=plan("save"), amendment=amendment())
    assert revised["steps"][0]["state"] == revised["steps"][1]["state"] == "succeeded"
    assert revised["steps"][2]["spec"]["tool"] == "propose_reply"
    assert c.tick()["state"] == "proposal_ready"
    assert all(name != "query_current_video" for name, _ in c.calls)
    with pytest.raises(WorkflowError, match="revision_not_allowed"):
        c.engine.revise(c.id, expected_revision=c.store.get(c.id)["revision"], plan=plan(), amendment=amendment(source_key="second-amendment"))


def test_executed_query_question_and_stale_plan_revision_cannot_change(tmp_path):
    c = Harness(tmp_path)
    c.tick()
    with pytest.raises(WorkflowError, match="revision_changed"):
        c.engine.revise(c.id, expected_revision=1, plan=plan("save"), amendment=amendment())
    c.tick(); row = c.tick()
    with pytest.raises(WorkflowError, match="executed_steps_immutable"):
        c.engine.revise(c.id, expected_revision=row["revision"], plan=plan(question="另一个问题"), amendment=amendment())


def test_cancel_preserves_completed_import_and_never_undoes_it(tmp_path):
    c = Harness(tmp_path)
    row = c.tick()
    cancelled = c.engine.cancel(c.id, expected_revision=row["revision"])
    assert cancelled["state"] == "cancelled" and cancelled["steps"][0]["state"] == "succeeded"
    c.tick()
    assert len(c.calls) == 1


@pytest.mark.parametrize("bad", [
    {"goal": "save", "steps": [{"tool": "send_comment", "args": {}}]},
    {"goal": "query", "steps": [{"tool": "propose_reply", "args": {"purpose": "answer"}}]},
    {"goal": "save", "steps": [{"tool": "import_current_video", "args": {"knowledge_base_id": 88}},
        {"tool": "await_import", "args": {}}, {"tool": "propose_reply", "args": {"purpose": "import_status"}}]},
    {"goal": "query", "steps": [{"tool": "query_current_video", "args": {"question": "问题", "video_id": "other"}},
        {"tool": "propose_reply", "args": {"purpose": "answer"}}]},
])
def test_planner_cannot_invent_tools_scope_arguments_or_skip_preconditions(bad):
    with pytest.raises(WorkflowError):
        validate_plan(bad, scope())


def test_all_local_query_does_not_grant_import(tmp_path):
    with pytest.raises(WorkflowError, match="save_library_required"):
        validate_plan(plan(), scope(query_scope="all_local", knowledge_base_id=None))


def test_proposal_requires_actual_sources_and_program_validation(tmp_path):
    c = Harness(tmp_path)
    c.valid_proposal = False
    c.tick(); c.tick(); c.tick()
    result = c.tick()
    assert result["state"] == "needs_review" and result["reason_code"] == "workflow_proposal_validation_failed"
    assert result["held_tokens"] == 0 and result["spent_tokens"] == 13
    assert c.publication_reads == 0


def test_unknown_publication_is_not_completion_and_never_republishes(tmp_path):
    c = Harness(tmp_path)
    c.ready()
    c.publication = c.publication_receipt("delivery_unknown")
    row = c.tick()
    assert row["state"] == "needs_review" and row["publication"]["state"] == "delivery_unknown"
    count = len(c.calls)
    c.tick()
    assert len(c.calls) == count and c.publication_reads == 1


def test_wrong_publication_target_or_text_hash_cannot_complete(tmp_path):
    c = Harness(tmp_path)
    c.ready()
    c.publication = {**c.publication_receipt(), "text_sha256": "b" * 64}
    row = c.tick()
    assert row["state"] == "needs_review" and row["reason_code"] == "workflow_publication_invalid"


def test_schema_marker_or_deleted_database_cannot_be_silently_adopted(tmp_path):
    c = Harness(tmp_path)
    original = c.store.path.read_bytes()
    marker = json.loads(c.store.marker.read_text())
    marker["owner"] = "f" * 32
    c.store.marker.write_text(json.dumps(marker))
    with pytest.raises(WorkflowError):
        c.store.get(c.id)
    assert c.store.path.read_bytes() == original
    c.store.path.unlink()
    with pytest.raises(WorkflowError):
        WorkflowStore.prepare(c.store.path, activated_at=1000)
    assert not c.store.path.exists()


def test_foreign_database_and_protected_location_are_not_modified(tmp_path):
    p = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(p) as db:
        db.execute("CREATE TABLE private_data(value TEXT)")
    before = p.read_bytes()
    with pytest.raises(WorkflowError):
        WorkflowStore.prepare(p, activated_at=1000)
    assert p.read_bytes() == before
    with pytest.raises(WorkflowError, match="path_invalid"):
        WorkflowStore.prepare(tmp_path / "blocked.sqlite3", protected_paths=(tmp_path,))


def test_untrusted_result_secrets_are_not_persisted(tmp_path):
    c = Harness(tmp_path)
    c.mutate_result = lambda tool, result: {**result, "cookie": "synthetic-forbidden"}
    row = c.tick()
    assert row["state"] == "needs_review"
    assert row["steps"][0]["result"] is None
    assert b"synthetic-forbidden" not in c.store.path.read_bytes()


@pytest.mark.parametrize("initial,new_goal", [("query", "save"), ("query", "save_then_query"), ("save", "save_then_query")])
def test_replan_cannot_add_actions_to_original_user_goal(tmp_path, initial, new_goal):
    c = Harness(tmp_path, goal=initial)
    seen = []
    c.engine.authorize = lambda workflow, phase: seen.append((phase, deepcopy(workflow))) or True
    with pytest.raises(WorkflowError, match="goal_expansion_rejected"):
        c.engine.revise(c.id, expected_revision=1, plan=plan(new_goal), amendment=amendment())
    candidate = [value for phase, value in seen if phase == "revise"][0]
    assert candidate["plan"]["goal"] == new_goal
    assert candidate["previous_plan"]["goal"] == initial
    assert candidate["amendment"] == amendment()
    assert c.store.get(c.id)["plan"]["goal"] == initial and c.calls == []


def test_replan_requires_independent_user_amendment_and_unique_consumption(tmp_path):
    c = Harness(tmp_path)
    with pytest.raises(WorkflowError, match="amendment_invalid"):
        c.engine.revise(c.id, expected_revision=1, plan=plan("save"), amendment={})
    with pytest.raises(WorkflowError, match="amendment_invalid"):
        c.engine.revise(c.id, expected_revision=1, plan=plan("save"),
            amendment=amendment(source_key=source()["source_key"]))
    row = c.engine.revise(c.id, expected_revision=1, plan=plan("save"), amendment=amendment())
    assert row["plan"]["goal"] == "save"
    again = c.engine.revise(c.id, expected_revision=row["revision"], plan=plan("save"), amendment=amendment())
    assert again["revision"] == row["revision"]
    with pytest.raises(WorkflowError):
        c.engine.revise(c.id, expected_revision=row["revision"], plan=plan(),
            amendment=amendment())
    assert len([event for event in c.store.events(c.id) if event["kind"] == "plan_revised"]) == 1


def test_dispatch_authorization_cannot_expire_claim_then_allow_tool(tmp_path):
    c = Harness(tmp_path)
    def gate(workflow, phase):
        if phase == "dispatch:import_current_video":
            c.time[0] += 121
            c.store.expire_reserved(c.id, expected_revision=workflow["revision"])
        return True
    c.engine.authorize = gate
    row = c.tick()
    assert row["state"] == "needs_review"
    assert row["reason_code"] == "workflow_interrupted_effect_unknown"
    assert c.calls == []


def test_adapter_checkpoint_fences_a_claim_expired_after_engine_check(tmp_path):
    c = Harness(tmp_path)
    def race(name):
        c.time[0] += 121
        row = c.store.get(c.id)
        c.store.expire_reserved(c.id, expected_revision=row["revision"])
    c.before_checkpoint = race
    row = c.tick()
    assert row["state"] == "needs_review"
    assert c.calls == []


def test_late_finish_never_advances_an_expired_reservation(tmp_path):
    c = Harness(tmp_path)
    claim = c.store.reserve_next(c.id, expected_revision=1, estimated_tokens=5, lease_seconds=1)
    c.time[0] += 2
    row = c.store.finish(claim, state="succeeded", actual_tokens=2,
        result={"status": "accepted", "receipt_id": "fixture-late-receipt"})
    assert row["state"] == "needs_review" and row["steps"][0]["state"] == "unknown"
    assert row["reason_code"] == "workflow_claim_expired" and row["spent_tokens"] == 5
    c.tick()
    assert c.calls == []


def test_future_source_does_not_backfill_an_old_task(tmp_path):
    store = WorkflowStore.prepare(tmp_path / "future.sqlite3", activated_at=1000, clock=lambda: 1000)
    with pytest.raises(WorkflowError, match="future_source_rejected"):
        store.create(source=source(created_at=1006), scope=scope(), plan=plan(), budget_limit=100)


def test_boolean_library_scope_result_is_not_an_integer_library(tmp_path):
    c = Harness(tmp_path, goal="query")
    c.mutate_result = lambda tool, result: {**result, "knowledge_base_id": True}
    assert c.tick()["reason_code"] == "workflow_result_scope_mismatch"


def test_explicit_null_scope_key_is_required_for_all_local_query(tmp_path):
    c = Harness(tmp_path, goal="query")
    row = c.engine.start(source=source(source_key="all-local-source", origin_task_id="2" * 32),
        scope=scope(query_scope="all_local", knowledge_base_id=None), plan=plan("query"),
        budget_limit=100, initial_tokens=3)
    c.id = row["workflow_id"]
    c.mutate_result = lambda tool, result: {key: value for key, value in result.items() if key != "knowledge_base_id"}
    assert c.tick()["reason_code"] == "workflow_result_scope_mismatch"


def test_zero_usage_from_another_operation_never_releases_this_reservation(tmp_path):
    c = Harness(tmp_path)
    c.mutate_result = lambda tool, result: {**result, "operation_key": "another-operation",
        "usage": {"total_tokens": 0}}
    row = c.tick()
    assert row["state"] == "needs_review" and row["steps"][0]["state"] == "unknown"
    assert row["held_tokens"] == 5 and row["spent_tokens"] == 3
    assert row["steps"][0]["actual_tokens"] is None
    assert row["steps"][0]["result"] is None
    c.tick()
    assert len(c.calls) == 1
