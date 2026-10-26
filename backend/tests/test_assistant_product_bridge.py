"""Actual queue/registry/legacy bridge with fake providers and a fake UI."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from app.assistant.browser_lane import BrowserLane
from app.assistant.commands import CommandResolver
from app.assistant.conversation import ConversationStore
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.queue import AssistantQueue
from app.assistant.registry import Registry
from app.assistant.runtime import AssistantRuntime
from app.mention_agent.store import AgentStore
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel
from tests.test_mention_agent_import_end_to_end import scope, Backend, Model, Knowledge, Adapter


class Control:
    desired = 'running'
    def snapshot(self):
        return {'desired_state':self.desired,'run_id':'run-test'}


def system(tmp_path, *, public=True):
    binding,agent,env=scope(tmp_path)
    registry=Registry(tmp_path/'registry.sqlite3',catalog_reader=lambda:[{'id':7,'name':'测试库'},{'id':8,'name':'另一个库'}])
    migrated=registry.import_legacy(binding,verified_by='fictional-test-owner',allow_import=True,aliases=['编程'])
    if not public:
        registry.grant_library(migrated['user_id'],migrated['device_id'],7,aliases=['编程'],
            can_query=True,can_import=True,public_reply_allowed=False,is_default=True)
    queue=AssistantQueue(tmp_path/'queue.sqlite3')
    conversation=ConversationStore(tmp_path/'conversation.sqlite3')
    control=Control()
    return binding,agent,env,registry,migrated,queue,conversation,control


def receive(queue,history,registry,env):
    selected=CommandResolver(registry).resolve(env)
    assert selected.binding is not None and selected.authorization is not None
    task=queue.enqueue(selected.binding,env,kind=selected.action,user_id=selected.user_id,
        device_id=selected.device_id,authorization=selected.authorization,video_title='虚构视频')
    history.bind_task(task['id'],selected.binding,env,env['reference_id'],
        {'quality':'current_comment','root_reference_id':env['reference_id']})
    return task


def test_real_bridge_query_then_owner_publication_once(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    e['comment_text']='@助手 请根据编程知识库说明Git。'
    task=receive(q,h,r,e)
    model=QueryModel();adapter=Adapter()
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter)
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    ready=runtime.execute_once()
    assert ready['state']=='ready_to_publish' and len(model.requests)==2
    assert q.public_task(ready,detail=True)['details']['sources'][0]['time_range']=='00:17–00:55'
    visible=runtime.publish_once()
    assert visible['state']=='b_view_visible' and adapter.submits==1
    assert runtime.publish_once() is None
    assert h.roots(b,e['video_id'])
    reopened=AssistantQueue(q.path)
    assert reopened.claim_next('execute') is None and reopened.claim_next('publish') is None


def test_nonpublic_grant_executes_locally_without_any_publish_claim(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path,public=False)
    e['comment_text']='@助手 请根据编程知识库说明Git。'
    task=receive(q,h,r,e)
    model=QueryModel();adapter=Adapter()
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter)
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    private=runtime.execute_once()
    assert private['state']=='completed_private' and len(model.requests)==2
    assert runtime.publish_once() is None and adapter.submits==0
    assert q.public_task(private,detail=True)['details']['reply_text']
    # Granting public permission later never upgrades that private old answer.
    r.grant_library(m['user_id'],m['device_id'],7,aliases=['编程'],can_query=True,can_import=True,
                    public_reply_allowed=True,is_default=True)
    assert runtime.publish_once() is None and adapter.submits==0


def test_waiting_video_releases_execution_lane_and_finishes_without_paid_replay(tmp_path,monkeypatch):
    b,a,e,r,m,q,h,c=system(tmp_path)
    class Pending(Backend):
        done=False
        def _response(self):
            return dict(self.body,state='succeeded' if self.done else 'processing',membership_exists=True,
                cache_status='done' if self.done else 'transcribing',indexed_content_chunks=2 if self.done else 0)
    backend=Pending();import_model=Model([('save_current_video',{'knowledge_base_id':7}),('propose_import_status',{})]);query_model=QueryModel()
    models=iter([import_model,query_model]);adapter=Adapter()
    save=receive(q,h,r,{**e,'comment_text':'@助手 存到编程库'})
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        import_client=backend,model_factory=lambda:next(models),knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter)
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    waiting=runtime.execute_once()
    assert waiting['state']=='waiting_ingestion' and backend.posts==1
    # This legacy per-library fixture has no all-local owner grant. Keep the
    # queue-concurrency test on an explicitly selected, authorized library.
    question=receive(q,h,r,{**e,'command_id':'query-two','reference_id':'query-two','comment_text':'@助手 根据编程知识库，Git的工作区是什么？'})
    assert runtime.execute_once()['id']==question['id']
    assert q.get_task(question['id'])['state']=='ready_to_publish'
    backend.done=True
    from app.assistant import queue as queue_module
    now=time.time()
    monkeypatch.setattr(queue_module.time,'time',lambda:now+20)
    finished=runtime.poll_waiting_once()
    assert finished['id']==save['id'] and finished['state']=='ready_to_publish'
    assert backend.posts==1 and import_model.count==1 and len(query_model.requests)==2
    updates=a.list_import_reply_updates(finished['agent_task_id'])
    assert len(updates)==1 and updates[0]['reply']['import_state']=='processing'


def test_revoke_after_admission_prevents_model_and_import(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    task=receive(q,h,r,e)
    r.revoke_grant(m['grant_id'])
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:pytest.fail('revoked task must not instantiate model'),knowledge_factory=lambda _:pytest.fail('must not query'))
    result=AssistantRuntime(q,c,execute=bridge.execute).execute_once()
    assert result['state']=='needs_review' and result['reason_code']=='binding_changed'
    assert a.list_tasks()==[]


def test_unknown_library_is_fixed_clarification_with_zero_model_requests(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    task=receive(q,h,r,{**e,'comment_text':'@助手 存到并不存在的知识库'})
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:pytest.fail('clarification does not need a model'),knowledge_factory=lambda _:pytest.fail('no retrieval'))
    result=AssistantRuntime(q,c,execute=bridge.execute).execute_once()
    assert result['state']=='ready_to_publish'
    assert result['usage']['model_responses']==0
    assert '明确' in result['details']['reply_text']


class FakePage:
    def __init__(self):
        self.navigations=[];self.keys=[]
        self.keyboard=SimpleNamespace(press=lambda key:self.keys.append(key))
    def goto(self,url,**kwargs):
        self.navigations.append(url)


class CaptureAdapter:
    def __init__(self,binding):
        self.expected_profile_url=binding.receiver_profile_url
        self.page=FakePage();self.notices=[];self.details=0
    def begin_cycle(self):pass
    def check_intervention(self):pass
    def identity(self):return object()
    def notifications(self):return deepcopy(self.notices)
    def conversation_reference(self,envelope,roots):
        return {'quality':'current_comment','root_reference_id':envelope['reference_id']}
    def detail(self,notice,keep_open=False):
        self.details+=1
        proof={'sender_profile_url':notice['source_profile_url'],'receiver_profile_url':self.expected_profile_url,
               'command_text':notice['exact_text'],'native_comment_id':None,'unique_count':1,'video_id':'7999999000000000011',
               'real_mention':True,'sender_verified':True,'video_verified':True}
        return {'ok':True,**proof,'evidence':proof,'video_title':'虚构视频',
            'source_url':'https://www.douyin.com/video/7999999000000000011',
            'diagnostics':{'id_conflict':False,'command_match_count':1,'visible_video_ids':['7999999000000000011']}}


def test_capture_baseline_ignores_history_then_admits_and_remembers_new_comment(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    adapter=CaptureAdapter(b);lane=BrowserLane(adapter,r,q,h)
    old={'source_profile_url':b.sender_profile_url,'exact_text':'@助手 旧问题','semantic_fingerprint':'cover-old'}
    adapter.notices=[old]
    assert lane.collect()==[] and lane.last_baseline_created
    fresh={'source_profile_url':b.sender_profile_url,'exact_text':'@助手 解释Git','semantic_fingerprint':'cover-new'}
    adapter.notices=[fresh,old]
    rows=lane.collect()
    assert len(rows)==1 and not lane.last_baseline_created and adapter.details==1
    task=q.enqueue(**rows[0]);lane.admitted([task])
    assert h.task_scope(task['id']) is not None
    assert lane.collect()==[] and adapter.details==1
    # A new BrowserLane object does not re-baseline away the next unseen item.
    adapter.notices.insert(0,{**fresh,'exact_text':'@助手 新的第二问','semantic_fingerprint':'cover-next'})
    assert len(BrowserLane(adapter,r,q,h).collect())==1


def test_unverified_sender_is_never_opened_or_executed(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    adapter=CaptureAdapter(b);lane=BrowserLane(adapter,r,q,h)
    assert lane.collect()==[]
    adapter.notices=[{'source_profile_url':'https://www.douyin.com/user/unverified',
                     'exact_text':'@助手 存到编程库','semantic_fingerprint':'cover-unknown'}]
    assert lane.collect()==[] and adapter.details==0
    assert not any(q.counts().values())
