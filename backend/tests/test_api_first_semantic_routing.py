"""Owned API meaning inbox and Registry; fake provider, no browser/network."""
from copy import deepcopy
import pytest
from tests.test_semantic_command_resolution import case
from tests.test_assistant_native_receiver import at,page
from app.assistant.native_events import project_ui_review_hints
from app.assistant.native_semantic_inbox import NativeSemanticInbox,native_message_source_key
from app.assistant.semantic_plan_store import decision_metadata


def api_message(case):
    raw=at(created='101',text=case.env['comment_text'],sec=case.env['sender_profile_url'].rsplit('/',1)[-1])
    raw['aweme_id']=case.env['video_id'];raw['at']['aweme']['aweme_id']=case.env['video_id']
    raw['at']['schema_url']='aweme://aweme/detail/'+case.env['video_id']+'?cid=7999999000000000099'
    raw['at']['comment_status']=42
    [message]=project_ui_review_hints(page([raw]),9,receiver_uid='9001',
        receiver_sec_uid=case.env['receiver_profile_url'].rsplit('/',1)[-1],profile_allowed=lambda p:True)
    return message


def enable_inbox(case,tmp_path):
    inbox=NativeSemanticInbox.prepare(tmp_path/'native-meaning.sqlite3','9001',case.env['receiver_profile_url'],
        source_authorizer=lambda message:True,clock=lambda:100.0)
    inbox.enable_new_messages();case.router.configure_native(inbox,lambda message:True)
    message=api_message(case);inbox.record_messages([message],page_fingerprint='a'*64)
    return inbox,message


def test_unknown_api_status_is_understood_before_ui_then_same_receipt_authorizes_after_ui(case,tmp_path):
    inbox,message=enable_inbox(case,tmp_path)
    assert case.model.requests==[] and case.router.native_intake_ready() is False
    assert case.router.process_native_once()['state']=='understood'
    assert len(case.model.requests)==1
    assert case.model.requests[0][0][-1]['content']==message['content']
    assert case.store.get_recorded_decision(case.env) is None
    # A paid meaning alone cannot authorize saving without the actual UI checkpoint.
    denied=case.router(case.env,native_candidate=message,source_checkpoint=lambda:False)
    assert denied.action!='save' and len(case.model.requests)==1
    result=case.router(case.env,native_candidate=message,source_checkpoint=lambda:True)
    assert result.action=='save' and result.binding.knowledge_base_id==5
    receipt=case.store.get_recorded_decision(case.env,binding=result.binding)
    assert decision_metadata(receipt)['mode']=='root'
    assert len(case.model.requests)==1 and case.budget.summary()['all_time']['request_count']==1
    assert case.router(case.env,native_candidate=message,source_checkpoint=lambda:True).action=='save'
    assert len(case.model.requests)==1 and case.router.native_intake_ready() is True


@pytest.mark.parametrize('action,expected',[('query','query'),('save','clarify')])
def test_api_interpretation_keeps_query_all_and_save_ask_defaults(case,tmp_path,action,expected):
    case.model.decision={'action':action,'target_state':'unspecified','grant_id':None}
    inbox,message=enable_inbox(case,tmp_path)
    assert case.router.process_native_once()['state']=='understood'
    result=case.router(case.env,native_candidate=message,source_checkpoint=lambda:True)
    assert result.action==expected and len(case.model.requests)==1
    if action=='query':assert result.binding.is_all_local
    else:assert result.reason_code=='save_library_required' and result.catalog_count==2


def test_native_payload_conflict_cannot_be_reinterpreted_or_saved(case,tmp_path):
    inbox,message=enable_inbox(case,tmp_path)
    assert case.router.process_native_once()['state']=='understood'
    changed=deepcopy(message);changed['content']='现在改成另一个保存指令'
    inbox.record_messages([changed],page_fingerprint='b'*64)
    assert inbox.lookup(native_message_source_key(message))['state']=='held'
    result=case.router(case.env,native_candidate=message,source_checkpoint=lambda:True)
    assert result.action!='save' and len(case.model.requests)==1


def test_registry_revoke_after_api_understanding_blocks_save_without_second_model(case,tmp_path):
    inbox,message=enable_inbox(case,tmp_path)
    assert case.router.process_native_once()['state']=='understood'
    case.registry.revoke_grant(case.grant['grant_id'])
    result=case.router(case.env,native_candidate=message,source_checkpoint=lambda:True)
    assert result.action!='save' and len(case.model.requests)==1
