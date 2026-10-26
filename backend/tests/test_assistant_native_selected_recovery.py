"""Unspent selected handoff recovery; fake UI/identity and temp ledgers only."""
from copy import deepcopy

from tests.test_assistant_choice_controller import case
from tests.test_assistant_native_lane import choice_native_system
from tests.test_assistant_native_slow_identity_gate import aging_source


def test_stage_delayed_121_seconds_reverifies_identity_before_native_plan(case):
    lane, store, root_task, session, adapter = choice_native_system(case)
    calls = aging_source(store, lane.native_receiver)
    original = case.controller._stage
    def slow_stage(value, run_id):
        rows = original(value, run_id)
        case.clock[0] += 121
        store.transport_fresh = False
        return rows
    case.controller._stage = slow_stage
    rows = lane.collect()
    assert len(rows) == 1
    assert calls == [('fixed_identity_get',)]
    assert store.plan is not None and store.state == 'planned'


def prepared_selection(case):
    lane, store, root_task, session, adapter = choice_native_system(case)
    rows = case.controller.collect_pending(adapter, 'prior-unspent-read', choice_id=session['choice_id'])
    assert len(rows) == 1
    selected = case.store.get(session['choice_id'])
    lane.clear_buffer()
    case.clock[0] += 31
    return lane, store, session, adapter, selected


def test_exact_selected_recovery_rereads_UI_without_second_reserve_or_timestamp_change(case):
    lane, store, session, adapter, selected = prepared_selection(case)
    before = deepcopy(selected['selection_envelope'])
    rows = lane.collect()
    assert len(rows) == 1 and len(adapter.reads) == 2
    assert rows[0]['envelope'] == before
    current = case.store.get(session['choice_id'])
    assert current['selection_envelope'] == before and current['selected_kb_id'] == 8
    with case.store._db() as db:
        assert db.execute("SELECT COUNT(*) FROM choice_events WHERE choice_id=? AND event='selection_reserved'", (session['choice_id'],)).fetchone()[0] == 1
    child = case.queue.enqueue(**rows[0]); lane.admitted([child])
    assert case.store.get(session['choice_id'])['child_task_id'] == child['id']
    assert case.queue.list_tasks()['total'] == 2


def test_selected_recovery_wrong_actual_reply_cannot_stage_or_change_original_selection(case):
    lane, store, session, adapter, selected = prepared_selection(case)
    adapter.text = '1'; adapter.target = 7
    assert lane.collect() == []
    current = case.store.get(session['choice_id'])
    assert current['selection_envelope'] == selected['selection_envelope']
    assert current['child_task_id'] is None and store.plan is None
    assert len(adapter.reads) == 2


def test_selected_recovery_revoked_scope_never_creates_a_child(case):
    lane, store, session, adapter, selected = prepared_selection(case)
    auth = selected['selected_authorization']
    case.registry.grant_library(auth['user_id'],auth['device_id'],8,
        can_query=False,can_import=False,public_reply_allowed=False)
    assert lane.collect() == []
    assert case.store.get(session['choice_id'])['child_task_id'] is None
    assert store.plan is None and case.queue.list_tasks()['total'] == 1
