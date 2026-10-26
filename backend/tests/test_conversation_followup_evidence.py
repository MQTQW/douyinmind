"""Ordinary query follow-up contract; no browser/account/network/database."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib

import pytest

from app.mention_agent.contracts import Binding,validate_envelope
from app.mention_agent.followup_evidence import make_followup_envelope,validate_followup_envelope

A='https://www.douyin.com/user/followup-A';B='https://www.douyin.com/user/followup-B'
VIDEO='7999999000000000011';QUESTION='这个视频讲了什么事情';PRIOR='这个视频已加入测试长视频，文字稿和资料已就绪。'


def binding():return Binding('followup-all','followup-instance',A,B,None,True,query_scope='all_local')


def detail():
    proof={'source_kind':'conversation_followup','evidence_version':1,'query_only':True,'instance_id':'followup-instance',
        'real_mention':False,'sender_verified':True,'receiver_verified':True,'video_verified':True,
        'current_receiver_verified':True,'unique_count':1,'root_chain_verified':True,'prior_reply_visible_unique':True,
        'reply_to_receiver_verified':True,'direct_reply_to_prior':False,'parent_scope':'unique_followup_in_verified_root',
        'sender_profile_url':A,'receiver_profile_url':B,'video_id':VIDEO,'root_task_id':'a'*32,'root_agent_task_id':'b'*32,
        'root_reference_id':'reference:original','root_comment_text':'@6 帮我加入知识库','root_native_comment_id':None,
        'prior_reply_native_comment_id':None,'prior_reply_text':PRIOR,'prior_reply_sha256':hashlib.sha256(PRIOR.encode()).hexdigest(),
        'followup_reference_id':'pending:derive','followup_text':QUESTION,'native_comment_id':None,
        'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    return {'ok':True,'sender_profile_url':A,'receiver_profile_url':B,'video_id':VIDEO,
        'command_text':QUESTION,'native_comment_id':None,'evidence':proof}


def test_all_local_query_followup_is_not_a_new_mention_or_library_choice():
    env=make_followup_envelope(detail(),binding())
    assert validate_envelope(env,binding())==env
    assert env['native_comment_id'] is None and env['reference_quality']=='verified_ui'
    assert env['comment_text']==QUESTION and env['evidence']['real_mention'] is False
    assert env['evidence']['query_only'] is True and 'choice_id' not in env['evidence']


@pytest.mark.parametrize('key,value',[
    ('real_mention',True),('query_only',False),('root_chain_verified',False),('prior_reply_visible_unique',False),
    ('reply_to_receiver_verified',False),('current_receiver_verified',False),('unique_count',True),('unique_count',2),
    ('parent_scope','verified_ask_parent'),('direct_reply_to_prior',True),('prior_reply_sha256','0'*64),
    ('root_task_id','not-task'),('root_agent_task_id','not-agent'),('observed_at_utc','2026-10-26T00:00:00'),
    ('native_comment_id','1234567'),('root_native_comment_id','not-cid')])
def test_invalid_or_overclaimed_followup_proof_rejected(key,value):
    env=make_followup_envelope(detail(),binding());env['evidence'][key]=value
    with pytest.raises(ValueError):validate_followup_envelope(env,binding())


@pytest.mark.parametrize('field,value',[('sender_profile_url',B),('video_id','7999999000000000012'),('comment_text','别的正文')])
def test_cross_scope_or_rewritten_raw_body_rejected(field,value):
    env=make_followup_envelope(detail(),binding());env[field]=value
    with pytest.raises(ValueError):validate_envelope(env,binding())


def test_same_raw_question_in_another_root_has_another_reference():
    first=make_followup_envelope(detail(),binding());other=detail()
    other['evidence']['root_reference_id']='reference:another'
    second=make_followup_envelope(other,binding())
    assert first['reference_id']!=second['reference_id']


def test_native_CID_is_observed_only_and_cannot_equal_ancestor():
    value=detail();value['native_comment_id']=value['evidence']['native_comment_id']='7999999000000000099'
    env=make_followup_envelope(value,binding());assert env['reference_quality']=='native'
    env['evidence']['root_native_comment_id']=env['native_comment_id']
    with pytest.raises(ValueError):validate_followup_envelope(env,binding())
