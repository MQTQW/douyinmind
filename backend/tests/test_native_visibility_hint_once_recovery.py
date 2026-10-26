"""Once-only held READ recovery; temporary owned ledgers, no real provider/UI."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.assistant.native_visibility_hint_store import (NativeVisibilityHintStore,NativeVisibilityHintError,
    hint_source_id)
from app.assistant.native_semantic_inbox import NativeSemanticInbox,native_message_digest
from app.assistant.semantic_plan_store import SemanticPlanStore,decision_metadata
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.semantic_resolution import catalog_snapshot_from_grants
from app.assistant.native_lane import NativeLaneError
from app.mention_receiver.browser import ReceiverDetailError
from tests.test_native_visibility_hint_store import case as ledger_case,claim,plan_for,original_notices
from tests.test_native_visibility_hint_inspection import held as lane_case
from tests.test_assistant_native_visibility_lane import case as native_case,no_task
from tests.test_semantic_planning_store import OWNER,CATALOG,Budget,turn
from tests.test_assistant_native_recovery_mailbox import make

REASON='explicit_operator_held_visibility_ui_recovery'


@pytest.fixture(autouse=True)
def valid_synthetic_video(monkeypatch):
    # Older isolated notice tests use a four-digit video ID. Native semantic
    # ownership correctly requires 5..30 digits; adapt only this raw fixture.
    from tests import test_native_visibility_hint_store as fixtures
    original=fixtures.at
    def at(*args,**kwargs):
        raw=original(*args,**kwargs)
        raw['aweme_id']=raw['at']['aweme']['aweme_id']='90001'
        cid=raw['at']['schema_url'].split('cid=',1)[1]
        raw['at']['schema_url']='aweme://aweme/detail/90001?cid='+cid
        return raw
    monkeypatch.setattr(fixtures,'at',at)


def native_receipt(store,hint,*,owner=None,catalog=None):
    source=hint_source_id(hint)
    owner=owner or {**OWNER,**{k:hint[k] for k in ('sender_profile_url','receiver_profile_url','video_id')},
        'reference_id':source,'root_reference_id':source}
    calls=[]
    def complete(messages,tools):calls.append(1);return turn()
    receipt=store.run_once(source_key=source,frozen_ui_digest=native_message_digest(hint),owner_scope=owner,
        catalog_snapshot=catalog or deepcopy(CATALOG),mode='native',original_text=hint['content'],native_facts=hint,
        model=SimpleNamespace(complete=complete),budget=Budget(),scope_authorizer=lambda:True,should_stop=lambda:False)
    return receipt,calls


@pytest.fixture
def held(ledger_case,tmp_path):
    source,ledger,clock,hid,hint=ledger_case
    first=claim(ledger_case);ledger.hold(hid,first['claim_token'],'native_visibility_ui_unverified')
    store=SemanticPlanStore.prepare(tmp_path/'semantic-once.sqlite3',clock=lambda:clock[0])
    receipt,calls=native_receipt(store,hint)
    return SimpleNamespace(source=source,ledger=ledger,clock=clock,hid=hid,hint=hint,first=first,
        store=store,receipt=receipt,calls=calls)


def recover(c):return c.ledger.request_recovery_review(c.hid,operator_reason=REASON,semantic_receipt=c.receipt)


def audit(c,kind):
    with c.ledger._connection() as db:
        return [json.loads(r[0]) for r in db.execute('SELECT facts FROM audit WHERE hint_id=? AND kind=?',(c.hid,kind))]


def test_explicit_recovery_preserves_old_facts_then_second_claim_is_unique(held):
    c=held;old=c.ledger.get_hint(c.hid);notices=original_notices(c.source)
    with c.ledger._connection() as db:old_row=dict(db.execute('SELECT * FROM hints WHERE hint_id=?',(c.hid,)).fetchone())
    assert recover(c)['status']=='visibility_recovery_requested'
    pending=c.ledger.get_hint(c.hid)
    assert pending['state']=='pending' and pending['attempts']==1 and pending['hold_code']==old['hold_code']
    with c.ledger._connection() as db:new_row=dict(db.execute('SELECT * FROM hints WHERE hint_id=?',(c.hid,)).fetchone())
    assert {k:v for k,v in old_row.items() if k!='state'}=={k:v for k,v in new_row.items() if k!='state'}
    [facts]=audit(c,'explicit_recovery_requested')
    assert facts['old_claim_sha256']==hashlib.sha256(c.first['claim_token'].encode()).hexdigest()
    assert c.first['claim_token'] not in json.dumps(facts)
    assert c.ledger.check_claim(c.hid,c.first['claim_token'])['valid'] is False
    second=c.ledger.claim_review(c.hid,'new-owner')
    assert second['claim_token']!=c.first['claim_token'] and c.ledger.get_hint(c.hid)['attempts']==2
    assert c.ledger.check_claim(c.hid,second['claim_token'])['valid'] is True
    assert c.ledger.check_claim(c.hid,c.first['claim_token'])['valid'] is False
    with pytest.raises(Exception):c.ledger.hold(c.hid,c.first['claim_token'],'native_visibility_ui_unverified')
    assert c.ledger.claim_review(c.hid,'another-owner') is None
    assert original_notices(c.source)==notices and c.calls==[1]


def test_second_claim_can_use_original_typed_plan_path_without_resetting_attempt(held):
    c=held;recover(c);second=c.ledger.claim_review(c.hid,'new-owner')
    permit=c.ledger.mint_ui_plan_permit(c.hid,second['claim_token'],plan_for(c.hint))
    planned=c.source.accept_verified_hint_ui_plan(permit)
    assert c.source.get_source(planned['source_id'])['state']=='planned'
    assert c.ledger.get_hint(c.hid)['state']=='admitted' and c.ledger.get_hint(c.hid)['attempts']==2
    with pytest.raises(Exception):recover(c)
    assert c.calls==[1]


def test_two_instances_cannot_double_request_or_double_claim(held):
    c=held;other=NativeVisibilityHintStore(c.ledger.path,c.source.receiver_uid,c.source.receiver_profile_url,c.source,clock=lambda:c.clock[0])
    def request(store):
        try:return store.request_recovery_review(c.hid,operator_reason=REASON,semantic_receipt=c.receipt)['status']
        except NativeVisibilityHintError:return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(request,(c.ledger,other)))
    assert results.count('visibility_recovery_requested')==1
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda store:store.claim_review(c.hid,'new-owner'),(c.ledger,other)))
    assert sum(r is not None for r in results)==1 and len(audit(c,'explicit_recovery_requested'))==1


def test_second_failure_or_restart_never_unlocks_third_attempt(held):
    c=held;recover(c);second=c.ledger.claim_review(c.hid,'new-owner',lease_seconds=1)
    c.clock[0]+=2;c.source.set_verified()
    reloaded=NativeVisibilityHintStore(c.ledger.path,c.source.receiver_uid,c.source.receiver_profile_url,c.source,clock=lambda:c.clock[0])
    assert reloaded.check_claim(c.hid,second['claim_token'])['valid'] is False
    assert reloaded.claim_review(c.hid,'third-owner') is None
    reloaded.hold(c.hid,second['claim_token'],'native_visibility_ui_unverified')
    with pytest.raises(Exception):reloaded.request_recovery_review(c.hid,operator_reason=REASON,semantic_receipt=c.receipt)
    assert reloaded.get_hint(c.hid)['attempts']==2


@pytest.mark.parametrize('change',["hold_code='native_visibility_review_intervention'","attempts=2",
    "plan_hash='existing-plan'","admitted_source_id='native:existing'","state='committing'",
    "claim_token=NULL"])
def test_other_held_or_possible_effects_cannot_receive_recovery(held,change):
    with held.ledger._connection(write=True) as db:db.execute('UPDATE hints SET '+change+' WHERE hint_id=?',(held.hid,))
    with pytest.raises(Exception):recover(held)
    assert audit(held,'explicit_recovery_requested')==[]


def test_plain_dict_or_wrong_source_private_receipt_rejected(held):
    c=held
    with pytest.raises(Exception):c.ledger.request_recovery_review(c.hid,operator_reason=REASON,semantic_receipt=decision_metadata(c.receipt))
    wrong=deepcopy(c.hint);wrong['native_comment_id']='9991'
    other,_=native_receipt(c.store,wrong)
    with pytest.raises(Exception):c.ledger.request_recovery_review(c.hid,operator_reason=REASON,semantic_receipt=other)
    assert audit(c,'explicit_recovery_requested')==[]


def test_attempt_two_without_matching_audit_or_with_forged_audit_is_not_usable(held):
    c=held;recover(c);second=c.ledger.claim_review(c.hid,'new-owner')
    with c.ledger._connection(write=True) as db:
        db.execute("DELETE FROM audit WHERE hint_id=? AND kind='explicit_recovery_claimed'",(c.hid,))
    assert c.ledger.check_claim(c.hid,second['claim_token'])['valid'] is False
    with pytest.raises(Exception):c.ledger.mint_ui_plan_permit(c.hid,second['claim_token'],plan_for(c.hint))


def install_provider(case,tmp_path):
    store=SemanticPlanStore.prepare(tmp_path/'lane-meaning.sqlite3',clock=lambda:case.clock[0])
    provider=SemanticIntentInterpreter(case.registry,store,None,None)
    user,device,grants=provider._context(case.hint)
    owner={k:case.hint[k] for k in ('sender_profile_url','receiver_profile_url','video_id')}
    key=hint_source_id(case.hint)
    owner.update(user_id=user['user_id'],device_id=device['device_id'],instance_id=device['instance_id'],reference_id=key,root_reference_id=key)
    catalog=catalog_snapshot_from_grants(grants)
    # A complete clarification is enough to test reuse without granting import.
    model=SimpleNamespace(complete=lambda *a:turn({'action':'clarify','target_state':'unspecified','grant_id':None}))
    receipt=store.run_once(source_key=key,frozen_ui_digest=native_message_digest(case.hint),owner_scope=owner,
        catalog_snapshot=catalog,mode='native',original_text=case.hint['content'],native_facts=case.hint,
        model=model,budget=Budget(),scope_authorizer=lambda:True,should_stop=lambda:False)
    row={'state':'understood','message':deepcopy(case.hint),'facts_digest':native_message_digest(case.hint),'plan_id':receipt.plan_id}
    provider.native_inbox=SimpleNamespace(lookup=lambda source:deepcopy(row) if source==key else None)
    case.lane.browser_lane.semantic_resolution_provider=provider
    return row,provider


def test_lane_registers_only_recovery_and_does_not_open_UI_or_run_model(lane_case,tmp_path):
    case=lane_case;row,provider=install_provider(case,tmp_path)
    provider.model=SimpleNamespace(complete=lambda *_:pytest.fail('new planning request'))
    result=case.lane.recover_visibility_hint(case.hid)
    assert result['status']=='visibility_hint_recovery_requested' and result['dispatchable'] is False
    assert case.ledger.get_hint(case.hid)['state']=='pending' and case.ledger.get_hint(case.hid)['attempts']==1
    assert not case.adapter.native_calls
    no_task(case)


@pytest.mark.parametrize('problem',['running','revoked','meaning_held','meaning_wrong_plan','new_catalog','queue_effect'])
def test_lane_requires_current_private_meaning_scope_and_no_effects(lane_case,tmp_path,problem):
    case=lane_case;row,provider=install_provider(case,tmp_path)
    if problem=='running':case.queue.update_control(lambda old:{**old,'state':'running','desired_state':'running'})
    elif problem=='revoked':case.registry.revoke_user(case.migrated['user_id'])
    elif problem=='meaning_held':row['state']='held'
    elif problem=='meaning_wrong_plan':row['plan_id']='different-plan'
    elif problem=='new_catalog':
        original=provider._context
        def changed(hint):
            u,d,g=original(hint);g=deepcopy(g);g[0]['name']+=' changed';return u,d,g
        provider._context=changed
    else:case.lane._visibility_absent_from_effects=lambda _:False
    with pytest.raises(NativeLaneError):case.lane.recover_visibility_hint(case.hid)
    assert case.ledger.get_hint(case.hid)['state']=='held' and case.ledger.get_hint(case.hid)['attempts']==1
    assert not case.adapter.native_calls


def test_automatic_read_failure_records_only_fixed_stage_counts(lane_case):
    case=lane_case
    # Diagnostic collection is separately exercised with an ordinary first
    # attempt. No restoration of a production claim or retry is involved.
    with case.ledger._connection(write=True) as db:
        db.execute("UPDATE hints SET state='pending',attempts=0,claim_token=NULL,claim_owner=NULL,lease_until=NULL WHERE hint_id=?",(case.hid,))
    def fail(*args,**kwargs):
        case.adapter.last_original_lookup={'candidate_limit':3,'candidate_count':0,'lookup_stage_index':3,
            'reason_code':'original_lookup_unavailable','secret':'DO-NOT-SAVE','checked_count':True}
        raise ReceiverDetailError('DO-NOT-SAVE',{})
    case.adapter.open_original_from_notifications=fail
    assert case.lane.collect()==[]
    with case.ledger._connection() as db:
        [facts]=[json.loads(r[0]) for r in db.execute("SELECT facts FROM audit WHERE hint_id=? AND kind='ui_review_diagnostic'",(case.hid,))]
    assert facts['diagnostic']['stage']=='original_lookup'
    assert facts['diagnostic']['exception_type']=='ReceiverDetailError'
    assert facts['diagnostic']['lookup_counts']=={'candidate_limit':3,'candidate_count':0,'lookup_stage_index':3}
    assert facts['diagnostic']['lookup_reason_code']=='original_lookup_unavailable'
    assert 'DO-NOT-SAVE' not in json.dumps(facts)
    assert case.ledger.get_hint(case.hid)['state']=='held'
    no_task(case)


def test_recovery_mailbox_does_not_execute_inline_and_is_once(tmp_path):
    box,_,_=make(tmp_path);hint='native-hint:'+'c'*64
    with pytest.raises(Exception):box.submit('native:'+'c'*64,'recover_visibility_hint')
    box.submit(hint,'recover_visibility_hint');calls=[]
    def handler(op,source):calls.append((op,source));return {'ok':True,'queued_for_fresh_ui':True,'dispatchable':False}
    result=box.process_one(handler)
    assert result['code']=='native_visibility_recovery_requested' and result['no_model'] and result['no_send']
    assert calls==[('recover_visibility_hint',hint)] and box.process_one(handler) is None
