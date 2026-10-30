"""Independent synthetic review probes; no production adapters or network."""
import pytest

from app.assistant.workflow_store import WorkflowError
from tests.test_workflow_core import Harness, amendment, plan


def test_revise_cannot_authorize_added_import_using_the_old_query_plan(tmp_path):
    case = Harness(tmp_path, goal="query")
    original = case.engine.authorize

    def authorize(workflow, phase):
        if phase == "revise":
            return workflow["plan"]["goal"] == "query"
        return original(workflow, phase)

    case.engine.authorize = authorize
    with pytest.raises(WorkflowError):
        case.engine.revise(case.id, expected_revision=case.workflow["revision"],
                           plan=plan("save_then_query"), amendment=amendment())
    assert case.store.get(case.id)["plan"]["goal"] == "query"
    assert case.calls == []


def test_revise_cannot_add_paid_query_to_save_via_plan_amendment(tmp_path):
    case = Harness(tmp_path, goal="save")
    imported = case.tick()
    with pytest.raises(WorkflowError):
        case.engine.revise(case.id, expected_revision=imported["revision"],
                           plan=plan("save_then_query"), amendment=amendment())
    assert case.store.get(case.id)["plan"]["goal"] == "save"
    assert [name for name, _ in case.calls] == ["import_current_video"]


def test_expired_unknown_reservation_cannot_dispatch_after_authorization_race(tmp_path):
    case = Harness(tmp_path, goal="save")
    original = case.engine.authorize

    def expire_from_another_executor(workflow, phase):
        if phase == "dispatch:import_current_video":
            current = case.store.get(case.id)
            case.time[0] += 121
            case.store.expire_reserved(case.id, expected_revision=current["revision"])
        return original(workflow, phase)

    case.engine.authorize = expire_from_another_executor
    try:
        case.tick()
    except WorkflowError:
        pass
    assert case.calls == [], "effect tool was dispatched after reservation became unknown"
    assert case.store.get(case.id)["state"] == "needs_review"


def test_late_finish_cannot_promote_a_reservation_past_its_lease(tmp_path):
    case = Harness(tmp_path, goal="save")
    claim = case.store.reserve_next(case.id, expected_revision=1,
                                    estimated_tokens=5, lease_seconds=1)
    case.time[0] += 2
    try:
        case.store.finish(claim, state="succeeded", actual_tokens=0,
                          result={"status": "accepted", "receipt_id": "fixture-receipt"})
    except WorkflowError:
        pass
    assert case.store.get(case.id)["steps"][0]["state"] != "succeeded"
