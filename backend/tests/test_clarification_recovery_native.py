"""Optional clarification recovery cannot weaken native owner/source gates.

Only in-memory fakes and an empty Source scan are used: no database, browser,
network, model, enqueue or publication is available to these tests.
"""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from app.assistant.native_lane import NativeLane, NativeLaneError
from app.mention_receiver.browser import ReceiverInterventionRequired


class EmptySources:
    def __init__(self, *, error=None):
        self.error = error
        self.scans = 0

    def recoverable_ui_plans(self):
        self.scans += 1
        if self.error is not None:
            raise self.error
        return []


class Coordinator:
    def __init__(self, *, failure_method=None, error=None):
        self.failure_method = failure_method
        self.error = error
        self.calls = []

    def reconcile_existing(self):
        self.calls.append(("reconcile", None))
        if self.failure_method == "reconcile":
            raise self.error
        return {"links_repaired": 0}

    def recover_links(self, tasks):
        self.calls.append(("links", tasks))
        if self.failure_method == "links":
            raise self.error
        return {"links_repaired": 0}


def lane_case(coordinator, *, source_error=None):
    def forbidden_effect(*args, **kwargs):
        pytest.fail("empty native recovery must not use a UI, task or effect capability")

    lane = NativeLane.__new__(NativeLane)
    lane.browser_lane = SimpleNamespace()
    lane._owner_thread = threading.get_ident()
    lane.clarification_coordinator = coordinator
    lane.source_store = EmptySources(error=source_error)
    lane.should_stop = lambda: False
    lane.queue = SimpleNamespace(enqueue=forbidden_effect,
                                 lookup_by_source_reference=forbidden_effect)
    lane.conversation = SimpleNamespace(task_scope=forbidden_effect, bind_task=forbidden_effect)
    lane.adapter = SimpleNamespace(check_intervention=forbidden_effect,
                                   prepare_reply=forbidden_effect, submit_reply=forbidden_effect)
    return lane


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_optional_reconcile_error_preserves_coordinator_and_keeps_empty_source_scanning(error_type):
    coordinator = Coordinator(failure_method="reconcile", error=error_type("synthetic optional failure"))
    lane = lane_case(coordinator)

    assert lane.recover() == 0
    assert lane.recover() == 0

    assert lane.clarification_coordinator is coordinator
    assert coordinator.calls == [("reconcile", None), ("reconcile", None)]
    assert lane.source_store.scans == 2
    assert lane.last_clarification_recovery_diagnostic == {"code": "clarification_recovery_unavailable"}


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_optional_link_error_retains_coordinator_tasks_and_subsequent_native_scan(error_type):
    coordinator = Coordinator(failure_method="links", error=error_type("synthetic optional failure"))
    lane = lane_case(coordinator)
    tasks = [{"id": "synthetic-existing-task", "envelope": {"reference_id": "synthetic-reference"}}]
    before = deepcopy(tasks)

    assert lane._recover_clarifications(tasks) is None
    assert lane.clarification_coordinator is coordinator and tasks == before
    assert coordinator.calls == [("links", tasks)]
    assert lane.source_store.scans == 0
    assert lane.last_clarification_recovery_diagnostic == {"code": "clarification_recovery_unavailable"}

    assert lane.recover() == 0
    assert coordinator.calls == [("links", tasks), ("reconcile", None)]
    assert lane.clarification_coordinator is coordinator and lane.source_store.scans == 1


@pytest.mark.parametrize("method", ["reconcile", "links"])
@pytest.mark.parametrize("error_type", [ReceiverInterventionRequired, KeyboardInterrupt, SystemExit])
def test_human_intervention_and_process_stop_signals_propagate_from_optional_recovery(method, error_type):
    error = error_type("synthetic stop or intervention")
    coordinator = Coordinator(failure_method=method, error=error)
    lane = lane_case(coordinator)
    tasks = [{"id": "synthetic-existing-task"}]

    with pytest.raises(error_type) as caught:
        if method == "reconcile":
            lane.recover()
        else:
            lane._recover_clarifications(tasks)

    assert caught.value is error
    assert lane.clarification_coordinator is coordinator
    assert lane.source_store.scans == 0
    assert not hasattr(lane, "last_clarification_recovery_diagnostic")


def test_native_owner_failure_precedes_optional_recovery_and_source_scan():
    coordinator = Coordinator(failure_method="reconcile", error=ValueError("unreached"))
    lane = lane_case(coordinator)
    lane._owner_thread = threading.get_ident() + 1

    with pytest.raises(NativeLaneError, match="^native_browser_owner_required$"):
        lane.recover()

    assert coordinator.calls == [] and lane.source_store.scans == 0
    assert lane.clarification_coordinator is coordinator


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_shared_source_error_is_not_swallowed_after_an_optional_recovery_error(error_type):
    error = error_type("synthetic shared source failure")
    coordinator = Coordinator(failure_method="reconcile", error=ValueError("synthetic optional failure"))
    lane = lane_case(coordinator, source_error=error)

    with pytest.raises(error_type) as caught:
        lane.recover()

    assert caught.value is error
    assert coordinator.calls == [("reconcile", None)] and lane.source_store.scans == 1
    assert lane.clarification_coordinator is coordinator
    assert lane.last_clarification_recovery_diagnostic == {"code": "clarification_recovery_unavailable"}
