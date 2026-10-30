"""Future-only durable clarification: synthetic identities, temporary stores."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace

import pytest

from app.assistant.clarification_sessions import ClarificationSessionStore, ClarificationError, DATABASE_NAME
from app.mention_agent.contracts import Binding, validate_envelope
from app.mention_agent.clarification_evidence import make_clarification_envelope, validate_clarification_envelope
from tests.test_conversation_followup_evidence import detail as base_detail


def sample(tmp_path):
    clock=[1000.0]
    store=ClarificationSessionStore.prepare(tmp_path/DATABASE_NAME,enabled=True,clock=lambda:clock[0])
    clock[0]=1001.0
    b=Binding('intent-v1','local-fixture','https://www.douyin.com/user/synthetic-intent-A',
        'https://www.douyin.com/user/synthetic-intent-B',7,True)
    env={'command_id':'command:future-intent','reference_id':'reference:future-intent','reference_quality':'native',
        'native_comment_id':'7999999000000000001','sender_profile_url':b.sender_profile_url,
        'receiver_profile_url':b.receiver_profile_url,'video_id':'7999999000000000011',
        'comment_text':'@助手 请帮我处理这条合成视频。','source_mode':'live_ui',
        'evidence':{'real_mention':True,'sender_verified':True,'video_verified':True,'unique_count':1,
            'observed_at_utc':datetime.now(timezone.utc).isoformat()}}
    task={'id':'1'*32,'kind':'clarify','binding':b.to_dict(),'envelope':env,'authorization':{'action':'query'},
        'user_id':'synthetic-user','device_id':'synthetic-device','created_at':1001.0}
    source={'source_id':'native:'+'a'*64,'task_id':task['id'],'state':'admitted','historical':False,
        'first_seen':1001.0,'attempts':1,'candidate':{'native_comment_id':env['native_comment_id']}}
    session=store.arm(task,source,reason_code='semantic_intent_unclear',semantic_plan_id='semantic-'+'d'*64)
    text='请说明希望保存这条视频，还是根据已有资料提问。'
    target={key:value for key,value in env.items() if key!='evidence'}
    target.update(binding_id=b.binding_id,instance_id=b.instance_id,knowledge_base_id=7)
    evidence={'submit_gate_passed':True,'exact_body_in_original_thread':True,
        'reply_sha256':hashlib.sha256(text.encode()).hexdigest(),'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    reply={'reply_id':'2'*32,'task_id':'3'*32,'kind':'clarification','state':'b_view_visible','send_token':'b'*48,
        'text':text,'target':target,'evidence':evidence,'verification':{}}
    session=store.published(session['session_id'],reply)
    return SimpleNamespace(store=store,clock=clock,binding=b,task=task,source=source,session=session,reply=reply)


def response(c, text='请保存到测试库。'):
    detail=base_detail()
    detail.update(sender_profile_url=c.binding.sender_profile_url,receiver_profile_url=c.binding.receiver_profile_url,
        video_id=c.task['envelope']['video_id'],native_comment_id='7999999000000000002',command_text=text)
    p=detail['evidence']
    p.update(instance_id=c.binding.instance_id,sender_profile_url=c.binding.sender_profile_url,
        receiver_profile_url=c.binding.receiver_profile_url,video_id=detail['video_id'],
        root_task_id=c.task['id'],root_agent_task_id='3'*32,root_reference_id=c.task['envelope']['reference_id'],
        root_comment_text=c.task['envelope']['comment_text'],root_native_comment_id=c.task['envelope']['native_comment_id'],
        prior_reply_text=c.reply['text'],prior_reply_sha256=c.reply['evidence']['reply_sha256'],
        followup_text=text,native_comment_id=detail['native_comment_id'])
    return make_clarification_envelope(detail,c.binding,c.session)


def test_feature_default_disabled_never_creates_a_store(tmp_path):
    with pytest.raises(ClarificationError,match='feature_disabled'):
        ClarificationSessionStore.prepare(tmp_path/DATABASE_NAME)
    assert not (tmp_path/DATABASE_NAME).exists()


def test_activation_survives_restart_and_response_is_once_with_immutable_handoff(tmp_path):
    c=sample(tmp_path)
    reopened=ClarificationSessionStore.prepare(c.store.path,enabled=True,clock=lambda:2000.0)
    assert reopened.activated_at==1000.0 and reopened.get(c.session['session_id'])==c.session
    assert c.store.arm(c.task,c.source,reason_code='semantic_intent_unclear',semantic_plan_id='semantic-'+'d'*64)==c.session
    env=response(c);sid='native:'+'c'*64
    claimed=c.store.claim_response(c.session['session_id'],sid,env)
    assert claimed['state']=='responding'
    assert c.store.claim_response(c.session['session_id'],sid,env)==claimed
    with pytest.raises(ClarificationError): c.store.claim_response(c.session['session_id'],'native:'+'d'*64,env)
    row={'envelope':env,'kind':'save','binding':c.binding.to_dict()}
    decision={'plan_id':'semantic-'+'e'*64,'action':'save','target_state':'one','grant_id':'synthetic-grant'}
    planned=c.store.plan(c.session['session_id'],sid,env,decision,row)
    assert c.store.plan(c.session['session_id'],sid,env,decision,row)==planned
    task={'id':'4'*32,'envelope':env}
    linked=c.store.admitted(c.session['session_id'],sid,task)
    assert linked['child_task_id']==task['id'] and linked['state']=='handed_off'
    assert c.store.admitted(c.session['session_id'],sid,task)==linked
    assert c.store.matching({**c.source['candidate'],**{key:c.task['envelope'][key] for key in
        ('sender_profile_url','receiver_profile_url','video_id')},'parent_id_hint':c.source['candidate']['native_comment_id']})==[]


@pytest.mark.parametrize('problem',['old_task','old_source','historical','attempt2','wrong_reason','already_held'])
def test_old_or_consumed_admissions_cannot_gain_a_new_capability(tmp_path,problem):
    c=sample(tmp_path);task,source=deepcopy(c.task),deepcopy(c.source)
    task['id']='5'*32;source['task_id']=task['id'];source['source_id']='native:'+'f'*64
    reason='semantic_intent_unclear'
    if problem=='old_task':task['created_at']=999.0
    elif problem=='old_source':source['first_seen']=999.0
    elif problem=='historical':source['historical']=True
    elif problem=='attempt2':source['attempts']=2
    elif problem=='wrong_reason':reason='no_evidence'
    else:source['state']='held'
    with pytest.raises(ClarificationError):c.store.arm(task,source,reason_code=reason,semantic_plan_id='semantic-'+'d'*64)
    assert c.store.for_root(task['id']) is None


@pytest.mark.parametrize('change',['unknown','no_token','bad_hash','bad_target'])
def test_only_an_exact_published_clarification_can_open_a_session(tmp_path,change):
    c=sample(tmp_path);reply=deepcopy(c.reply)
    if change=='unknown':reply['state']='delivery_unknown'
    elif change=='no_token':reply['send_token']=None
    elif change=='bad_hash':reply['evidence']['reply_sha256']='0'*64
    else:reply['target']['comment_text']='different synthetic root'
    with pytest.raises(ClarificationError):c.store.published(c.session['session_id'],reply)
    assert c.store.get(c.session['session_id'])==c.session


def test_new_typed_evidence_is_not_a_real_mention_or_query_only_followup(tmp_path):
    c=sample(tmp_path);env=response(c)
    assert env['comment_text']=='请保存到测试库。' and env['evidence']['real_mention'] is False
    assert env['evidence']['source_kind']=='intent_clarification_reply' and 'query_only' not in env['evidence']
    assert env['reference_id'].startswith('clarification-reply:')
    assert validate_envelope(env,c.binding)==env
    assert validate_clarification_envelope(env,c.binding)==env
    altered=deepcopy(env);altered['comment_text']='changed raw text'
    with pytest.raises(ValueError):validate_envelope(altered,c.binding)
    altered=deepcopy(env);altered['evidence']['allowed_actions'].append('delete')
    with pytest.raises(ValueError):validate_envelope(altered,c.binding)
