"""Explicit prequeue UI recheck only, synthetic native facts/temp owned DB."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from app.assistant.native_source_store import NativeSourceStore, NativeStoreError
from tests.test_assistant_native_receiver import B, PROFILE_B, page, project, reply, at


REASON = 'explicit_operator_choice_ui_recheck'
CODE = 'native_reply_not_matched'


@pytest.fixture
def held(tmp_path):
    clock = [100.0]
    store = NativeSourceStore(tmp_path/'sources.sqlite3', B, PROFILE_B, create=True, clock=lambda: clock[0])
    for channel, group in ((9, '601'), (7, '2')):
        store.record_page(store.prepare_channel(channel, group), project(page(), channel),
            next_cursor=('0', '0'), has_more=False)
    assert store.arm_if_ready(True)
    clock[0] = 102.0; store.set_verified()
    rows = store.record_page(store.prepare_channel(7, '2'), project(page([reply(created='101')]), 7),
        next_cursor=('0', '0'), has_more=False, allow_live=True)
    source_id = rows[0]['source_id']
    claim = store.claim_ui_candidate('synthetic-first-read')
    store.hold(source_id, claim['claim_token'], CODE)
    return store, clock, source_id, claim


def request(store, source_id, **changes):
    options = {'expected_old_code': CODE, 'operator_reason': REASON}
    options.update(changes)
    return store.request_ui_recheck(source_id, **options)


def row(store, source_id):
    with store._connection() as db:
        return dict(db.execute('SELECT * FROM sources WHERE source_id=?', (source_id,)).fetchone())


def audits(store):
    with store._connection() as db:
        return [json.loads(value[0]) for value in db.execute("SELECT value FROM meta WHERE key LIKE 'ui_recheck_v1:%'")]


def test_recheck_is_one_atomic_permission_to_read_not_task_or_attempt_reset(held):
    store, clock, source_id, first = held
    before = row(store, source_id)
    result = request(store, source_id)
    after = row(store, source_id)
    assert result['dispatchable'] is False and result['maximum_requests'] == 1
    assert result['status'] == 'ui_recheck_requested'
    assert after['state'] == 'fresh' and after['attempts'] == before['attempts'] == 1
    for key in ('candidate', 'immutable_hash', 'historical', 'first_seen', 'last_seen', 'identity_revision', 'video_id', 'cid'):
        assert before[key] == after[key]
    assert after['claim_token'] is None and after['plan'] is None and after['task_id'] is None
    assert store.check_candidate(source_id, first['claim_token'])['valid'] is False
    audit = audits(store)
    assert len(audit) == 1 and audit[0]['old_hold_code'] == CODE and audit[0]['old_attempts'] == 1
    assert audit[0]['old_hold_at'] is None and audit[0]['hold_timestamp_was_not_recorded'] is True
    assert first['claim_token'] not in json.dumps(audit)
    again = store.claim_ui_candidate('synthetic-fresh-UI-read')
    assert again['claim_token'] != first['claim_token']
    assert row(store, source_id)['attempts'] == 2
    assert store.check_candidate(source_id, again['claim_token'])['valid'] is True


def test_restart_and_second_hold_never_reopen_second_operator_request(held):
    store, clock, source_id, first = held
    request(store, source_id)
    reopened = NativeSourceStore(store.path, B, PROFILE_B, clock=lambda: clock[0])
    next_claim = reopened.claim_ui_candidate('synthetic-recheck')
    reopened.hold(source_id, next_claim['claim_token'], CODE)
    with pytest.raises(NativeStoreError, match='native_recheck_already_requested'):
        request(reopened, source_id)
    assert len(audits(reopened)) == 1 and row(store, source_id)['attempts'] == 2


def test_old_claim_cannot_reapply_hold_after_explicit_recheck(held):
    store, clock, source_id, first = held
    request(store, source_id)
    with pytest.raises(NativeStoreError, match='native_claim_conflict'):
        store.hold(source_id, first['claim_token'], CODE)
    assert row(store, source_id)['state'] == 'fresh'
    assert len(audits(store)) == 1


def test_fixed_legacy_parent_hold_can_only_request_one_new_ui_read(held):
    store, clock, source_id, first = held
    parent_code = 'native_reply_parent_unresolved'
    store.hold(source_id, first['claim_token'], parent_code)
    before = row(store, source_id)
    assert store.claim_ui_candidate('no-automatic-release') is None
    result = request(store, source_id, expected_old_code=parent_code)
    after = row(store, source_id)
    assert result['dispatchable'] is False and after['state'] == 'fresh'
    assert after['attempts'] == before['attempts'] == 1
    for key in ('candidate', 'immutable_hash', 'historical', 'first_seen', 'last_seen', 'identity_revision'):
        assert before[key] == after[key]
    assert audits(store)[0]['old_hold_code'] == parent_code
    with pytest.raises(NativeStoreError, match='native_recheck_already_requested'):
        request(store, source_id, expected_old_code=parent_code)


def test_expected_parent_hold_cannot_release_an_actual_different_hold(held):
    store, clock, source_id, first = held
    before = row(store, source_id)
    with pytest.raises(NativeStoreError, match='native_recheck_not_allowed'):
        request(store, source_id, expected_old_code='native_reply_parent_unresolved')
    assert row(store, source_id) == before and audits(store) == []


@pytest.mark.parametrize('field,value', [('historical', 1), ('has_at', 1), ('task_id', 'already-spent')])
def test_parent_hold_never_reopens_history_mixed_roles_or_spent_effects(held, field, value):
    store, clock, source_id, first = held
    parent_code = 'native_reply_parent_unresolved'
    store.hold(source_id, first['claim_token'], parent_code)
    with store._connection(write=True) as db:
        db.execute('UPDATE sources SET '+field+'=? WHERE source_id=?', (value, source_id))
    before = row(store, source_id)
    with pytest.raises(NativeStoreError, match='native_recheck_not_allowed'):
        request(store, source_id, expected_old_code=parent_code)
    assert row(store, source_id) == before and audits(store) == []


def test_two_independent_store_actors_get_exactly_one_success(held):
    store, clock, source_id, first = held
    other = NativeSourceStore(store.path, B, PROFILE_B, clock=lambda: clock[0])
    def invoke(value):
        try: return request(value, source_id)['status']
        except NativeStoreError as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(invoke, [store, other]))
    assert sorted(values) == ['native_recheck_already_requested', 'ui_recheck_requested']
    assert len(audits(store)) == 1 and row(store, source_id)['attempts'] == 1


@pytest.mark.parametrize('column,value', [
    ('state', 'admitted'), ('state', 'planned'), ('state', 'held_conflict'), ('state', 'historical'),
    ('historical', 1), ('has_at', 1), ('has_reply', 0), ('attempts', 0), ('attempts', 2),
    ('hold_code', 'native_unknown_lease'), ('hold_code', 'native_identity_unverified'),
    ('plan', '{}'), ('task_id', 'possibly-spent-task'), ('reference_id', 'possibly-spent-reference'),
])
def test_no_spent_other_hold_mixed_role_or_history_can_be_rechecked(held, column, value):
    store, clock, source_id, first = held
    with store._connection(write=True) as db:
        db.execute('UPDATE sources SET '+column+'=? WHERE source_id=?', (value, source_id))
    before = row(store, source_id)
    with pytest.raises(NativeStoreError, match='native_recheck_not_allowed'): request(store, source_id)
    assert row(store, source_id) == before and audits(store) == []


@pytest.mark.parametrize('state', ['stale', 'paused', 'nan', 'future', 'identity_revision', 'identity_conflict'])
def test_fresh_native_identity_and_pinned_source_are_required(held, state):
    store, clock, source_id, first = held
    if state == 'stale': clock[0] += 121
    elif state == 'paused': store.pause('network_timeout')
    else:
        with store._connection(write=True) as db:
            if state in {'nan', 'future'}:
                db.execute("UPDATE meta SET value=? WHERE key='verified_at'", ('NaN' if state == 'nan' else str(clock[0]+6),))
            elif state == 'identity_revision': db.execute('UPDATE identities SET revision=revision+1')
            else: db.execute("UPDATE identities SET state='conflict'")
    with pytest.raises(NativeStoreError, match='native_identity_unverified'): request(store, source_id)
    assert row(store, source_id)['state'] == 'held' and audits(store) == []


def test_semantic_competitor_requires_review_not_recheck(held):
    store, clock, source_id, first = held
    store.record_page(store.prepare_channel(7, '2'), project(page([reply(nid='1003', cid='2003', created='101')]), 7),
        next_cursor=('0', '0'), has_more=False, allow_live=True)
    with pytest.raises(NativeStoreError, match='native_semantic_conflict'): request(store, source_id)
    assert audits(store) == []


def test_recheck_audit_and_source_transition_roll_back_together(held):
    store, clock, source_id, first = held
    with store._connection(write=True) as db:
        db.execute("CREATE TRIGGER synthetic_recheck_abort BEFORE UPDATE ON sources WHEN NEW.state='fresh' BEGIN SELECT RAISE(ABORT,'synthetic_fault'); END")
    before = row(store, source_id)
    with pytest.raises(Exception): request(store, source_id)
    assert row(store, source_id) == before and audits(store) == []
    with store._connection(write=True) as db: db.execute('DROP TRIGGER synthetic_recheck_abort')
    assert request(store, source_id)['audit_recorded']


@pytest.mark.parametrize('options', [
    {'operator_reason': 'synthetic-secret-not-a-reason'}, {'operator_reason': None},
    {'expected_old_code': 'native_unknown_lease'}, {'expected_old_code': []},
])
def test_reason_and_old_hold_are_explicit_fixed_values_not_arbitrary_metadata(held, options):
    store, clock, source_id, first = held
    with pytest.raises(NativeStoreError, match='native_recheck_request_invalid'):
        request(store, source_id, **options)
    assert audits(store) == [] and row(store, source_id)['state'] == 'held'


def test_old_observation_and_cursor_rows_remain_unchanged(held):
    store, clock, source_id, first = held
    def facts():
        with store._connection() as db:
            return {table: [tuple(r) for r in db.execute('SELECT * FROM '+table)]
                    for table in ('observations', 'notice_refs', 'scan_pages', 'channels', 'identities')}
    before = deepcopy(facts())
    request(store, source_id)
    assert facts() == before
