"""Slow public UI must obtain fresh identity before durable admission.

Fake public pages/transport and temporary real product ledgers only.
"""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.assistant.native_receiver import NativeReceiverError
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_native_lane import native_system
from tests.test_assistant_choice_controller import case, root, publish_fact, Adapter


def aging_source(store, receiver):
    original = store.check_candidate
    calls = []
    store.pause = lambda code: calls.append(('pause', code))

    def check(source_id, token):
        result = original(source_id, token)
        if store.transport_fresh is not True:
            result['reason'] = 'native_identity_unverified'
        return result

    store.check_candidate = check
    store.candidate_identity_refresh_allowed = lambda *_: store.valid and not store.conflict

    def verify():
        calls.append(('fixed_identity_get',))
        store.transport_fresh = True
        return True

    receiver.reverify_identity_checkpoint = verify
    return calls


def test_slow_mention_rechecks_actual_identity_then_admits_same_scope(tmp_path):
    lane, store, receiver, adapter, queue, history, registry, migrated = native_system(tmp_path)
    calls = aging_source(store, receiver)
    lease = []
    original_claim = store.claim_ui_candidate
    store.claim_ui_candidate = lambda owner, lease_seconds: (lease.append(lease_seconds),
        original_claim(owner, lease_seconds))[1]
    adapter.before_return = lambda: setattr(store, 'transport_fresh', False)
    rows = lane.collect()
    assert lease == [300] and calls == [('fixed_identity_get',)]
    assert len(rows) == 1 and sum(queue.counts().values()) == 0
    task = queue.enqueue(**rows[0]); lane.admitted([task])
    assert store.state == 'admitted' and len(store.links) == 1
    assert history.task_scope(task['id']) is not None


@pytest.mark.parametrize('reason', ['conflict', 'claim_expired', 'changed_candidate'])
def test_slow_source_with_other_change_never_refreshes_or_enqueues(tmp_path, reason):
    lane, store, receiver, adapter, queue, history, registry, migrated = native_system(tmp_path)
    calls = aging_source(store, receiver)

    def change():
        store.transport_fresh = False
        if reason == 'conflict': store.conflict = True
        elif reason == 'claim_expired': store.candidate_identity_refresh_allowed = lambda *_: False
        else: store.candidate['content'] += ' changed'

    adapter.before_return = change
    assert lane.collect() == [] and calls == []
    assert sum(queue.counts().values()) == 0 and store.plan is None


def test_changed_scope_after_real_identity_check_still_cannot_admit(tmp_path):
    lane, store, receiver, adapter, queue, history, registry, migrated = native_system(tmp_path)
    calls = aging_source(store, receiver)
    adapter.before_return = lambda: setattr(store, 'transport_fresh', False)

    def verify():
        calls.append(('fixed_identity_get',)); store.transport_fresh = True
        store.candidate['video_id'] = '7999999000000000099'
        return True

    receiver.reverify_identity_checkpoint = verify
    assert lane.collect() == [] and calls == [('fixed_identity_get',)]
    assert sum(queue.counts().values()) == 0 and store.plan is None


def test_identity_challenge_remains_human_pause_without_admission(tmp_path):
    lane, store, receiver, adapter, queue, history, registry, migrated = native_system(tmp_path)
    calls = aging_source(store, receiver)
    adapter.before_return = lambda: setattr(store, 'transport_fresh', False)
    receiver.reverify_identity_checkpoint = lambda: (_ for _ in ()).throw(
        NativeReceiverError('verification_required'))
    with pytest.raises(ReceiverInterventionRequired): lane.collect()
    assert calls == [('pause', 'verification_required')]
    assert sum(queue.counts().values()) == 0 and store.plan is None


def test_identity_get_is_pinned_without_resetting_full_poll_budget(tmp_path):
    lane, store, receiver, adapter, queue, history, registry, migrated = native_system(tmp_path)
    calls = aging_source(store, receiver)
    adapter.before_return = lambda: setattr(store, 'transport_fresh', False)

    @contextmanager
    def cycle(*, reset_failures_on_success):
        assert reset_failures_on_success is False
        calls.append(('pin',))
        try: yield
        finally: calls.append(('unpin',))

    lane.session_lifecycle = SimpleNamespace(cycle=cycle)
    assert len(lane.collect()) == 1
    assert calls == [('pin',), ('fixed_identity_get',), ('unpin',)]


def test_choice_cannot_reserve_until_exact_true_source_checkpoint(case):
    task, session, _ = root(case); publish_fact(case, task, session)
    adapter = Adapter(case); seen = []
    for rejected in [False, 1]:
        assert case.controller.collect_pending(adapter, 'run', choice_id=session['choice_id'],
            source_checkpoint=lambda envelope: rejected) == []
        current = case.store.get(session['choice_id'])
        assert current['state'] == 'waiting_library' and current['selection_ref'] is None

    def verify(envelope):
        current = case.store.get(session['choice_id'])
        seen.append((current['state'], current['selection_ref']))
        return True

    rows = case.controller.collect_pending(adapter, 'run', choice_id=session['choice_id'], source_checkpoint=verify)
    assert seen == [('waiting_library', None)] and len(rows) == 1
    assert case.store.get(session['choice_id'])['selected_kb_id'] == 8


def test_identity_network_delay_does_not_relabel_old_choice_proof_fresh(case):
    task, session, _ = root(case); publish_fact(case, task, session)

    def delayed_verification(envelope):
        case.clock[0] += 121
        return True

    assert case.controller.collect_pending(Adapter(case), 'run', choice_id=session['choice_id'],
        source_checkpoint=delayed_verification) == []
    current = case.store.get(session['choice_id'])
    assert current['state'] == 'waiting_library' and current['selection_ref'] is None
    assert sum(case.queue.counts().values()) == 1
