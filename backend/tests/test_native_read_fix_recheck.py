"""Bounded corrective read for a never-admitted source, temp stores only."""
import json
import pytest
from app.assistant.native_source_store import NativeStoreError
from tests.test_assistant_native_ui_recheck import held, request, row, audits


def second_hold(held):
    store, clock, sid, original = held
    request(store, sid)
    claim = store.claim_ui_candidate('first-explicit-read')
    store.hold(sid, claim['claim_token'], 'native_reply_not_matched')
    return store, clock, sid


def corrected(store, sid):
    return store.request_ui_recheck_after_read_fix(sid,
        expected_old_code='native_reply_not_matched', operator_reason='explicit_operator_verified_read_fix')


def test_corrective_read_preserves_prior_audit_attempts_and_original_source(held):
    store, clock, sid = second_hold(held)
    prior = audits(store); before = row(store, sid)
    result = corrected(store, sid); after = row(store, sid)
    assert result['dispatchable'] is False and after['state'] == 'fresh'
    assert after['attempts'] == before['attempts'] == 2 and audits(store) == prior
    for key in ('candidate', 'immutable_hash', 'historical', 'first_seen', 'last_seen', 'identity_revision'):
        assert before[key] == after[key]
    assert store.claim_ui_candidate('corrected-read')['source_id'] == sid
    assert row(store, sid)['attempts'] == 3
    with pytest.raises(NativeStoreError, match='native_recheck_already_requested'): corrected(store, sid)


def test_prior_original_one_shot_audit_is_required(held):
    store, clock, sid, first = held
    before = row(store, sid)
    with pytest.raises(NativeStoreError, match='native_recheck_not_allowed'): corrected(store, sid)
    assert row(store, sid) == before


@pytest.mark.parametrize('field,value', [('historical', 1), ('has_at', 1),
    ('task_id', 'already-spent'), ('reference_id', 'possibly-queued'), ('plan', '{}'), ('attempts', 3)])
def test_no_history_mixed_roles_plan_or_possible_effect_can_reopen(held, field, value):
    store, clock, sid = second_hold(held)
    with store._connection(write=True) as db:
        db.execute('UPDATE sources SET '+field+'=? WHERE source_id=?', (value, sid))
    before = row(store, sid)
    with pytest.raises(NativeStoreError, match='native_recheck_not_allowed'): corrected(store, sid)
    assert row(store, sid) == before


def test_stale_or_paused_identity_cannot_be_relabelled_fresh(held):
    store, clock, sid = second_hold(held)
    clock[0] += 121
    before = row(store, sid)
    with pytest.raises(NativeStoreError, match='native_identity_unverified'): corrected(store, sid)
    assert row(store, sid) == before


def test_corrective_audit_and_state_change_are_atomic(held):
    store, clock, sid = second_hold(held)
    with store._connection(write=True) as db:
        db.execute("CREATE TRIGGER reject_corrective BEFORE UPDATE ON sources WHEN NEW.state='fresh' BEGIN SELECT RAISE(ABORT,'test interruption'); END")
    before = row(store, sid)
    with pytest.raises(Exception): corrected(store, sid)
    assert row(store, sid) == before
    with store._connection() as db:
        assert db.execute("SELECT count(*) FROM meta WHERE key LIKE 'ui_read_fix_recheck_v1:%'").fetchone()[0] == 0
