"""One explicit followup READ, synthetic native pages and temporary stores."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json

import pytest

from app.assistant.native_source_store import NativeSourceStore,NativeStoreError,_dump,_digest
from tests.test_assistant_native_ui_recheck import held as reply_case,row
from tests.test_assistant_native_receiver import B,PROFILE_B

REASON='explicit_operator_thread_followup_review'


@pytest.fixture
def case(reply_case):
    store,clock,source,claim=reply_case
    store.hold(source,claim['claim_token'],'native_reply_parent_unresolved')
    return store,clock,source,claim


def request(case,**kwargs):
    store,clock,source,claim=case
    return store.request_followup_review(source,operator_reason=kwargs.get('operator_reason',REASON))


def audits(store,source):
    with store._connection() as db:
        return [json.loads(r[0]) for r in db.execute('SELECT value FROM meta WHERE key=?',('thread_followup_review_v1:'+source,))]


def facts(store):
    with store._connection() as db:
        return {t:[tuple(r) for r in db.execute('SELECT * FROM '+t+' ORDER BY rowid')]
            for t in ('observations','notice_refs','notice_skips','channels','scan_pages','identities')}


def test_one_request_preserves_old_immutable_facts_and_attempt_then_claims_two(case):
    store,clock,source,first=case
    old=row(store,source);original=facts(store)
    result=request(case);updated=row(store,source)
    assert result=={'source_id':source,'status':'followup_ui_review_requested','attempts_preserved':1,
        'maximum_requests':1,'audit_recorded':True,'dispatchable':False}
    assert updated['state']=='fresh' and updated['attempts']==1
    for key in ('candidate','immutable_hash','historical','has_at','has_reply','video_id','cid','identity_revision','first_seen','last_seen'):
        assert old[key]==updated[key]
    assert updated['plan'] is updated['task_id'] is updated['reference_id'] is None
    assert updated['claim_token'] is updated['claim_owner'] is updated['lease_until'] is None
    [audit]=audits(store,source)
    assert audit['old_hold_code']=='native_reply_parent_unresolved' and audit['old_attempts']==1
    assert audit['old_claim_sha256']==hashlib.sha256(first['claim_token'].encode()).hexdigest()
    assert audit['immutable_hash']==old['immutable_hash'] and audit['request_one_count']==1
    assert first['claim_token'] not in json.dumps(audit)
    assert facts(store)==original and store.check_candidate(source,first['claim_token'])['valid'] is False
    with pytest.raises(NativeStoreError):store.hold(source,first['claim_token'],'native_reply_parent_unresolved')
    second=store.claim_ui_candidate('synthetic-second-read')
    assert second['source_id']==source and second['claim_token']!=first['claim_token']
    assert row(store,source)['attempts']==2 and store.check_candidate(source,second['claim_token'])['valid'] is True
    store.hold(source,second['claim_token'],'native_reply_parent_unresolved')
    with pytest.raises(NativeStoreError,match='already_requested'):request(case)
    assert store.claim_ui_candidate('no-third-read') is None and row(store,source)['attempts']==2


def test_restart_or_two_store_instances_cannot_issue_another_read(case):
    store,clock,source,first=case
    other=NativeSourceStore(store.path,B,PROFILE_B,clock=lambda:clock[0])
    def invoke(instance):
        try:return instance.request_followup_review(source,operator_reason=REASON)['status']
        except NativeStoreError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(invoke,(store,other)))
    assert result.count('followup_ui_review_requested')==1
    assert result.count('native_followup_review_already_requested')==1 and len(audits(store,source))==1
    assert row(store,source)['attempts']==1


@pytest.mark.parametrize('column,value',[
    ('state','admitted'),('state','claimed'),('state','planned'),('state','held_conflict'),('historical',1),
    ('attempts',0),('attempts',2),('has_at',1),('has_reply',0),('plan','{}'),('task_id','effect-exists'),
    ('reference_id','already-linked'),('hold_code','native_unknown_lease'),('hold_code','native_reply_not_matched')])
def test_spent_unknown_history_other_hold_and_second_attempt_refused(case,column,value):
    store,clock,source,first=case
    with store._connection(write=True) as db:db.execute('UPDATE sources SET '+column+'=? WHERE source_id=?',(value,source))
    before=row(store,source)
    with pytest.raises(NativeStoreError):request(case)
    assert row(store,source)==before and audits(store,source)==[]


@pytest.mark.parametrize('problem',['stale','paused','pin_revision','pin_conflict','immutable','wrong_type','bool_type',
    'wrong_comment_type','missing_observation','wrong_notice_ref','wrong_source_id','old_created'])
def test_only_original_complete_reply_facts_and_current_pinned_identity(case,problem):
    store,clock,source,first=case
    if problem=='stale':clock[0]+=121
    elif problem=='paused':store.pause('synthetic_unavailable')
    else:
        with store._connection(write=True) as db:
            if problem=='pin_revision':db.execute('UPDATE identities SET revision=revision+1')
            elif problem=='pin_conflict':db.execute("UPDATE identities SET state='conflict'")
            elif problem=='immutable':db.execute("UPDATE sources SET immutable_hash='broken' WHERE source_id=?",(source,))
            elif problem=='missing_observation':db.execute('DELETE FROM observations WHERE source_id=?',(source,))
            elif problem=='wrong_notice_ref':db.execute('UPDATE notice_refs SET notice_type=45 WHERE source_id=?',(source,))
            elif problem=='wrong_source_id':db.execute("UPDATE sources SET cid='9999' WHERE source_id=?",(source,))
            else:
                candidate=json.loads(row(store,source)['candidate'])
                if problem=='wrong_type':candidate['notice_type']=45
                elif problem=='bool_type':candidate['notice_type']=True
                elif problem=='wrong_comment_type':candidate['comment_type']=12
                elif problem=='old_created':candidate['created_time']='90'
                db.execute('UPDATE sources SET candidate=? WHERE source_id=?',(_dump(candidate),source))
    before=row(store,source)
    with pytest.raises(NativeStoreError):request(case)
    assert row(store,source)==before and audits(store,source)==[]


def test_existing_other_explicit_recheck_fence_cannot_be_bypassed(case):
    store,clock,source,first=case
    with store._connection(write=True) as db:db.execute('INSERT INTO meta VALUES(?,?)',('ui_recheck_v1:'+source,'{}'))
    before=row(store,source)
    with pytest.raises(NativeStoreError,match='already_requested'):request(case)
    assert row(store,source)==before and audits(store,source)==[]


def test_source_transition_and_once_audit_are_atomic(case):
    store,clock,source,first=case
    with store._connection(write=True) as db:
        db.execute("CREATE TRIGGER synthetic_followup_abort BEFORE UPDATE ON sources WHEN NEW.state='fresh' BEGIN SELECT RAISE(ABORT,'synthetic-fault'); END")
    before=row(store,source)
    with pytest.raises(Exception):request(case)
    assert row(store,source)==before and audits(store,source)==[]
    with store._connection(write=True) as db:db.execute('DROP TRIGGER synthetic_followup_abort')
    assert request(case)['audit_recorded'] is True


@pytest.mark.parametrize('source,reason',[
    ('native-hint:'+'1'*64,REASON),('assistant-task:'+'1'*32,REASON),('native:'+'A'*64,REASON),
    ('native:'+'1'*64,'untrusted reason'),('native:'+'1'*64,True)])
def test_namespace_and_reason_are_fixed_not_freeform(case,source,reason):
    store,clock,original,first=case
    with pytest.raises(NativeStoreError):store.request_followup_review(source,operator_reason=reason)
    assert audits(store,original)==[] and row(store,original)['state']=='held'
