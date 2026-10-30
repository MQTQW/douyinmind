"""Independent once-only Source recovery; only synthetic temporary native facts.

The private issuer stands in for the Lane's separately tested complete audit.
These tests never assert that empty Source links alone prove no external effects.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.assistant.native_source_store import (NativeSourceStore, NativeStoreError,
    _FollowupNoEffectsPermit, _followup_no_effects_permit, _dump, _digest)
from tests.test_native_source_followup_review import (
    case as first_review_case, reply_case, request, row, facts,
)


@pytest.fixture
def expired(first_review_case):
    store, clock, source, _ = first_review_case
    request(first_review_case)
    claim=store.claim_ui_candidate('synthetic-second-owner',lease_seconds=300)
    clock[0]=row(store,source)['lease_until']+1
    store.set_verified()
    assert store.claim_ui_candidate('normal-expiry-sweep') is None
    assert row(store,source)['state']=='held' and row(store,source)['hold_code']=='native_unknown_lease'
    return store,clock,source,claim


def review(clock, **changes):
    return {'operation':'owner_fresh_followup_no_effects_review','reviewed_at':clock[0],
        'root_task_id':'1'*32,'root_agent_task_id':'2'*32,'ui_reference_id':'reference:synthetic-followup',
        'ui_digest':'3'*64,'cross_ledger_digest':'4'*64,
        'operator_reason':'explicit_operator_followup_no_effects_recovery',**changes}


def prepared(expired):
    store,clock,source,_=expired
    snapshot=store.followup_no_effects_review_snapshot(source)
    value=review(clock)
    return snapshot,value,_followup_no_effects_permit(store,snapshot,value)


def saved_audits(store,source):
    with store._connection() as db:
        return [json.loads(item[0]) for item in db.execute('SELECT value FROM meta WHERE key=?',
            ('followup_no_effects_review_v1:'+source,))]


def test_new_once_capability_preserves_review_and_old_claim_until_normal_claim_three(expired,first_review_case):
    store,clock,source,old_claim=expired
    before=row(store,source);original_facts=facts(store)
    with store._connection() as db:
        old_audit=db.execute('SELECT value FROM meta WHERE key=?',('thread_followup_review_v1:'+source,)).fetchone()[0]
    db_bytes=store.path.read_bytes()
    snapshot,value,permit=prepared(expired)
    assert store.path.read_bytes()==db_bytes
    result=store._commit_followup_no_effects_review(permit)
    assert result=={'source_id':source,'status':'followup_no_effects_review_queued','attempts_preserved':2,'dispatchable':False}
    after=row(store,source)
    assert after=={**before,'state':'fresh','hold_code':None}
    assert after['attempts']==2 and after['claim_token']==old_claim['claim_token']
    assert after['lease_until']==before['lease_until'] and facts(store)==original_facts
    assert store.check_candidate(source,old_claim['claim_token'])['valid'] is False
    [audit]=saved_audits(store,source)
    assert audit['maximum_requests']==1 and audit['old_state']=='held' and audit['old_hold_code']=='native_unknown_lease'
    assert audit['old_attempts']==audit['attempts_preserved']==2
    assert audit['old_lease_until']==before['lease_until'] and audit['immutable_hash']==before['immutable_hash']
    assert audit['old_claim_sha256']==hashlib.sha256(old_claim['claim_token'].encode()).hexdigest()
    assert old_claim['claim_token'] not in json.dumps(audit)
    with store._connection() as db:
        assert db.execute('SELECT value FROM meta WHERE key=?',('thread_followup_review_v1:'+source,)).fetchone()[0]==old_audit
    with pytest.raises(NativeStoreError,match='already_requested'):
        request(first_review_case)
    third=store.claim_ui_candidate('normal-third-read',lease_seconds=300)
    assert third['source_id']==source and third['claim_token']!=old_claim['claim_token']
    assert row(store,source)['attempts']==3 and row(store,source)['plan'] is None
    assert row(store,source)['task_id'] is row(store,source)['reference_id'] is None
    with pytest.raises(NativeStoreError,match='already_used'):
        store._commit_followup_no_effects_review(permit)
    with pytest.raises(NativeStoreError,match='already_used'):
        store.followup_no_effects_review_snapshot(source)
    assert len(saved_audits(store,source))==1


@pytest.mark.parametrize('column,value',[
    ('state','fresh'),('state','claimed'),('state','planned'),('state','admitted'),
    ('hold_code','native_ui_unverified'),('historical',1),('attempts',1),('attempts',3),
    ('has_at',1),('has_reply',0),('plan','{}'),('task_id','effect'),('reference_id','effect'),
    ('lease_until',None),('lease_until',9999.0),('lease_until',0.0),('claim_token',None),('claim_owner',None),
    ('immutable_hash','broken'),('identity_revision','999'),('first_seen',103.0),('last_seen',9999.0),
])
def test_only_exact_expired_unplanned_attempt_two_is_eligible(expired,column,value):
    store,clock,source,_=expired
    with store._connection(write=True) as db:
        db.execute('UPDATE sources SET '+column+'=? WHERE source_id=?',(value,source))
    before=row(store,source)
    with pytest.raises(NativeStoreError):
        store.followup_no_effects_review_snapshot(source)
    assert row(store,source)==before and saved_audits(store,source)==[]


@pytest.mark.parametrize('key,value',[
    ('version',True),('maximum_requests',2),('request_one_count',True),('old_attempts',2),
    ('immutable_hash','0'*64),('source_first_seen',1.0),('operator_reason','untrusted'),
    ('permission_scope','execute'),('hold_timestamp_was_not_recorded',False),
])
def test_original_review_audit_is_exact_and_not_a_caller_no_effects_claim(expired,key,value):
    store,clock,source,_=expired
    with store._connection(write=True) as db:
        prior=json.loads(db.execute('SELECT value FROM meta WHERE key=?',('thread_followup_review_v1:'+source,)).fetchone()[0])
        prior[key]=value
        db.execute('UPDATE meta SET value=? WHERE key=?',(_dump(prior),'thread_followup_review_v1:'+source))
    with pytest.raises(NativeStoreError):store.followup_no_effects_review_snapshot(source)
    assert saved_audits(store,source)==[]


@pytest.mark.parametrize('marker',['ui_recheck_v1:','ui_read_fix_recheck_v1:','legacy_prequeue_review_v1:',
    'prequeue_resume_v1:','prequeue_intervention_v1:','visibility_hint_admission_v1:','future_unknown_recovery:'])
def test_other_recovery_markers_cannot_be_chained_into_the_new_capability(expired,marker):
    store,clock,source,_=expired
    with store._connection(write=True) as db:db.execute('INSERT INTO meta VALUES(?,?)',(marker+source,'{}'))
    with pytest.raises(NativeStoreError):store.followup_no_effects_review_snapshot(source)
    assert saved_audits(store,source)==[]


@pytest.mark.parametrize('problem',['missing_prior','missing_observation','wrong_channel','wrong_notice_ref',
    'armed_empty','armed_after_source','armed_nan','paused','stale_identity','pin_conflict','bad_native_type'])
def test_strict_native_facts_arming_and_current_identity_are_reused(expired,problem):
    store,clock,source,_=expired
    if problem=='paused':store.pause('synthetic_unavailable')
    elif problem=='stale_identity':clock[0]+=121
    else:
        with store._connection(write=True) as db:
            if problem=='missing_prior':db.execute('DELETE FROM meta WHERE key=?',('thread_followup_review_v1:'+source,))
            elif problem=='missing_observation':db.execute('DELETE FROM observations WHERE source_id=?',(source,))
            elif problem=='wrong_channel':db.execute('UPDATE observations SET channel=9 WHERE source_id=?',(source,))
            elif problem=='wrong_notice_ref':db.execute('UPDATE notice_refs SET notice_type=45 WHERE source_id=?',(source,))
            elif problem.startswith('armed_'):
                db.execute("UPDATE meta SET value=? WHERE key='armed_at'",({'armed_empty':'','armed_after_source':'102','armed_nan':'NaN'}[problem],))
            elif problem=='pin_conflict':db.execute("UPDATE identities SET state='conflict'")
            else:
                candidate=json.loads(db.execute('SELECT candidate FROM sources WHERE source_id=?',(source,)).fetchone()[0])
                candidate['notice_type']=True
                db.execute('UPDATE sources SET candidate=? WHERE source_id=?',(_dump(candidate),source))
    with pytest.raises(NativeStoreError):store.followup_no_effects_review_snapshot(source)
    assert saved_audits(store,source)==[]


@pytest.mark.parametrize('target',['snapshot','review','seal','store'])
def test_private_permit_cannot_be_mutated_after_the_lane_issues_it(expired,target):
    store,clock,source,_=expired
    snapshot,value,permit=prepared(expired)
    if target=='snapshot':permit.snapshot['attempts']=1
    elif target=='review':permit.review['cross_ledger_digest']='f'*64
    elif target=='seal':permit._seal=b'forged'
    else:permit.store=SimpleNamespace()
    before=row(store,source)
    with pytest.raises(NativeStoreError,match='required'):store._commit_followup_no_effects_review(permit)
    assert row(store,source)==before and saved_audits(store,source)==[]


def test_no_json_flag_or_public_constructor_can_forge_the_private_permit(expired):
    store,clock,source,_=expired
    snapshot,value,_=prepared(expired)
    with pytest.raises(NativeStoreError):_FollowupNoEffectsPermit(store,snapshot,value)
    with pytest.raises(NativeStoreError):store._commit_followup_no_effects_review({'snapshot':snapshot,'review':value,'no_effects':True})
    with pytest.raises(NativeStoreError):store._commit_followup_no_effects_review(_FollowupNoEffectsPermit.__new__(_FollowupNoEffectsPermit))
    assert saved_audits(store,source)==[]


def test_changed_snapshot_or_review_shape_is_not_external_proof(expired):
    store,clock,source,_=expired
    snapshot,value,_=prepared(expired)
    wrong=deepcopy(snapshot);wrong['claim_fingerprint']='0'*64
    with pytest.raises(NativeStoreError,match='scope_changed'):
        store._commit_followup_no_effects_review(_followup_no_effects_permit(store,wrong,value))
    for changes in ({'no_effects':True},{'operator_reason':'untrusted'},{'root_task_id':'invalid'},
                    {'ui_reference_id':'https://outside.invalid'},{'reviewed_at':True},{'ui_digest':'broken'}):
        with pytest.raises(NativeStoreError,match='required'):
            _followup_no_effects_permit(store,snapshot,{**value,**changes})
    assert saved_audits(store,source)==[]


def test_input_alias_mutation_does_not_rewrite_the_frozen_private_review(expired):
    store,clock,source,_=expired
    snapshot,value,permit=prepared(expired)
    snapshot['attempts']=99;value['operator_reason']='forged'
    store._commit_followup_no_effects_review(permit)
    [audit]=saved_audits(store,source)
    assert audit['old_attempts']==2 and audit['review']['operator_reason']=='explicit_operator_followup_no_effects_recovery'


def test_source_change_or_expired_ui_review_cannot_commit_a_stale_permit(expired):
    store,clock,source,_=expired
    snapshot,value,permit=prepared(expired)
    with store._connection(write=True) as db:db.execute('UPDATE sources SET last_seen=last_seen+0.5 WHERE source_id=?',(source,))
    with pytest.raises(NativeStoreError,match='scope_changed'):store._commit_followup_no_effects_review(permit)
    snapshot,value,permit=prepared(expired)
    clock[0]+=121;store.set_verified()
    with pytest.raises(NativeStoreError,match='expired'):store._commit_followup_no_effects_review(permit)
    assert saved_audits(store,source)==[] and row(store,source)['state']=='held'


def test_two_store_instances_can_commit_only_one_independent_recovery(expired):
    store,clock,source,_=expired
    other=NativeSourceStore(store.path,store.receiver_uid,store.receiver_profile_url,clock=lambda:clock[0])
    permits=[_followup_no_effects_permit(item,item.followup_no_effects_review_snapshot(source),review(clock)) for item in (store,other)]
    def commit(pair):
        item,permit=pair
        try:return item._commit_followup_no_effects_review(permit)['status']
        except NativeStoreError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(commit,zip((store,other),permits)))
    assert results.count('followup_no_effects_review_queued')==1
    assert results.count('native_followup_no_effects_review_already_used')==1
    assert len(saved_audits(store,source))==1 and row(store,source)['attempts']==2


def test_new_audit_and_fresh_transition_roll_back_atomically(expired):
    store,clock,source,_=expired
    snapshot,value,permit=prepared(expired)
    with store._connection(write=True) as db:
        db.execute("CREATE TRIGGER refuse_new_fresh BEFORE UPDATE ON sources WHEN NEW.state='fresh' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    before=row(store,source)
    with pytest.raises(Exception):store._commit_followup_no_effects_review(permit)
    assert row(store,source)==before and saved_audits(store,source)==[]


def test_same_rows_in_a_replaced_source_file_do_not_inherit_a_prior_permit(expired):
    store,clock,source,_=expired
    snapshot,value,permit=prepared(expired)
    replacement=store.path.with_suffix('.replacement.sqlite3')
    replacement.write_bytes(store.path.read_bytes())
    os.replace(replacement,store.path)
    with pytest.raises(NativeStoreError):store._commit_followup_no_effects_review(permit)
    assert saved_audits(store,source)==[] and row(store,source)['state']=='held'
