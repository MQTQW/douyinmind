"""Isolated hint persistence/admission contracts; synthetic public facts only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3

import pytest

from app.assistant.native_source_store import NativeSourceStore, NativeStoreError
from app.assistant.native_visibility_hint_store import (
    NativeVisibilityHintStore, NativeVisibilityHintError, validate_review_hint,
    _VerifiedHintUIPlanPermit,
)
from tests.test_assistant_native_receiver import B, SEC_B, PROFILE_A, PROFILE_B, at, page, project


@pytest.fixture
def case(tmp_path):
    clock = [100.0]
    source = NativeSourceStore(tmp_path/'sources.sqlite3', B, PROFILE_B, create=True, clock=lambda: clock[0])
    # Pin an existing registered A using an old, different public instruction.
    source.record_page(source.prepare_channel(9, '601'), project(page([at(nid='1000', cid='2000', text='older public instruction')]), 9), next_cursor=('0', '0'), has_more=False)
    source.record_page(source.prepare_channel(7, '2'), project(page(), 7), next_cursor=('0', '0'), has_more=False)
    assert source.arm_if_ready(True)
    clock[0] = 102.0; source.set_verified()
    raw = at(nid='1009', cid='2009', created='101', text='@助手 请把这个视频存到测试知识库')
    hint = project(page([raw]), 9)['candidates'][0]
    hint.update(source_kind='native_visibility_review_hint', native_visibility='unknown', visible_category='other_int', hidden_category='int0')
    unknown = deepcopy(raw); unknown['at']['comment_status'] = 42
    projection = project(page([unknown]), 9)
    source.record_page(source.prepare_channel(9, '601'), projection, next_cursor=('0', '0'), has_more=False, allow_live=True)
    ledger = NativeVisibilityHintStore(tmp_path/'hints.sqlite3', B, PROFILE_B, source, create=True, clock=lambda: clock[0])
    hid = ledger.record_hints([hint], page_fingerprint=projection['fingerprint'])[0]
    return source, ledger, clock, hid, hint


def request(ledger, hid):
    return ledger.request_review(hid, operator_reason='explicit_operator_unknown_visibility_ui_review')


def claim(case):
    source, ledger, clock, hid, hint = case
    request(ledger, hid)
    return ledger.claim_review(hid, 'owner-run')


def plan_for(hint):
    envelope = {'source_mode': 'live_ui', 'reference_id': 'ui:actual-owner-observation',
        'sender_profile_url': hint['sender_profile_url'], 'receiver_profile_url': hint['receiver_profile_url'],
        'video_id': hint['video_id'], 'comment_text': hint['content'], 'native_comment_id': None}
    return {'task_id': None, 'reference_id': envelope['reference_id'], 'envelope': envelope,
        'thread': {'root_reference_id': envelope['reference_id']}, 'queue_item': {'envelope': deepcopy(envelope)},
        'native_correlation': {'source_native_comment_id': hint['native_comment_id'],
            'observed_native_comment_id': None, 'quality': 'semantic_only'}}


def original_notices(source):
    with source._connection() as db:
        return {'refs': [tuple(r) for r in db.execute('SELECT * FROM notice_refs ORDER BY notice_id')],
            'skips': [tuple(r) for r in db.execute('SELECT * FROM notice_skips ORDER BY notice_id,channel')]}


def test_record_and_restart_stay_historical_no_source_no_claim(case):
    source, ledger, clock, hid, hint = case
    assert ledger.get_hint(hid)['state'] == 'historical'
    assert ledger.claim_review(hid, 'owner-run') is None
    assert source.find_source_by_native_comment(B, hint['video_id'], hint['native_comment_id']) is None
    second = NativeVisibilityHintStore(ledger.path, B, PROFILE_B, source, clock=lambda: clock[0])
    assert second.get_hint(hid)['hint'] == hint
    assert second.list_hints('pending') == []
    assert second.record_hints([hint], page_fingerprint='a'*64) == [hid]
    assert second.get_hint(hid)['state'] == 'historical'


def test_real_pure_projector_output_matches_owned_ledger_contract(case):
    from app.assistant.native_events import project_ui_review_hints
    source, ledger, clock, hid, hint = case
    raw = at(nid='1009', cid='2009', created='101', text=hint['content'])
    raw['at']['comment_status'] = 42
    hints = project_ui_review_hints(page([raw]), 9, receiver_uid=B, receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile: profile == PROFILE_A)
    assert hints == [hint]
    assert ledger.record_hints(hints, page_fingerprint='b'*64) == [hid]
    assert ledger.get_hint(hid)['state'] == 'historical'


def test_explicit_request_is_once_even_after_restart(case):
    source, ledger, clock, hid, hint = case
    assert request(ledger, hid)['dispatchable'] is False
    second = NativeVisibilityHintStore(ledger.path, B, PROFILE_B, source, clock=lambda: clock[0])
    with pytest.raises(NativeVisibilityHintError, match='request_unavailable'): request(second, hid)
    assert second.get_hint(hid)['state'] == 'pending'


def test_two_owned_instances_cannot_double_request_or_claim(case):
    source, ledger, clock, hid, hint = case
    second = NativeVisibilityHintStore(ledger.path, B, PROFILE_B, source, clock=lambda: clock[0])
    def activate(instance):
        try: return request(instance, hid)['status']
        except NativeVisibilityHintError as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes = list(pool.map(activate, (ledger, second)))
    assert outcomes.count('visibility_review_requested') == 1
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes = list(pool.map(lambda s: s.claim_review(hid, 'owner-run'), (ledger, second)))
    assert sum(value is not None for value in outcomes) == 1
    assert ledger.get_hint(hid)['attempts'] == 1


def test_claim_is_only_normal_lookup_projection_raw_hint_stays_unknown(case):
    source, ledger, clock, hid, hint = case; reading = claim(case)
    candidate = reading['candidate']
    assert candidate['source_kind'] == 'native_notice_candidate'
    assert candidate['native_visibility'] == 'unknown' and candidate['dispatchable'] is False
    assert candidate['native_source_key'] == reading['source_id']
    assert candidate['hint_provenance']['original_source_kind'] == 'native_visibility_review_hint'
    assert ledger.get_hint(hid)['hint'] == hint
    assert ledger.check_claim(hid, reading['claim_token'])['candidate'] == candidate


def test_typed_plan_admission_preserves_skip_ref_and_uses_ordinary_final_link(case):
    source, ledger, clock, hid, hint = case; reading = claim(case); original = original_notices(source)
    plan = plan_for(hint)
    permit = ledger.mint_ui_plan_permit(hid, reading['claim_token'], plan)
    admitted = source.accept_verified_hint_ui_plan(permit)
    assert original_notices(source) == original
    assert source.get_source(admitted['source_id'])['state'] == 'planned'
    assert source.get_source(admitted['source_id'])['plan'] == plan
    assert source.claim_ui_candidate('other-owner') is None
    assert source.admitted_source_current(admitted['source_id']) is False
    assert ledger.get_hint(hid)['state'] == 'admitted'
    with pytest.raises(Exception): source.accept_verified_hint_ui_plan(permit)
    assert ledger.claim_review(hid, 'other-owner') is None
    with source._connection() as db:
        audit = json.loads(db.execute('SELECT value FROM meta WHERE key=?', ('visibility_hint_admission_v1:'+admitted['source_id'],)).fetchone()[0])
    assert audit['native_visibility'] == 'unknown' and audit['cross_database_atomic'] is False
    assert reading['claim_token'] not in json.dumps(audit)
    source.finish_ui_admission(admitted['source_id'], admitted['claim_token'], 'actual-enqueued-task', plan['reference_id'])
    assert source.admitted_source_current(admitted['source_id']) is True


@pytest.mark.parametrize('field,value', [('native_visibility', 'visible'), ('visible_category', 'int1'),
    ('hidden_category', 'int1'), ('dispatchable', True), ('notice_type', True), ('at_sub_type', True),
    ('parent_id_hint', '2'), ('created_time', None), ('created_time', '001'), ('native_comment_id', 2009),
    ('content', '\x00bad'), ('sender_profile_url', 'https://www.douyin.com/user/different')])
def test_hint_full_structural_contract_rejects_mutations(case, field, value):
    hint = deepcopy(case[4]); hint[field] = value
    with pytest.raises(NativeVisibilityHintError): validate_review_hint(hint)


@pytest.mark.parametrize('problem', ['stale', 'pause', 'pin', 'prearm', 'ref_linked', 'skip_changed', 'semantic'])
def test_explicit_review_requires_original_live_scope_not_diagnostic_flags(case, problem):
    source, ledger, clock, hid, hint = case
    if problem == 'stale': clock[0] += 121
    elif problem == 'pause': source.pause('verification_required')
    elif problem == 'prearm':
        hint['created_time'] = '90'
        with ledger._connection(write=True) as db:
            from app.assistant.native_visibility_hint_store import _dump, _digest
            db.execute('UPDATE hints SET hint=?,immutable_hash=? WHERE hint_id=?', (_dump(hint), _digest(hint), hid))
    else:
        with source._connection(write=True) as db:
            if problem == 'pin': db.execute("UPDATE identities SET state='conflict'")
            elif problem == 'ref_linked': db.execute("UPDATE notice_refs SET source_id=(SELECT source_id FROM sources LIMIT 1) WHERE notice_id=?", (hint['native_notice_id'],))
            elif problem == 'skip_changed': db.execute("UPDATE notice_skips SET reason='native_comment_unavailable'")
            else:
                existing = json.loads(db.execute('SELECT candidate FROM sources LIMIT 1').fetchone()[0]); existing['content'] = hint['content']
                db.execute('UPDATE sources SET candidate=?', (json.dumps(existing),))
    with pytest.raises(Exception): request(ledger, hid)
    assert ledger.get_hint(hid)['state'] == 'historical'


def test_expired_or_held_claim_cannot_be_reclaimed_or_minted(case):
    source, ledger, clock, hid, hint = case; reading = claim(case)
    clock[0] += 121; source.set_verified()
    assert ledger.check_claim(hid, reading['claim_token'])['valid'] is False
    assert ledger.claim_review(hid, 'new-owner') is None
    with pytest.raises(Exception): ledger.mint_ui_plan_permit(hid, reading['claim_token'], plan_for(hint))
    ledger.hold(hid, reading['claim_token'], 'native_visibility_review_expired')
    assert ledger.get_hint(hid)['state'] == 'held'
    with pytest.raises(Exception): request(ledger, hid)


def test_json_or_foreign_store_cannot_be_a_permit(case, tmp_path):
    source, ledger, clock, hid, hint = case; reading = claim(case)
    with pytest.raises(NativeStoreError): source.accept_verified_hint_ui_plan({'hint_id': hid, 'verified': True})
    with pytest.raises(NativeVisibilityHintError): _VerifiedHintUIPlanPermit(ledger, hid, reading['claim_token'], {}, plan_for(hint))
    second_source = NativeSourceStore(source.path, B, PROFILE_B, clock=lambda: clock[0])
    permit = ledger.mint_ui_plan_permit(hid, reading['claim_token'], plan_for(hint))
    with pytest.raises(NativeStoreError): second_source.accept_verified_hint_ui_plan(permit)
    assert ledger.get_hint(hid)['state'] == 'claimed'


def test_changed_hint_holds_instead_of_overwriting_original_content(case):
    source, ledger, clock, hid, hint = case
    changed = deepcopy(hint); changed['content'] = 'different content'
    ledger.record_hints([changed], page_fingerprint='a'*64)
    assert ledger.get_hint(hid)['state'] == 'held'
    assert ledger.get_hint(hid)['hint'] == hint
    with pytest.raises(Exception): request(ledger, hid)


def test_wrong_ui_scope_spends_claim_without_creating_source(case):
    source, ledger, clock, hid, hint = case; reading = claim(case)
    plan = plan_for(hint); plan['envelope']['video_id'] = '9999'; plan['queue_item']['envelope'] = deepcopy(plan['envelope'])
    permit = ledger.mint_ui_plan_permit(hid, reading['claim_token'], plan)
    with pytest.raises(NativeStoreError): source.accept_verified_hint_ui_plan(permit)
    assert ledger.get_hint(hid)['state'] == 'committing'
    assert ledger.claim_review(hid, 'new-owner') is None
    assert source.find_source_by_native_comment(B, hint['video_id'], hint['native_comment_id']) is None


def test_source_transaction_failure_rolls_back_source_and_audit_but_never_replays_hint(case):
    source, ledger, clock, hid, hint = case; reading = claim(case); original = original_notices(source)
    with source._connection(write=True) as db:
        db.execute("CREATE TRIGGER fixture_no_source BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT,'fixture'); END")
    permit = ledger.mint_ui_plan_permit(hid, reading['claim_token'], plan_for(hint))
    with pytest.raises(sqlite3.IntegrityError): source.accept_verified_hint_ui_plan(permit)
    assert ledger.get_hint(hid)['state'] == 'committing'
    assert original_notices(source) == original
    assert source.find_source_by_native_comment(B, hint['video_id'], hint['native_comment_id']) is None
    with source._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'visibility_hint_admission_v1:%'").fetchone()[0] == 0
    assert ledger.claim_review(hid, 'new-owner') is None


def test_marker_mismatch_fails_without_writing_foreign_database(case):
    source, ledger, clock, hid, hint = case
    marker = json.loads(ledger._marker.read_text(encoding='utf-8')); marker['owner'] = 'f'*32
    ledger._marker.write_text(json.dumps(marker), encoding='utf-8')
    original = ledger.path.read_bytes()
    with pytest.raises(Exception): ledger.record_hints([hint], page_fingerprint='a'*64)
    assert ledger.path.read_bytes() == original
