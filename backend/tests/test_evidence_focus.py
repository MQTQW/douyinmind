"""Pure current-video defaults; no I/O or authorization is issued here."""
from copy import deepcopy

import pytest

from app.assistant.evidence_focus import default_current_video
from app.mention_agent.followup_evidence import make_followup_envelope
from tests.test_conversation_followup_evidence import VIDEO, binding, detail


def followup(text):
    value=detail();value['command_text']=value['evidence']['followup_text']=text
    return make_followup_envelope(value,binding())


@pytest.mark.parametrize('text',['帮我总结下','展开讲讲','具体说说',
    '资料已整理好，请接着概括核心观点'])
def test_complete_followup_proof_defaults_omitted_subject_to_current_video(text):
    env=followup(text);before=deepcopy(env)
    assert default_current_video(env)==VIDEO and env==before


@pytest.mark.parametrize('text',['总结这个视频','当前视频说了什么','请解释这条视频',
    '本条视频主要观点','本视频总结','该视频讲了什么','当前  视频有何结论'])
def test_legacy_explicit_current_video_rule_is_unchanged(text):
    assert default_current_video({'comment_text':text,'video_id':VIDEO})==VIDEO


@pytest.mark.parametrize('text',['帮我总结下','展开讲讲','具体说说','查找我保存的资料'])
def test_plain_root_or_json_context_flag_does_not_get_new_followup_default(text):
    env={'comment_text':text,'video_id':VIDEO,'evidence':{'query_only':True,'root_chain_verified':True}}
    assert default_current_video(env) is None


@pytest.mark.parametrize('text',['不要总结这个视频','不用当前视频','不必看这条视频',
    '别看这个视频','不是这个视频','并非本视频','对比两个结论','比较它们',
    '另一个视频讲什么','其他视频呢','其它视频也讲过吗'])
def test_followup_keeps_old_negative_and_cross_video_auto_exceptions(text):
    assert default_current_video(followup(text)) is None
    assert default_current_video({'comment_text':text,'video_id':VIDEO}) is None


@pytest.mark.parametrize('key,value',[('query_only',1),('unique_count',True),
    ('evidence_version',True),('real_mention',True),('sender_verified',False),
    ('root_task_id','broken'),('prior_reply_sha256','0'*64)])
def test_overclaimed_or_forged_bool_followup_cannot_supply_even_explicit_default(key,value):
    env=followup('请总结这个视频');env['evidence'][key]=value
    assert default_current_video(env) is None


@pytest.mark.parametrize('key,value',[('sender_profile_url','https://www.douyin.com/user/other-A'),
    ('video_id','7999999000000000012'),('comment_text','不同原话')])
def test_cross_scope_followup_proof_has_no_default(key,value):
    env=followup('帮我总结下');env[key]=value
    assert default_current_video(env) is None


@pytest.mark.parametrize('value',[None,True,{},
    {'comment_text':'这个视频','video_id':True},
    {'comment_text':None,'video_id':VIDEO},
    {'comment_text':'帮我总结下','video_id':VIDEO,'evidence':{'source_kind':'conversation_followup','query_only':True}}])
def test_unknown_shape_has_no_default(value):
    assert default_current_video(value) is None
