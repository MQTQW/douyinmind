"""Choice-specific reply identity and additive all-local history, no live I/O."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from app.assistant.conversation import ConversationStore
from app.mention_agent.contracts import Binding
from app.mention_agent.store import AgentStore, AgentStoreError
from tests.library_choice_fixture import choice_fixture, visible, selection, reserve
from tests.test_mention_agent_store import binding, envelope, proposal


def now():
    return datetime.now(timezone.utc).isoformat()


def choice_ready(tmp_path, *, parent_scope='verified_ask_parent', native=None):
    case=choice_fixture(tmp_path,source_mode='live_ui');visible(case)
    env=selection(case,parent_scope=parent_scope,native=native);reserve(case,envelope=env)
    store=AgentStore(tmp_path/'agent.sqlite3');selected=case.selected_bindings[7]
    store.put_binding(selected.to_dict())
    task=store.enqueue(selected.binding_id,env);claim=store.claim(task['task_id'])
    reply=store.complete(task['task_id'],claim['claim_token'],proposal(kind='clarification',source_ids=[],sources=[]))
    verification={**env['evidence'],**{key:env[key] for key in (
        'sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','native_comment_id')},
        'current_receiver_verified':True,'observed_at_utc':now()}
    return case,env,store,reply,verification


@pytest.mark.parametrize('parent_scope',['verified_ask_parent','unique_pending_choice_in_verified_root'])
@pytest.mark.parametrize('native',[None,'7999999000000000044'])
def test_original_plain_selection_and_frozen_parent_survive_agent_and_one_send(tmp_path,parent_scope,native):
    case,env,store,reply,proof=choice_ready(tmp_path,parent_scope=parent_scope,native=native)
    assert reply['target']['comment_text']=='1' and reply['target']['native_comment_id']==native
    assert reply['target']['choice_evidence']==env['evidence']
    assert proof['real_mention'] is False
    reserved=store.reserve_send(reply['task_id'],proof)
    assert reserved is not None and reserved['state']=='reserved'
    assert store.reserve_send(reply['task_id'],proof) is None
    done=store.finish_send(reply['task_id'],reserved['send_token'],'delivery_unknown',{'reason':'no_readback'})
    assert done['state']=='delivery_unknown' and store.reserve_send(reply['task_id'],proof) is None


@pytest.mark.parametrize('field,value',[
    ('root_reference_id','wrong-root'),('root_comment_text','wrong original save'),
    ('ask_ref','wrong-ask'),('ask_reply_ref','wrong-reply'),('ask_text','wrong prompt'),
    ('choice_id','f'*64),('instance_id','another-computer'),('source_kind','ordinary_reply'),
    ('parent_scope','verified_ask_parent'),('direct_reply_to_ask',True),
    ('root_chain_verified',False),('ask_visible_unique',False),
    ('receiver_reply_target_verified',False),('real_mention',True),('unique_count',True),
])
def test_publication_cannot_replace_original_parent_or_overclaim_weak_parent(tmp_path,field,value):
    _,_,store,reply,proof=choice_ready(tmp_path,parent_scope='unique_pending_choice_in_verified_root')
    proof[field]=value
    assert store.reserve_send(reply['task_id'],proof) is None
    assert store.get_reply(reply['task_id'])['send_token'] is None


def test_plain_reply_without_choice_proof_keeps_strict_mention_rule(tmp_path):
    store=AgentStore(tmp_path/'agent.sqlite3');store.put_binding(binding())
    env=envelope();env['evidence']['real_mention']=False
    with pytest.raises(AgentStoreError,match='incomplete_mention_evidence'):
        store.enqueue('fixture-binding',env)
    assert store.list_tasks()==[]


def test_same_native_reply_cannot_borrow_task_in_another_parent(tmp_path):
    _,env,store,reply,_=choice_ready(tmp_path,native='7999999000000000044')
    other=deepcopy(env);other['evidence']['root_reference_id']='another-original-root'
    with pytest.raises(AgentStoreError,match='command_conflict|reference_conflict'):
        store.enqueue('selected-7',other)
    assert len(store.list_tasks())==1 and store.get_reply(reply['task_id'])['state']=='ready'


def test_choice_command_for_selected_binding_cannot_be_enqueued_under_other_library(tmp_path):
    case=choice_fixture(tmp_path);visible(case);env=selection(case)
    store=AgentStore(tmp_path/'agent.sqlite3');store.put_binding(case.selected_bindings[8].to_dict())
    with pytest.raises(AgentStoreError,match='invalid_choice_binding'):
        store.enqueue('selected-8',env)
    assert store.list_tasks()==[]


def test_held_choice_requires_fresh_same_parent_and_releases_only_once(tmp_path):
    _,_,store,reply,proof=choice_ready(tmp_path)
    store.hold_reply(reply['task_id'],'preflight_verification_required')
    wrong={**proof,'ask_reply_ref':'wrong'}
    with pytest.raises(AgentStoreError,match='hold_reason_not_releasable'):
        store.release_held_reply(reply['task_id'],'operator_verified',verification=wrong)
    released=store.release_held_reply(reply['task_id'],'operator_verified',verification=proof)
    assert released['state']=='ready' and released['target']['choice_evidence']['real_mention'] is False
    assert store.reserve_send(reply['task_id'],proof) is not None


def all_binding(**changes):
    return Binding.from_dict(binding(binding_id='explicit-all',knowledge_base_id=None,query_scope='all_local',**changes))


def bind(history,task,scope,env=None,thread='root-thread'):
    env=env or envelope(command_id=task,reference_id=task)
    history.bind_task(task,scope,env,thread,{'quality':'verified_ancestor','root_reference_id':thread})
    return env


def published(history,task,scope,text):
    return history.record_published(task,scope,{'kind':'answer','state':'b_view_visible','text':text,'sources':[]})


def original_rows(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        return {name:db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall()
                for name in ('comment_context_tasks','comment_context_answers')}


def test_all_binding_requires_explicit_scope_and_never_replaces_legacy_payload(tmp_path):
    store=AgentStore(tmp_path/'agent.sqlite3');old=store.put_binding(binding())
    for wrong in (binding(knowledge_base_id=None),binding(knowledge_base_id=0),
                  binding(knowledge_base_id=7,query_scope='all_local')):
        with pytest.raises(AgentStoreError):store.put_binding(wrong)
    all_scope=all_binding();store.put_binding(all_scope.to_dict())
    assert store.get_binding(old['binding_id'])==old and 'query_scope' not in old
    assert store.get_binding(all_scope.binding_id)['knowledge_base_id'] is None
    with pytest.raises(AgentStoreError,match='binding_conflict'):
        store.put_binding({**all_scope.to_dict(),'binding_id':old['binding_id']})
    task=store.enqueue(all_scope.binding_id,envelope());claim=store.claim(task['task_id'])
    reply=store.complete(task['task_id'],claim['claim_token'],proposal())
    assert reply['target']['knowledge_base_id'] is None and reply['target']['query_scope']=='all_local'


def test_scope_migration_is_explicit_additive_and_never_imports_legacy_answers(tmp_path):
    path=tmp_path/'conversation.sqlite3';history=ConversationStore(path)
    old=Binding.from_dict(binding());all_scope=all_binding()
    bind(history,'legacy',old);assert published(history,'legacy',old,'仅旧库的历史')
    before=original_rows(path)
    with pytest.raises(ValueError,match='migration_required'):bind(history,'all-1',all_scope)
    assert original_rows(path)==before
    assert history.prepare_scope_v2() is True and history.prepare_scope_v2() is False
    assert original_rows(path)==before
    bind(history,'all-1',all_scope)
    assert history.context_for('all-1',all_scope) is None
    assert history.task_scope('all-1')['library_id'] is None
    assert history.task_scope('all-1')['query_scope']=='all_local'
    assert published(history,'all-1',all_scope,'全部资料范围的新历史')
    bind(history,'all-2',all_scope)
    assert history.context_for('all-2',all_scope)['previous_answer']=='全部资料范围的新历史'
    assert history.context_for('all-2',old) is None
    bind(history,'legacy-2',old)
    assert history.context_for('legacy-2',old)['previous_answer']=='仅旧库的历史'
    assert original_rows(path)['comment_context_tasks'][:1]==before['comment_context_tasks']
    assert before['comment_context_answers'][0] in original_rows(path)['comment_context_answers']


def test_reopen_keeps_all_scope_and_rejects_cross_scope_task_id_reuse(tmp_path):
    path=tmp_path/'conversation.sqlite3';history=ConversationStore(path);history.prepare_scope_v2()
    old=Binding.from_dict(binding());all_scope=all_binding()
    bind(history,'all-1',all_scope);published(history,'all-1',all_scope,'全部范围回答')
    reopened=ConversationStore(path);bind(reopened,'all-2',all_scope)
    assert reopened.context_for('all-2',all_scope)['previous_answer']=='全部范围回答'
    with pytest.raises(ValueError,match='thread_scope_conflict'):bind(reopened,'all-1',old)
    assert not reopened.record_published('all-1',old,{'kind':'answer','state':'b_view_visible','text':'跨范围','sources':[]})
    for scope in (replace(all_scope,binding_id='other'),replace(all_scope,instance_id='other'),
                  replace(all_scope,sender_profile_url='https://www.douyin.com/user/another')):
        assert reopened.context_for('all-2',scope) is None


@pytest.mark.parametrize('damage',['missing_table','missing_marker','wrong_marker'])
def test_partial_or_corrupt_scope_migration_is_not_recreated(tmp_path,damage):
    path=tmp_path/'conversation.sqlite3';history=ConversationStore(path);history.prepare_scope_v2()
    with sqlite3.connect(path) as db:
        if damage=='missing_table':db.execute('DROP TABLE comment_context_all_tasks')
        elif damage=='missing_marker':db.execute("DELETE FROM context_meta WHERE key='all_local_scope_version'")
        else:db.execute("UPDATE context_meta SET value='future' WHERE key='all_local_scope_version'")
    before=path.read_bytes()
    with pytest.raises(ValueError,match='migration_incomplete'):history.prepare_scope_v2()
    assert path.read_bytes()==before


def test_clarification_never_enters_answer_history_and_choice_body_stays_plain(tmp_path):
    case=choice_fixture(tmp_path);visible(case);env=selection(case);scope=case.selected_bindings[7]
    history=ConversationStore(tmp_path/'conversation.sqlite3')
    bind(history,'choice',scope,env,env['reference_id'])
    assert not history.record_published('choice',scope,{'kind':'clarification','state':'b_view_visible','text':'请选库','sources':[]})
    assert history.roots(scope,env['video_id'])==[]
    assert json.loads(history.task_scope('choice')['envelope'])['comment_text']=='1'


def test_additive_scope_migration_and_all_history_are_serialized_across_store_instances(tmp_path):
    path=tmp_path/'conversation.sqlite3';ConversationStore(path)
    scope=all_binding()
    def actor(index):
        history=ConversationStore(path);migrated=history.prepare_scope_v2()
        for number in range(6):bind(history,f'{index}-{number}',scope)
        return migrated
    with ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(actor,range(3)))
    assert results.count(True)==1 and results.count(False)==2
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('SELECT count(*) FROM comment_context_all_tasks').fetchone()[0]==18
        assert db.execute('SELECT count(*) FROM comment_context_tasks').fetchone()[0]==0
