"""API meaning before UI, using synthetic messages and temporary owned files."""
from copy import deepcopy
from datetime import datetime,timezone
import json

import pytest

from app.assistant.native_semantic_inbox import (NativeSemanticInbox,NativeSemanticInboxError,
    validate_native_message,native_message_source_key,native_message_digest)
from app.assistant.semantic_plan_store import (SemanticPlanStore,SemanticPlanError,
    decision_metadata,verify_semantic_decision,ui_semantic_digest)
from app.assistant.semantic_intent_model import intent_messages
from app.assistant.native_events import project_page
from tests.test_assistant_native_receiver import at,page,B,SEC_B,PROFILE_B,PROFILE_A
from tests.test_semantic_planning_store import Model,Budget,TEXT,CATALOG,OWNER


def message(*,nid='1001',cid='2001',created='102',text=TEXT):
    raw=at(nid=nid,cid=cid,created=created,text=text)
    raw['aweme_id']=raw['at']['aweme']['aweme_id']='90001'
    raw['at']['schema_url']='aweme://aweme/detail/90001?cid='+cid
    return project_page(page([raw]),9,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:profile==PROFILE_A)['candidates'][0]


@pytest.fixture
def case(tmp_path):
    clock=[100.0];allowed=[True]
    inbox=NativeSemanticInbox.prepare(tmp_path/'inbox.sqlite3',B,PROFILE_B,clock=lambda:clock[0],
        source_authorizer=lambda _message:allowed[0] is True)
    store=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3',clock=lambda:clock[0])
    return inbox,store,clock,allowed,Model(),Budget()


def scope_for(value,*,actual_ui=False):
    key=native_message_source_key(value)
    return {**OWNER,'sender_profile_url':PROFILE_A,'receiver_profile_url':PROFILE_B,'video_id':value['video_id'],
            'reference_id':'reference:actual-ui' if actual_ui else key,
            'root_reference_id':'reference:actual-ui' if actual_ui else key}


def understand(case,value=None,**changes):
    inbox,store,clock,allowed,model,budget=case;value=value or message()
    kwargs=dict(source_key=native_message_source_key(value),frozen_ui_digest=native_message_digest(value),
        owner_scope=scope_for(value),catalog_snapshot=deepcopy(CATALOG),mode='native',original_text=value['content'],
        native_facts=value,model=model,budget=budget,scope_authorizer=lambda:allowed[0],should_stop=lambda:False)
    kwargs.update(changes)
    return store.run_once(**kwargs)


def ui_envelope(value,clock):
    return {'source_mode':'live_ui','command_id':'command:actual','reference_id':'reference:actual-ui',
        'sender_profile_url':PROFILE_A,'receiver_profile_url':PROFILE_B,'video_id':value['video_id'],
        'native_comment_id':None,'comment_text':value['content'],'evidence':{'real_mention':True,
        'sender_verified':True,'video_verified':True,'unique_count':1,
        'observed_at_utc':datetime.fromtimestamp(clock[0],timezone.utc).isoformat()}}


def bind(case,receipt,value,env=None,**changes):
    inbox,store,clock,allowed,model,budget=case;env=env or ui_envelope(value,clock)
    kwargs=dict(owner_scope=scope_for(value,actual_ui=True),catalog_snapshot=deepcopy(CATALOG),source_facts=value,
        scope_authorizer=lambda:allowed[0],should_stop=lambda:False)
    kwargs.update(changes)
    return store.bind_native_to_ui(receipt,env,**kwargs)


def test_default_queue_is_historical_and_activation_does_not_replay_old_message(case):
    inbox,store,clock,allowed,model,budget=case
    value=message(created='101');clock[0]=102
    [key]=inbox.record_messages([value],page_fingerprint='a'*64)
    assert inbox.lookup(key)['state']=='historical' and not inbox.is_processing()
    assert inbox.enable_new_messages()['activation_fence']==102.0
    inbox.record_messages([value],page_fingerprint='b'*64)
    assert inbox.claim_next('worker') is None and model.calls==0


