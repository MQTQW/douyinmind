"""Only first future inert hints may get one bounded UI-read request."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from app.assistant.native_visibility_hint_store import NativeVisibilityHintStore,NativeVisibilityHintError
from app.assistant.native_events import project_ui_review_hints
from tests.test_native_visibility_hint_store import case,claim,request,plan_for,original_notices
from tests.test_assistant_native_receiver import B,SEC_B,PROFILE_A,PROFILE_B,at,page,project


def add_unknown(case,*,nid='1020',cid='2020',created='104',text='synthetic new instruction'):
    source,ledger,clock,_,_=case
    raw=at(nid=nid,cid=cid,created=created,text=text);raw['at']['comment_status']=42
    projected=project(page([raw]),9)
    source.record_page(source.prepare_channel(9,'601'),projected,next_cursor=('0','0'),has_more=False,allow_live=True)
    [hint]=project_ui_review_hints(page([raw]),9,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:profile==PROFILE_A)
    return hint,projected['fingerprint']


def audit(ledger):
    with ledger._connection() as db:
        return [dict(row) for row in db.execute('SELECT hint_id,kind,at,facts FROM audit ORDER BY id')]


def test_default_is_unchanged_even_for_a_new_future_hint(case):
    source,ledger,clock,_,_=case;clock[0]=105
    hint,fp=add_unknown(case)
    [hid]=ledger.record_hints([hint],page_fingerprint=fp)
    row=ledger.get_hint(hid)
    assert row['state']=='historical' and row['requested'] is False and row['attempts']==0
    assert ledger.list_hints('pending')==[] and audit(ledger)==[]


def test_explicit_enable_requests_only_new_fresh_hint_without_touching_skip_or_source(case):
    source,ledger,clock,old_id,old_hint=case
    with ledger._connection() as db:meta_before=dict(db.execute('SELECT key,value FROM meta'))
    result=ledger.enable_new_hint_review()
    assert result=={'enabled':True,'activation_fence':102.0,'created':True,'scope':'fresh_ui_review_only','dispatchable':False}
    clock[0]=105
    hint,fp=add_unknown(case);before=original_notices(source)
    [hid]=ledger.record_hints([hint],page_fingerprint=fp)
    row=ledger.get_hint(hid)
    assert row['state']=='pending' and row['requested'] is True and row['attempts']==0
    assert ledger.get_hint(old_id)['state']=='historical'
    assert original_notices(source)==before
    assert source.find_source_by_native_comment(B,hint['video_id'],hint['native_comment_id']) is None
    with ledger._connection() as db:assert dict(db.execute('SELECT key,value FROM meta'))==meta_before
    request_audit=[r for r in audit(ledger) if r['kind']=='automatic_review_requested']
    assert len(request_audit)==1
    assert json.loads(request_audit[0]['facts'])['reason']=='automatic_fresh_ui_review_only'


def test_restart_requires_instance_optin_and_preserves_original_fence_and_old_rows(case):
    source,ledger,clock,_,_=case;ledger.enable_new_hint_review()
    clock[0]=110
    second=NativeVisibilityHintStore(ledger.path,B,PROFILE_B,source,clock=lambda:clock[0])
    hint,fp=add_unknown(case,created='109')
    [hid]=second.record_hints([hint],page_fingerprint=fp)
    assert second.get_hint(hid)['state']=='historical'  # Persisted audit alone is not enabled.
    result=second.enable_new_hint_review()
    assert result['created'] is False and result['activation_fence']==102.0
    assert second.record_hints([hint],page_fingerprint=fp)==[hid]
    assert second.get_hint(hid)['state']=='historical'  # Never upgrades an existing row.
    clock[0]=112;new,fp=add_unknown(case,nid='1021',cid='2021',created='111')
    [new_id]=second.record_hints([new],page_fingerprint=fp)
    assert second.get_hint(new_id)['state']=='pending'
    assert len([r for r in audit(second) if r['kind']=='automatic_new_hint_policy_enabled'])==1


@pytest.mark.parametrize('created',['101','102'])
def test_older_or_same_second_first_observation_is_not_automatically_upgraded(case,created):
    source,ledger,clock,_,_=case;ledger.enable_new_hint_review();clock[0]=105
    hint,fp=add_unknown(case,created=created)
    [hid]=ledger.record_hints([hint],page_fingerprint=fp)
    row=ledger.get_hint(hid)
    assert row['state']=='historical' and row['requested'] is False
    assert row['hold_code']=='native_visibility_hint_before_activation'
    request(ledger,hid)  # An explicit once-only owner request still has its original gates.
    assert ledger.get_hint(hid)['state']=='pending'


@pytest.mark.parametrize('state',['historical','pending','claimed','held','committing','admitted'])
def test_existing_state_is_not_replayed_or_rewritten_by_auto_enable(case,state):
    source,ledger,clock,hid,hint=case
    if state=='pending':request(ledger,hid)
    elif state in {'claimed','held','committing','admitted'}:
        reading=claim(case)
        if state=='held':ledger.hold(hid,reading['claim_token'],'synthetic_read_unconfirmed')
        if state in {'committing','admitted'}:
            permit=ledger.mint_ui_plan_permit(hid,reading['claim_token'],plan_for(hint))
            if state=='committing':ledger._begin_source_admission(permit,source)
            else:source.accept_verified_hint_ui_plan(permit)
    before=ledger.get_hint(hid);before_audit=audit(ledger)
    ledger.enable_new_hint_review();clock[0]=105
    ledger.record_hints([hint],page_fingerprint='a'*64)
    after=ledger.get_hint(hid)
    for key in ('state','hold_code','attempts','requested','first_seen','admitted_source_id','hint'):
        assert after[key]==before[key]
    assert [r for r in audit(ledger) if r['kind']!='automatic_new_hint_policy_enabled']==before_audit


def test_one_bad_checkpoint_keeps_historical_and_does_not_drop_next_valid_hint(case,monkeypatch):
    source,ledger,clock,_,_=case;ledger.enable_new_hint_review();clock[0]=105
    first,fp=add_unknown(case);second,_=add_unknown(case,nid='1021',cid='2021')
    original=source.visibility_hint_checkpoint
    def check(hint):
        if hint['native_comment_id']=='2020':raise RuntimeError('secret-cookie-url-and-raw-exception')
        return original(hint)
    monkeypatch.setattr(source,'visibility_hint_checkpoint',check)
    ids=ledger.record_hints([first,second],page_fingerprint=fp)
    assert ledger.get_hint(ids[0])['state']=='historical'
    assert ledger.get_hint(ids[0])['hold_code']=='native_visibility_auto_checkpoint_unavailable'
    assert ledger.get_hint(ids[1])['state']=='pending'
    assert b'secret-cookie' not in ledger.path.read_bytes()
    # Later health improvement cannot turn the existing failed hint pending.
    monkeypatch.setattr(source,'visibility_hint_checkpoint',original)
    ledger.record_hints([first],page_fingerprint=fp)
    assert ledger.get_hint(ids[0])['state']=='historical'


def test_stale_native_identity_is_a_fixed_historical_reason_not_permission(case):
    source,ledger,clock,_,_=case;ledger.enable_new_hint_review();clock[0]=400
    hint,fp=add_unknown(case,created='399')
    [hid]=ledger.record_hints([hint],page_fingerprint=fp)
    assert ledger.get_hint(hid)['state']=='historical'
    assert ledger.get_hint(hid)['hold_code']=='native_identity_unverified'
    assert ledger.list_hints('pending')==[]


def test_claim_timeout_and_restart_do_not_reclaim_or_reset_attempt(case):
    source,ledger,clock,_,_=case;ledger.enable_new_hint_review();clock[0]=105
    hint,fp=add_unknown(case)
    [hid]=ledger.record_hints([hint],page_fingerprint=fp)
    reading=ledger.claim_review(hid,'first-run',lease_seconds=1)
    assert reading and ledger.get_hint(hid)['attempts']==1
    clock[0]=108
    reopened=NativeVisibilityHintStore(ledger.path,B,PROFILE_B,source,clock=lambda:clock[0]);reopened.enable_new_hint_review()
    reopened.record_hints([hint],page_fingerprint=fp)
    assert reopened.claim_review(hid,'second-run') is None
    assert reopened.get_hint(hid)['attempts']==1 and reopened.get_hint(hid)['state']=='claimed'
    with pytest.raises(NativeVisibilityHintError):request(reopened,hid)


def test_corrupt_or_duplicated_policy_never_creates_a_new_fence(case):
    source,ledger,clock,_,_=case;ledger.enable_new_hint_review()
    with ledger._connection(write=True) as db:
        row=db.execute("SELECT * FROM audit WHERE kind='automatic_new_hint_policy_enabled'").fetchone()
        db.execute('INSERT INTO audit(hint_id,kind,at,facts) VALUES(?,?,?,?)',
            (row['hint_id'],row['kind'],row['at'],row['facts']))
    before=audit(ledger)
    other=NativeVisibilityHintStore(ledger.path,B,PROFILE_B,source,clock=lambda:clock[0])
    with pytest.raises(NativeVisibilityHintError,match='auto_policy_unavailable'):other.enable_new_hint_review()
    assert audit(other)==before


def test_concurrent_enable_instances_share_one_persistent_fence(case):
    source,ledger,clock,_,_=case
    other=NativeVisibilityHintStore(ledger.path,B,PROFILE_B,source,clock=lambda:clock[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda instance:instance.enable_new_hint_review(),(ledger,other)))
    assert sum(result['created'] for result in results)==1
    assert {result['activation_fence'] for result in results}=={102.0}
    assert len([r for r in audit(ledger) if r['kind']=='automatic_new_hint_policy_enabled'])==1
