"""Published same-thread text can resolve references without changing all scope.

Temporary context ledger only. No business DB, model, account, browser or RAG.
An instance ID represents the physical local device in the present Binding API.
"""
from dataclasses import replace
from datetime import datetime,timezone

import pytest

from app.assistant.conversation import ConversationStore
from app.mention_agent.contracts import Binding


A='https://www.douyin.com/user/cross-context-A'
B='https://www.douyin.com/user/cross-context-B'
OTHER='https://www.douyin.com/user/cross-context-other'
VIDEO='7999999000000000011';OTHER_VIDEO='7999999000000000022'
QUESTION='@虚构B 根据编程库解释视频的两点做法。'
ANSWER='第一点是先检查输入。第二点是验证改动后再提交。'


def envelope(binding,reference,text,video=VIDEO):
    return {'command_id':'command-'+reference,'reference_id':reference,'reference_quality':'verified_ui',
        'native_comment_id':None,'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'video_id':video,'comment_text':text,'source_mode':'isolated',
        'evidence':{'real_mention':True,'sender_verified':True,'video_verified':True,'unique_count':1,
                    'observed_at_utc':datetime.now(timezone.utc).isoformat()}}


def setup(tmp_path,*,prior_state='b_view_visible',prior_public=True,current_change=None,proof_quality='verified_ancestor',thread=None,video=VIDEO):
    store=ConversationStore(tmp_path/'context.sqlite3')
    store.prepare_scope_v2()
    prior=Binding('same-device-library7','same-instance',A,B,7,prior_public)
    original=envelope(prior,'original-root',QUESTION)
    store.bind_task('prior-published-task',prior,original,original['reference_id'],
                    {'quality':'current_comment','root_reference_id':original['reference_id']})
    recorded=store.record_published('prior-published-task',prior,
        {'state':prior_state,'kind':'answer','text':ANSWER,'sources':[{'source_id':'资料1','chunk_id':VIDEO+':1'}]})
    current=Binding('same-device-all','same-instance',A,B,None,True,query_scope='all_local')
    if current_change:current=replace(current,**current_change)
    followup=envelope(current,'new-followup','@虚构B 第二点呢？',video=video)
    root=thread or original['reference_id']
    store.bind_task('new-all-task',current,followup,root,{'quality':proof_quality,'root_reference_id':root})
    return store,prior,current,followup,recorded


def test_verified_same_thread_library_to_all_keeps_reference_context_without_inheriting_library(tmp_path):
    store,prior,current,followup,recorded=setup(tmp_path)
    assert recorded is True
    assert current.is_all_local and current.knowledge_base_id is None and prior.knowledge_base_id==7
    context=store.context_for('new-all-task',current)
    assert context=={'prior_questions':[QUESTION],'previous_answer':ANSWER}
    assert store.task_scope('new-all-task')['query_scope']=='all_local'
    assert store.task_scope('new-all-task')['library_id'] is None
    assert followup['comment_text']=='@虚构B 第二点呢？'


def test_all_roots_include_old_named_public_root_for_fresh_dom_ancestor_verification(tmp_path):
    store,_,current,_,recorded=setup(tmp_path)
    assert recorded is True
    assert store.roots(current,VIDEO)==[{'reference_id':'original-root','comment_text':QUESTION}]


@pytest.mark.parametrize('change',[
    {'thread':'different-root'},
    {'current_change':{'sender_profile_url':OTHER}},
    {'current_change':{'receiver_profile_url':OTHER}},
    {'current_change':{'instance_id':'different-physical-device'}},
    {'video':OTHER_VIDEO},
    {'proof_quality':'independent'},
])
def test_other_thread_person_receiver_device_video_or_unverified_parent_cannot_supply_cross_scope_history(tmp_path,change):
    store,_,current,_,recorded=setup(tmp_path,**change)
    assert recorded is True
    assert store.context_for('new-all-task',current) is None
    if 'current_change' in change or 'video' in change:
        assert store.roots(current,change.get('video',VIDEO))==[]


@pytest.mark.parametrize('prior_state,prior_public',[
    ('ready',True),('delivery_unknown',True),('completed_private',False),
])
def test_unpublished_unknown_and_private_source_answer_never_become_cross_scope_reference(tmp_path,prior_state,prior_public):
    store,_,current,_,recorded=setup(tmp_path,prior_state=prior_state,prior_public=prior_public)
    assert recorded is False
    assert store.context_for('new-all-task',current) is None
    assert store.roots(current,VIDEO)==[]


def test_explicit_other_library_must_not_get_named_or_all_reference_history(tmp_path):
    store,_,_,_,_=setup(tmp_path)
    named=Binding('same-device-library8','same-instance',A,B,8,True)
    current=envelope(named,'named-followup','@虚构B 只根据经济库解释第二点。')
    store.bind_task('named-other-task',named,current,'original-root',
                    {'quality':'verified_ancestor','root_reference_id':'original-root'})
    assert store.context_for('named-other-task',named) is None
    assert store.roots(named,VIDEO)==[]