def test_future_native_message_can_be_understood_without_any_ui_binding(case):
    inbox,store,clock,allowed,model,budget=case;inbox.enable_new_messages();clock[0]=103
    value=message();[key]=inbox.record_messages([value],page_fingerprint='a'*64)
    assert inbox.is_processing()
    reading=inbox.claim_next('worker');assert reading['source_key']==key and reading['message']==value
    origin=understand(case,value)
    assert decision_metadata(origin)['mode']=='native' and origin.decision['action']=='save'
    assert store.get_recorded_decision(ui_envelope(value,clock)) is None
    with pytest.raises(SemanticPlanError):verify_semantic_decision(origin,mode='root',original_text=TEXT)
    assert inbox.finish(key,reading['claim_token'],origin)['state']=='understood'
    assert not inbox.is_processing() and model.calls==1


def test_same_cid_new_notice_and_time_does_not_replan_or_overwrite_origin(case):
    inbox,store,clock,allowed,model,budget=case;inbox.enable_new_messages();clock[0]=103
    first=message();[key]=inbox.record_messages([first],page_fingerprint='a'*64)
    claim=inbox.claim_next('worker');origin=understand(case,first);inbox.finish(key,claim['claim_token'],origin)
    again=message(nid='1010',created='103')
    assert native_message_digest(first)==native_message_digest(again)
    inbox.record_messages([again],page_fingerprint='b'*64)
    assert inbox.lookup(key)['message']==first and inbox.lookup(key)['state']=='understood'
    assert understand(case,again).plan_id==origin.plan_id and model.calls==1


def test_same_cid_payload_change_holds_without_second_model(case):
    inbox,store,clock,allowed,model,budget=case;inbox.enable_new_messages();clock[0]=103
    value=message();[key]=inbox.record_messages([value],page_fingerprint='a'*64)
    claim=inbox.claim_next('worker');origin=understand(case,value);inbox.finish(key,claim['claim_token'],origin)
    inbox.record_messages([message(nid='1011',text=TEXT+' changed')],page_fingerprint='b'*64)
    assert inbox.lookup(key)['state']=='held' and inbox.claim_next('another') is None and model.calls==1


def test_crashed_claim_expires_held_without_blocking_other_messages_or_repay(case):
    inbox,store,clock,allowed,model,budget=case;inbox.enable_new_messages();clock[0]=103
    first=message();[key]=inbox.record_messages([first],page_fingerprint='a'*64)
    inbox.claim_next('old-worker',lease_seconds=1)
    clock[0]=106
    reopened=NativeSemanticInbox(inbox.path,B,PROFILE_B,clock=lambda:clock[0],source_authorizer=lambda _:True)
    assert reopened.is_processing() is False
    assert reopened.lookup(key)['state']=='held' and reopened.lookup(key)['attempts']==1
    reopened.enable_new_messages();assert reopened.claim_next('new-worker') is None
    second=message(nid='1020',cid='2020',created='105');[second_key]=reopened.record_messages([second],page_fingerprint='b'*64)
    assert reopened.claim_next('new-worker')['source_key']==second_key and model.calls==0


def test_current_source_authorization_is_required_before_pending_and_claim(case):
    inbox,store,clock,allowed,model,budget=case;inbox.enable_new_messages();clock[0]=103;allowed[0]=False
    [key]=inbox.record_messages([message()],page_fingerprint='a'*64)
    assert inbox.lookup(key)['state']=='historical' and model.calls==0
    allowed[0]=True;second=message(nid='1012',cid='2012');[key]=inbox.record_messages([second],page_fingerprint='b'*64)
    allowed[0]=False;assert inbox.claim_next('worker') is None and inbox.lookup(key)['state']=='held'


def test_native_source_key_accepts_ui_derivatives_but_fact_validator_does_not():
    value=message();augmented={**value,'native_source_key':'ignored','source_id':'ignored','identity_link_revision':'1'}
    assert native_message_source_key(augmented)==native_message_source_key(value)
    with pytest.raises(NativeSemanticInboxError):validate_native_message(augmented)


