"""Independent API-first boundaries, synthetic pages/temp ledgers only."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.assistant.native_events import project_page,project_semantic_messages
from app.assistant.native_receiver import NativeReceiver
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_semantic_inbox import NativeSemanticInbox,native_message_source_key
from app.assistant.semantic_routing import SemanticIntentInterpreter
from tests.test_assistant_native_receiver import at,reply,page,B,SEC_B,PROFILE_A,PROFILE_B
from tests import test_assistant_native_receiver as receiver_fixtures
from tests.test_assistant_native_lane import native_system
from tests.test_assistant_native_visibility_lane import case as hint_case,request
from tests.test_semantic_command_resolution import case
from tests.test_api_first_semantic_routing import enable_inbox


@pytest.mark.parametrize('sink_failure',[False,True])
def test_seventeen_registered_API_messages_are_chunked_after_post_GET_and_source_commit(tmp_path,sink_failure):
    clock=[100.0]
    source=NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:clock[0])
    for channel,group in ((9,'601'),(7,'2')):
        projection=project_page(page(),channel,receiver_uid=B,receiver_sec_uid=SEC_B,
            profile_allowed=lambda profile:profile==PROFILE_A)
        source.record_page(source.prepare_channel(channel,group),projection,next_cursor=('0','0'),has_more=False)
    assert source.arm_if_ready(True)
    clock[0]=102.0
    rows=[at(nid=str(11000+i),cid=str(21000+i),created='101',text='actual instruction '+str(i)) for i in range(17)]
    class Transport:
        identity_calls=0
        def verify_identity(self):self.identity_calls+=1;return B
        def get_groups(self):return {9:'601',7:'2'}
        def read_page(self,*args):return page(rows)
    transport=Transport();batches=[]
    def sink(messages,*,page_fingerprint):
        assert transport.identity_calls>=2  # Actual post-read session GET occurred.
        assert source.pending_count()==17  # Normal facts+cursor already committed.
        assert 1<=len(messages)<=16 and len(page_fingerprint)==64
        assert all(set(item)==set(messages[0]) and item['dispatchable'] is False for item in messages)
        batches.append(deepcopy(messages))
        if sink_failure:raise RuntimeError('synthetic local write failure')
    receiver=NativeReceiver(transport,source,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:profile==PROFILE_A,allow_live=True,
        semantic_message_sink=sink,clock=lambda:clock[0])
    assert len(receiver.poll(max_pages=1))==17
    assert [len(batch) for batch in batches]==([16] if sink_failure else [16,1])
    assert receiver.last_semantic_message_sink_error_code==('semantic_message_sink_unavailable' if sink_failure else None)
    if not sink_failure:
        assert len({item['native_comment_id'] for batch in batches for item in batch})==17


def test_missing_meaning_after_failed_cross_database_capture_does_not_spend_UI_claim(tmp_path):
    lane,source,_,adapter,*_=native_system(tmp_path)
    source.peek_ui_candidate=lambda:deepcopy(source.candidate)
    inbox=SimpleNamespace(is_processing=lambda:False,lookup=lambda key:None)
    router=SemanticIntentInterpreter(lane.registry,None,None,None)
    router.configure_native(inbox,lambda message:True,source_store=source)
    lane.semantic_intake_ready=router.native_intake_ready
    assert lane.collect()==[]
    assert source.state=='pending' and source.held==[] and adapter.native_calls==[]


def test_missing_meaning_for_explicit_pending_hint_does_not_spend_UI_claim(hint_case):
    s=hint_case;request(s)
    inbox=SimpleNamespace(is_processing=lambda:False,lookup=lambda key:None)
    router=SemanticIntentInterpreter(s.registry,None,None,None)
    router.configure_native(inbox,lambda message:True,source_store=s.source,hint_store=s.ledger)
    s.lane.semantic_intake_ready=router.native_intake_ready
    assert s.lane.collect()==[]
    row=s.ledger.get_hint(s.hid)
    assert row['state']=='pending' and row['attempts']==0 and s.adapter.native_calls==[]


def bound_native(case,tmp_path):
    inbox,message=enable_inbox(case,tmp_path)
    assert case.router.process_native_once()['state']=='understood'
    resolution=case.router(case.env,native_candidate=message,source_checkpoint=lambda:True)
    assert resolution.action=='save' and len(case.model.requests)==1
    key=native_message_source_key(message)
    task={'id':'actual-synthetic-queued-task','envelope':case.env,'binding':resolution.binding.to_dict()}
    case.router.native_source_store=SimpleNamespace(lookup_source_for_task=lambda task_id:{'source_id':key})
    assert case.router.native_authority_current(task) is True
    return inbox,message,task


def test_bound_native_then_inbox_payload_conflict_blocks_later_effects_without_second_model(case,tmp_path):
    inbox,message,task=bound_native(case,tmp_path)
    changed=deepcopy(message);changed['content']='a different instruction after admission'
    inbox.record_messages([changed],page_fingerprint='b'*64)
    assert inbox.lookup(native_message_source_key(message))['state']=='held'
    assert case.router.native_authority_current(task) is False
    assert len(case.model.requests)==1


def test_lost_complete_UI_receipt_cannot_fall_back_to_legacy_grammar(case,tmp_path,monkeypatch):
    inbox,message,task=bound_native(case,tmp_path)
    monkeypatch.setattr(case.store,'get_recorded_decision',lambda *args,**kwargs:None)
    assert inbox.lookup(native_message_source_key(message))['state']=='understood'
    assert case.router.native_authority_current(task) is False
    assert len(case.model.requests)==1


def test_paused_before_native_understanding_claims_nothing_and_has_zero_provider_calls(case,tmp_path):
    inbox,message=enable_inbox(case,tmp_path);case.stopped[0]=True
    assert case.router.process_native_once() is None
    row=inbox.lookup(native_message_source_key(message))
    assert row['state']=='pending' and row['attempts']==0
    assert case.model.requests==[] and case.budget.summary()['all_time']['request_count']==0


@pytest.mark.parametrize('already_admitted',[False,True])
def test_late_AT_for_already_planned_or_admitted_COMMENT_cannot_charge_native_meaning(tmp_path,already_admitted):
    clock=[100.0]
    source=NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:clock[0])
    def record(raw,channel):
        projection=project_page(page([raw] if raw else []),channel,receiver_uid=B,receiver_sec_uid=SEC_B,
            profile_allowed=lambda profile:profile==PROFILE_A)
        return source.record_page(source.prepare_channel(channel,'601' if channel==9 else '2'),
            projection,next_cursor=('0','0'),has_more=False,allow_live=True)
    record(None,9);record(None,7);assert source.arm_if_ready(True)
    inbox=NativeSemanticInbox.prepare(tmp_path/'meaning.sqlite3',B,PROFILE_B,
        source_authorizer=source.native_meaning_checkpoint,clock=lambda:clock[0])
    inbox.enable_new_messages()
    clock[0]=102.0;source.set_verified()
    body='@6 放到学习库'
    [candidate]=record(reply(cid='2001',created='101',text=body,parent='2000'),7)
    claim=source.claim_ui_candidate('synthetic-owner',lease_seconds=300)
    source.record_ui_plan(claim['source_id'],claim['claim_token'],receiver_fixtures.StoreTests.plan(None))
    if already_admitted:source.finish_ui_admission(claim['source_id'],claim['claim_token'],'child-task','ui-reference')
    frozen=source.get_source(claim['source_id'])
    raw=at(cid='2001',created='101',text=body)
    record(raw,9)
    [message]=project_semantic_messages(page([raw]),9,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:profile==PROFILE_A)
    assert source.native_meaning_checkpoint(message) is False
    inbox.record_messages([message],page_fingerprint='a'*64)
    assert inbox.lookup(native_message_source_key(message))['state']=='historical'
    current=source.get_source(claim['source_id'])
    assert {key:current[key] for key in ('state','plan','task_id','reference_id')}=={
        key:frozen[key] for key in ('state','plan','task_id','reference_id')}
    class NoProvider:
        calls=0
        def complete(self,*args):self.calls+=1;pytest.fail('Late channel alias is not a second paid command')
    model=NoProvider();router=SemanticIntentInterpreter(None,None,model,None)
    router.configure_native(inbox,source.native_meaning_checkpoint,source_store=source)
    assert router.process_native_once() is None and model.calls==0
