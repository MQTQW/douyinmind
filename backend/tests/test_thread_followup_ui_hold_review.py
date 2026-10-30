"""Explicit UI-held followup review, using only synthetic UI and temporary stores."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import json

import pytest

from app.assistant.native_lane import NativeLaneError
from app.assistant.native_source_store import NativeStoreError
from app.mention_receiver.browser import ReceiverDetailError
# These imported fixture aliases are explicit so this file also runs alone.
from tests.test_native_source_followup_review import (
    case as persisted_review_case, reply_case,
    request as request_ui_review, audits, facts, row,
)
from tests.test_thread_followup_owner_review import (
    case as observed_followup_case, semantic_case, inspection_lane,
)
from tests.test_native_recovery_owner import case as recovery_owner_case, SOURCE

UI_HOLDS = ('native_ui_unverified', 'native_followup_ui_unverified')


@pytest.fixture(params=UI_HOLDS)
def ui_persisted_case(request, persisted_review_case):
    store, clock, source, first = persisted_review_case
    store.hold(source, first['claim_token'], request.param)
    return SimpleNamespace(values=persisted_review_case, store=store, clock=clock,
        source=source, first=first, code=request.param)


@pytest.fixture(params=UI_HOLDS)
def ui_observed_case(request, observed_followup_case):
    lane, store, state, saved, events = inspection_lane(observed_followup_case)
    saved['hold_code'] = request.param
    def forbidden(*args, **kwargs):
        pytest.fail('read-only followup inspection invoked an effect')
    observed_followup_case.c.model.complete = forbidden
    observed_followup_case.coordinator.prepare = forbidden
    lane.queue.enqueue = forbidden
    lane.adapter.prepare_reply = forbidden
    lane.adapter.submit_reply = forbidden
    # inspection_lane already forbids claim_ui_candidate, request and record_ui_plan.
    return SimpleNamespace(value=observed_followup_case, lane=lane, store=store,
        state=state, saved=saved, events=events, code=request.param)


def test_ui_hold_inspection_is_read_only_and_projects_only_fixed_counts(ui_observed_case):
    c = ui_observed_case
    before = deepcopy(c.saved)
    def read(*args, **kwargs):
        c.events.append('read')
        c.lane.adapter.last_followup_diagnostic = {'reason_code': 'matched', 'counts': {
            'root_count': 1, 'prior_reply_count': 1, 'followup_candidate_count': 1,
            'body_candidate_count': 1, 'more_count': 0, 'selection_count': True,
            'loaded_row_count': 1000001, 'unsupported_more_count': -1, 'raw': 'DO-NOT-SAVE'}}
        c.lane.adapter.last_original_lookup = {
            'candidate_count': 1, 'final_verified': 1, 'checked_count': True, 'raw': 'DO-NOT-SAVE'}
        return deepcopy(c.value.detail)
    c.lane.adapter.read_followup_reply = read
    result = c.lane.inspect_followup_source(c.saved['source_id'])
    assert result == {'source_id': c.saved['source_id'], 'status': 'followup_source_inspected',
        'dispatchable': False, 'matched': True}
    assert c.saved == before and c.events == ['fixedB', 'read']
    diagnostic = c.lane.last_native_recovery_diagnostic
    assert diagnostic['code'] == 'matched'
    assert diagnostic['followup_counts'] == {'root_count': 1, 'prior_reply_count': 1,
        'followup_candidate_count': 1, 'body_candidate_count': 1, 'more_count': 0}
    assert diagnostic['lookup_counts'] == {'candidate_count': 1, 'final_verified': 1}
    assert 'DO-NOT-SAVE' not in str(diagnostic)
    assert c.value.c.model.requests == [] and not c.value.lane.pending


def test_ui_hold_request_records_once_audit_and_preserves_attempts_and_immutable_facts(ui_persisted_case):
    c = ui_persisted_case
    before, original_facts = row(c.store, c.source), facts(c.store)
    result = request_ui_review(c.values)
    after = row(c.store, c.source)
    assert result == {'source_id': c.source, 'status': 'followup_ui_review_requested',
        'attempts_preserved': 1, 'maximum_requests': 1, 'audit_recorded': True, 'dispatchable': False}
    assert after['state'] == 'fresh' and after['attempts'] == 1
    for key in ('candidate', 'immutable_hash', 'historical', 'has_at', 'has_reply',
                'video_id', 'cid', 'identity_revision', 'first_seen', 'last_seen'):
        assert after[key] == before[key]
    assert after['plan'] is after['task_id'] is after['reference_id'] is None
    assert after['claim_token'] is after['claim_owner'] is after['lease_until'] is None
    [audit] = audits(c.store, c.source)
    assert audit['old_hold_code'] == c.code and audit['old_attempts'] == 1
    assert audit['old_claim_sha256'] == hashlib.sha256(c.first['claim_token'].encode()).hexdigest()
    assert audit['immutable_hash'] == before['immutable_hash'] and audit['request_one_count'] == 1
    assert audit['permission_scope'] == 'fresh_ui_read_only'
    assert c.first['claim_token'] not in json.dumps(audit)
    assert facts(c.store) == original_facts
    assert c.store.check_candidate(c.source, c.first['claim_token'])['valid'] is False
    second = c.store.claim_ui_candidate('synthetic-second-followup-read')
    assert second['source_id'] == c.source and second['claim_token'] != c.first['claim_token']
    assert row(c.store, c.source)['attempts'] == 2
    c.store.hold(c.source, second['claim_token'], c.code)
    held_again = row(c.store, c.source)
    with pytest.raises(NativeStoreError, match='already_requested'):
        request_ui_review(c.values)
    assert c.store.claim_ui_candidate('forbidden-third-read') is None
    assert row(c.store, c.source) == held_again and len(audits(c.store, c.source)) == 1
    assert facts(c.store) == original_facts


def test_ui_hold_failed_inspection_keeps_row_and_only_fixed_diagnostic_counts(ui_observed_case):
    c = ui_observed_case
    before = deepcopy(c.saved)
    def failed(*args, **kwargs):
        c.events.append('read')
        c.lane.adapter.last_followup_diagnostic = {'reason_code': 'followup_source_ambiguous',
            'counts': {'root_count': 1, 'followup_candidate_count': 2,
                'selection_count': True, 'raw': 'DO-NOT-SAVE'}}
        c.lane.adapter.last_original_lookup = {'candidate_count': 1, 'checked_count': True, 'raw': 'DO-NOT-SAVE'}
        raise ReceiverDetailError('DO-NOT-SAVE', {})
    c.lane.adapter.read_followup_reply = failed
    assert c.lane.inspect_followup_source(c.saved['source_id'])['matched'] is False
    diagnostic = c.lane.last_native_recovery_diagnostic
    assert diagnostic['code'] == 'followup_source_ambiguous'
    assert diagnostic['followup_counts'] == {'root_count': 1, 'followup_candidate_count': 2}
    assert diagnostic['lookup_counts'] == {'candidate_count': 1}
    assert 'DO-NOT-SAVE' not in str(diagnostic)
    assert c.saved == before and c.events == ['fixedB', 'read']
    assert c.value.c.model.requests == [] and not c.value.lane.pending


def test_ui_hold_request_keeps_all_existing_outcome_and_source_fences(ui_persisted_case):
    c = ui_persisted_case
    original = row(c.store, c.source)
    # Each refusal is restored before checking the next independent fence.
    for column, value in (
        ('state', 'admitted'), ('state', 'claimed'), ('state', 'planned'), ('state', 'held_conflict'),
        ('historical', 1), ('attempts', 0), ('attempts', 2), ('has_at', 1), ('has_reply', 0),
        ('plan', '{}'), ('task_id', 'existing-effect'), ('reference_id', 'existing-reference'),
        ('hold_code', 'native_unknown_lease'), ('hold_code', 'native_reply_not_matched'),
        ('hold_code', 'native_visibility_ui_unverified'), ('hold_code', 'native_prequeue_intervention'),
        ('hold_code', 'unrecognized_outcome'), ('hold_code', None),
    ):
        with c.store._connection(write=True) as db:
            db.execute('UPDATE sources SET '+column+'=? WHERE source_id=?', (value, c.source))
        before = row(c.store, c.source)
        with pytest.raises(NativeStoreError):
            request_ui_review(c.values)
        assert row(c.store, c.source) == before, (column, value)
        assert audits(c.store, c.source) == []
        with c.store._connection(write=True) as db:
            db.execute('UPDATE sources SET '+column+'=? WHERE source_id=?', (original[column], c.source))
    assert row(c.store, c.source) == original


@pytest.mark.parametrize('problem', ('revision', 'conflict'))
def test_ui_hold_request_still_requires_current_pinned_identity(ui_persisted_case, problem):
    c = ui_persisted_case
    with c.store._connection(write=True) as db:
        if problem == 'revision':
            db.execute('UPDATE identities SET revision=revision+1')
        else:
            db.execute("UPDATE identities SET state='conflict'")
    before = row(c.store, c.source)
    with pytest.raises(NativeStoreError):
        request_ui_review(c.values)
    assert row(c.store, c.source) == before and audits(c.store, c.source) == []


def test_ui_hold_lane_rejects_other_outcomes_history_effects_and_identity_before_review(ui_observed_case):
    c = ui_observed_case
    original = deepcopy(c.saved)
    for column, value in (
        ('state', 'admitted'), ('hold_code', 'native_unknown_lease'),
        ('hold_code', 'native_reply_not_matched'), ('hold_code', 'unrecognized_outcome'),
        ('historical', True), ('plan', {}), ('task_id', 'existing-task'),
        ('reference_id', 'existing-reference'),
    ):
        c.saved.clear(); c.saved.update(deepcopy(original)); c.saved[column] = value
        before = deepcopy(c.saved)
        with pytest.raises(NativeLaneError):
            c.lane.request_followup_review(c.saved['source_id'])
        with pytest.raises(NativeLaneError):
            c.lane.inspect_followup_source(c.saved['source_id'])
        assert c.saved == before and 'read' not in c.events
    c.saved.clear(); c.saved.update(original)
    c.lane._visibility_scope_current = lambda candidate: False
    with pytest.raises(NativeLaneError):
        c.lane.request_followup_review(c.saved['source_id'])
    with pytest.raises(NativeLaneError):
        c.lane.inspect_followup_source(c.saved['source_id'])
    assert c.saved == original and 'read' not in c.events and not c.value.c.model.requests


def test_ui_hold_without_exactly_one_anchor_never_reads_requests_or_reuses_old_counts(ui_observed_case):
    c = ui_observed_case
    before = deepcopy(c.saved)
    one = deepcopy(c.value.anchors[0])
    second = deepcopy(one); second['root_task_id'] = '3'*32; second['root_agent_task_id'] = '4'*32
    c.lane.adapter.last_followup_diagnostic = {'reason_code': 'matched', 'counts': {'root_count': 1}}
    c.lane.adapter.last_original_lookup = {'candidate_count': 1}
    for anchors in ([], [one, second]):
        c.value.anchors[:] = deepcopy(anchors)
        with pytest.raises(NativeLaneError):
            c.lane.request_followup_review(c.saved['source_id'])
        result = c.lane.inspect_followup_source(c.saved['source_id'])
        assert result['matched'] is False and result['dispatchable'] is False
        diagnostic = c.lane.last_native_recovery_diagnostic
        assert diagnostic['code'] == 'native_reply_parent_unresolved'
        assert not diagnostic.get('followup_counts') and not diagnostic.get('lookup_counts')
        assert c.saved == before and 'read' not in c.events
    assert not c.value.c.model.requests and not c.value.lane.pending


@pytest.mark.parametrize('operation', ('request_followup_ui_review', 'inspect_thread_followup'))
def test_ui_hold_owner_operations_keep_pause_current_run_and_busy_gates(recovery_owner_case, operation):
    c = recovery_owner_case
    calls = []
    c.lane.request_followup_review = lambda source: calls.append(source)
    c.lane.inspect_followup_source = lambda source: calls.append(source)
    initial = dict(c.state)
    for block in ('running', 'run_changed', 'manual', 'worker', 'effects', 'hold'):
        c.state.clear(); c.state.update(initial)
        c.pending[0] = False; c.counts.clear()
        for flag in c.flags: flag.clear()
        if block == 'running': c.state['desired_state'] = 'running'
        elif block == 'run_changed': c.state['run_id'] = 'b'*32
        elif block == 'manual': c.pending[0] = True
        elif block == 'worker': c.flags[2].set()
        elif block == 'effects': c.counts['publishing'] = 1
        else: c.flags[0].set()
        assert c.owner(operation, SOURCE)['ok'] is False, block
        assert calls == []