def test_ui_binding_keeps_native_budget_request_usage_and_original_facts(case):
    inbox,store,clock,allowed,model,budget=case;clock[0]=103;value=message();origin=understand(case,value)
    before=decision_metadata(origin);env=ui_envelope(value,clock);bound=bind(case,origin,value,env)
    after=decision_metadata(bound)
    assert after['mode']=='root' and after['origin_mode']=='native' and after['frozen_ui_digest']==ui_semantic_digest(env)
    for key in ('plan_id','source_key','budget_scope_id','request_key','usage','native_facts_digest'):
        assert after[key]==before[key]
    assert decision_metadata(store.get_native_decision(before['source_key']))['mode']=='native'
    assert store.get_recorded_decision(env).plan_id==origin.plan_id
    assert bind(case,origin,value,env).plan_id==origin.plan_id and model.calls==1


@pytest.mark.parametrize('change',['wrong_actor','wrong_video','wrong_body','wrong_cid','no_mention','not_fresh','not_unique'])
def test_native_meaning_cannot_replace_actual_ui_proof(case,change):
    inbox,store,clock,allowed,model,budget=case;clock[0]=103;value=message();origin=understand(case,value);env=ui_envelope(value,clock)
    if change=='wrong_actor':env['sender_profile_url']='https://www.douyin.com/user/wrong-A'
    if change=='wrong_video':env['video_id']='90002'
    if change=='wrong_body':env['comment_text']='other body'
    if change=='wrong_cid':env['native_comment_id']='2999'
    if change=='no_mention':env['evidence']['real_mention']=False
    if change=='not_fresh':
        clock[0]=300
        env['evidence']['observed_at_utc']='1970-01-01T00:00:00Z'
    if change=='not_unique':env['evidence']['unique_count']=2
    with pytest.raises(SemanticPlanError):bind(case,origin,value,env)
    assert store.get_recorded_decision(env) is None and model.calls==1


def test_catalog_change_only_allows_fee_binding_not_a_second_meaning_request(case):
    inbox,store,clock,allowed,model,budget=case;clock[0]=103;value=message();origin=understand(case,value);env=ui_envelope(value,clock)
    with pytest.raises(SemanticPlanError,match='catalog_changed'):bind(case,origin,value,env,catalog_snapshot={**CATALOG,'revision':'changed'})
    fee=store.bind_native_accounting_to_ui(native_message_source_key(value),env,owner_scope=scope_for(value,actual_ui=True),
        source_facts=value,scope_authorizer=lambda:True,should_stop=lambda:False)
    assert fee['dispatchable'] is False and fee['usage']['total_tokens']==20 and store.get_recorded_decision(env) is None
    assert model.calls==1


def test_known_paid_usage_survives_scope_failure_and_fee_only_ui_binding(case):
    inbox,store,clock,allowed,model,budget=case;clock[0]=103;value=message()
    model.hook=lambda:allowed.__setitem__(0,False)
    with pytest.raises(SemanticPlanError,match='scope_unavailable'):understand(case,value)
    key=native_message_source_key(value);row=store.lookup_receipt(key)
    assert row['state']=='unknown' and row['usage']['total_tokens']==20 and store.get_native_decision(key) is None
    allowed[0]=True;env=ui_envelope(value,clock)
    fee=store.bind_native_accounting_to_ui(key,env,owner_scope=scope_for(value,actual_ui=True),source_facts=value,
        scope_authorizer=lambda:True,should_stop=lambda:False)
    assert fee['state']=='unknown' and fee['usage']['total_tokens']==20 and fee['request_keys']==[row['request_key']]
    assert store.get_recorded_decision(env) is None
    with pytest.raises(SemanticPlanError):verify_semantic_decision(fee,mode='root',original_text=TEXT)
    with pytest.raises(SemanticPlanError):understand(case,value)
    assert model.calls==1


def test_native_prompt_does_not_guess_previous_choice_menu():
    prompt=intent_messages('native','第四个',CATALOG)[0]['content']
    assert '不继承任何旧选库问句' in prompt and '不能按当前目录顺序猜' in prompt


def test_protected_path_and_unknown_empty_file_not_adopted(tmp_path):
    file=tmp_path/'protected.sqlite3';file.write_bytes(b'')
    with pytest.raises(NativeSemanticInboxError):NativeSemanticInbox.prepare(file,B,PROFILE_B,source_authorizer=lambda _:True,protected_paths=(file,))
    assert file.read_bytes()==b'' and not file.with_suffix('.sqlite3.owner.json').exists()
