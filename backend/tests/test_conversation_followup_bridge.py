"""Owned temporary ledgers; synthetic providers/UI only, never live accounts."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest

from app.assistant.commands import CommandResolver
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from app.assistant.semantic_plan_store import SemanticPlanStore
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.engine import AgentEngine
from app.mention_agent.followup_evidence import make_followup_envelope
from app.mention_agent.model import ModelTurn
from app.mention_agent.publisher import ReplyPublisher
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore, AgentStoreError, _canonical_reference, _verified_target
from tests.test_assistant_product_bridge import system, receive
from tests.test_mention_agent_import_end_to_end import Backend, Model, Knowledge, Adapter


class FollowupAdapter(Adapter):
    def prepare_reply(self, target, text):
        if 'followup_evidence' not in target:
            return super().prepare_reply(target, text)
        assert target['followup_evidence']['real_mention'] is False
        return {**deepcopy(target['followup_evidence']),
            **{k: target[k] for k in ('sender_profile_url','receiver_profile_url','video_id',
                                     'comment_text','reference_id','native_comment_id')},
            'observed_at_utc': datetime.now(timezone.utc).isoformat()}


class Planner:
    def __init__(self, action='query', grant=None):
        self.calls=0;self.action=action;self.grant=grant
    def complete(self,messages,tools):
        self.calls+=1
        return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'meaning-once',
            'type':'function','function':{'name':'submit_intent','arguments':json.dumps({
                'action':self.action,'target_state':'one' if self.grant else 'unspecified',
                'grant_id':self.grant})}}]},
            {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')


class AllKnowledge:
    def __init__(self):self.questions=[]
    def describe(self):
        return {'id':None,'name':'全部入库资料','total_count':2,'ready_count':2,'query_scope':'all_local'}
    def query(self,question):
        self.questions.append(question)
        return {'status':'ok','knowledge_base_id':None,'knowledge_base_name':'全部入库资料',
            'query_scope':'all_local','evidence':[{'source_id':'资料1',
            'platform_item_id':'7999999000000000011','chunk_id':'synthetic:1','title':'虚构视频',
            'text':'[00:01-00:10] 视频介绍如何整理文件。','time_range':'00:01–00:10',
            'url':'https://www.douyin.com/video/7999999000000000011'}]}


class AllModel:
    def __init__(self):self.calls=0
    def complete(self,messages,tools):
        self.calls+=1
        names={t['function']['name'] for t in tools}
        assert 'save_current_video' not in names and 'get_import_status' not in names
        name,args=(('query_knowledge_base',{'question':'这个视频讲了什么事情'}) if self.calls==1
            else ('propose_reply',{'text':'视频介绍如何整理文件。','source_ids':['资料1']}))
        return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'answer-'+str(self.calls),
            'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]},
            {'prompt_tokens':4,'completion_tokens':3,'total_tokens':7},'tool_calls')


def prepared(tmp_path):
    binding,agent,env,registry,migrated,queue,history,control=system(tmp_path)
    env['native_comment_id']='7999999000000000091';env['reference_quality']='native'
    root=receive(queue,history,registry,env)
    backend=Backend();import_model=Model([('save_current_video',{'knowledge_base_id':7})])
    adapter=FollowupAdapter()
    bridge=LegacyBridge(registry,queue,control,agent_path=tmp_path/'agent.sqlite3',conversation=history,
        database=tmp_path/'never-open-business.db',import_client=backend,model_factory=lambda:import_model,
        knowledge_factory=lambda _:Knowledge(),adapter=adapter)
    runtime=AssistantRuntime(queue,control,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    assert runtime.execute_once()['state']=='ready_to_publish'
    published=runtime.publish_once()
    assert published['state']=='b_view_visible' and backend.posts==1
    assert agent.get_reply(published['agent_task_id'])['kind']=='import_status'
    assert history.roots(binding,env['video_id'])==[]
    anchor=bridge._followup_root_anchor(root['id'])
    assert anchor is not None
    return binding,agent,env,registry,migrated,queue,history,control,bridge,anchor,backend,adapter


def child_detail(anchor, *, text='这个视频讲了什么事情'):
    root=anchor['root_envelope'];native='7999999000000000092'
    proof={'source_kind':'conversation_followup','evidence_version':1,'query_only':True,
        'instance_id':anchor['instance_id'],'real_mention':False,'sender_verified':True,
        'receiver_verified':True,'video_verified':True,'current_receiver_verified':True,'unique_count':1,
        'root_chain_verified':True,'prior_reply_visible_unique':True,'reply_to_receiver_verified':True,
        'direct_reply_to_prior':False,'parent_scope':'unique_followup_in_verified_root',
        'sender_profile_url':root['sender_profile_url'],'receiver_profile_url':root['receiver_profile_url'],
        'video_id':root['video_id'],'root_task_id':anchor['root_task_id'],
        'root_agent_task_id':anchor['root_agent_task_id'],'root_reference_id':root['reference_id'],
        'root_comment_text':root['comment_text'],'root_native_comment_id':root['native_comment_id'],
        'prior_reply_native_comment_id':None,'prior_reply_text':anchor['prior_reply_text'],
        'prior_reply_sha256':anchor['prior_reply_sha256'],'followup_reference_id':'filled-by-maker',
        'followup_text':text,'native_comment_id':native,'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    return {'ok':True,'sender_profile_url':root['sender_profile_url'],
        'receiver_profile_url':root['receiver_profile_url'],'video_id':root['video_id'],
        'native_comment_id':native,'command_text':text,'evidence':proof}


def planned_child(tmp_path, prepared_values, *, action='query'):
    b,a,e,r,m,q,h,c,bridge,anchor,backend,adapter=prepared_values
    r.set_local_owner_access(m['user_id'],m['device_id'],query_all_local=True,
        public_reply_allowed=True,verified_by='synthetic-test-owner')
    h.prepare_scope_v2()
    all_binding=r.authorize_all_local(m['user_id'],m['device_id'])
    env=make_followup_envelope(child_detail(anchor),all_binding)
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    planner=Planner(action, m['grant_id'] if action=='save' else None)
    interpreter=SemanticIntentInterpreter(r,semantic,planner,budget)
    resolution=interpreter(env,source_checkpoint=lambda:True)
    assert resolution.binding is not None
    # The root save authorization never becomes the child's binding.
    assert resolution.binding.is_all_local if action=='query' else resolution.action=='clarify'
    # make_followup_envelope binds command identity to the actual fresh choice.
    env=make_followup_envelope(child_detail(anchor),resolution.binding)
    task=q.enqueue(resolution.binding,env,kind=resolution.action,user_id=resolution.user_id,
        device_id=resolution.device_id,authorization=resolution.authorization)
    h.bind_task(task['id'],resolution.binding,env,e['reference_id'],
        {'quality':'verified_ancestor','root_reference_id':e['reference_id']})
    return env,task,budget,semantic,planner,resolution


def test_published_import_status_is_readonly_valid_anchor_without_answer_history(tmp_path):
    b,a,e,r,m,q,h,c,bridge,anchor,backend,adapter=prepared(tmp_path)
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in (q.path,a.path)}
    candidate={'source_kind':'native_reply_notice_candidate','sender_profile_url':b.sender_profile_url,
        'receiver_profile_url':b.receiver_profile_url,'video_id':e['video_id'],
        'parent_id_hint':e['native_comment_id'],'recipient_uid':'99999999'}
    assert bridge.followup_anchors(candidate)==[anchor]
    assert bridge.followup_anchor_current(anchor['root_task_id'],anchor['prior_reply_sha256'])
    for key,bad in [('parent_id_hint','7999999000000000098'),('video_id','7999999000000000099'),
                    ('sender_profile_url','https://www.douyin.com/user/foreign-owner')]:
        assert bridge.followup_anchors({**candidate,key:bad})==[]
    assert before=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    r.revoke_grant(m['grant_id'])
    assert bridge.followup_anchors(candidate)==[]
    assert not bridge.followup_anchor_current(anchor['root_task_id'],anchor['prior_reply_sha256'])


def test_plain_child_gets_one_independent_meaning_then_all_local_query_and_one_send(tmp_path,monkeypatch):
    values=prepared(tmp_path)
    b,a,e,r,m,q,h,c,old,anchor,backend,adapter=values
    env,task,budget,semantic,planner,resolution=planned_child(tmp_path,values)
    model=AllModel();knowledge=AllKnowledge()
    construction=[]
    def backend_tools(database,kbid,task_id,**kwargs):
        construction.append((kbid,task_id,kwargs));return knowledge
    monkeypatch.setattr('app.assistant.legacy_bridge.BackendKnowledgeTools',backend_tools)
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'never-open-business.db',
        import_client=backend,model_factory=lambda:model,
        adapter=adapter,semantic_store=semantic,usage_budget=budget,command_resolver=CommandResolver(r))
    assert bridge._semantic_owner_scope(task)['root_reference_id']==env['reference_id']
    assert env['evidence']['real_mention'] is False and resolution.binding.knowledge_base_id is None
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish)
    ready=runtime.execute_once()
    assert ready['state']=='ready_to_publish',ready
    assert construction[0][0] is None and construction[0][2]['query_scope']=='all_local'
    assert construction[0][2]['focus_video_id']==e['video_id']
    assert model.calls==2 and planner.calls==1 and backend.posts==1
    assert ready['usage']['total_tokens']==34 and ready['usage']['model_responses']==3
    reply=a.get_reply(ready['agent_task_id'])
    assert reply['target']['followup_evidence']==env['evidence']
    assert reply['target']['knowledge_base_id'] is None and reply['target']['query_scope']=='all_local'
    assert runtime.publish_once()['state']=='b_view_visible'
    assert adapter.submits==2 and runtime.publish_once() is None
    assert runtime.execute_once() is None and model.calls==2 and planner.calls==1
    assert budget.summary()['all_time']['actual_tokens']==34


def test_missing_recorded_meaning_never_falls_back_to_raw_regex(tmp_path):
    values=prepared(tmp_path);b,a,e,r,m,q,h,c,old,anchor,backend,adapter=values
    env,task,budget,semantic,planner,resolution=planned_child(tmp_path,values)
    empty=SemanticPlanStore.prepare(tmp_path/'empty-semantic.sqlite3')
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'never.db',semantic_store=empty,
        model_factory=lambda:pytest.fail('missing semantic authorization must not call model'))
    assert not bridge._semantic_permission_current(task)
    assert bridge._resolution(task) is None
    assert AssistantRuntime(q,c,execute=bridge.execute).execute_once()['state']=='needs_review'
    assert len(a.list_tasks())==1  # only original save


def test_semantic_save_from_plain_child_is_clarification_no_import(tmp_path):
    values=prepared(tmp_path);b,a,e,r,m,q,h,c,old,anchor,backend,adapter=values
    env,task,budget,semantic,planner,resolution=planned_child(tmp_path,values,action='save')
    assert resolution.action=='clarify' and resolution.reason_code=='followup_save_requires_mention'
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'never.db',semantic_store=semantic,
        usage_budget=budget,adapter=adapter,model_factory=lambda:pytest.fail('fixed clarification must not call model'))
    done=AssistantRuntime(q,c,execute=bridge.execute).execute_once()
    assert done['state']=='ready_to_publish' and backend.posts==1 and planner.calls==1
    assert a.get_reply(done['agent_task_id'])['kind']=='clarification'
    assert AgentService(a,resolution.binding,allow_import=True,knowledge=Knowledge(),model=AllModel()).process(env)['reason_code']=='followup_query_only'
    assert AgentEngine(AllModel(),AllKnowledge()).run(resolution.binding,env).review_code=='semantic_decision_unverified'


@pytest.mark.parametrize('field,bad',[('real_mention',True),('query_only',False),
    ('root_task_id','f'*32),('prior_reply_sha256','0'*64),('root_reference_id','different-root'),
    ('root_comment_text','changed root text'),('followup_text','changed current text')])
def test_fresh_publish_proof_cannot_change_frozen_followup_parent(tmp_path,field,bad):
    from tests.test_conversation_followup_evidence import binding,detail
    all_binding=binding();a=AgentStore(tmp_path/'agent.sqlite3');adapter=FollowupAdapter()
    env=make_followup_envelope(detail(),all_binding)
    a.put_binding(all_binding.to_dict());task=a.enqueue(all_binding.binding_id,env);claim=a.claim(task['task_id'])
    a.complete(task['task_id'],claim['claim_token'],{'kind':'answer','text':'合成答案。','sources':[],'source_ids':[]})
    target=a.get_reply(task['task_id'])['target'];proof=adapter.prepare_reply(target,'合成答案。')
    assert _verified_target(target,proof,all_binding.to_dict())
    assert not _verified_target(target,{**proof,field:bad},all_binding.to_dict())
    changed=deepcopy(env);changed['evidence'][field]=bad
    assert _canonical_reference(changed)!=_canonical_reference(env)
    assert a.get_reply(task['task_id'])['send_token'] is None


def test_store_rejects_import_proposal_and_ingestion_for_followup(tmp_path):
    values=prepared(tmp_path);b,a,e,r,m,q,h,c,old,anchor,backend,adapter=values
    env=make_followup_envelope(child_detail(anchor),b)
    task=a.enqueue(b.binding_id,env);claim=a.claim(task['task_id'])
    with pytest.raises(AgentStoreError,match='followup_query_only'):
        a.complete(task['task_id'],claim['claim_token'],{'kind':'import_status','text':'不应保存',
            'source_ids':[],'sources':[],'import_job_id':'synthetic-refused-job','import_state':'succeeded'})
    with a._tx() as db:
        with pytest.raises(AgentStoreError,match='followup_query_only'):
            a._ingestion_claim(db,task['task_id'],claim['claim_token'])
    assert backend.posts==1 and a.get_reply(task['task_id']) is None


def test_anchor_revoked_after_provider_blocks_tools_and_publication(tmp_path):
    values=prepared(tmp_path);b,a,e,r,m,q,h,c,old,anchor,backend,adapter=values
    env,task,budget,semantic,planner,resolution=planned_child(tmp_path,values)
    knowledge=AllKnowledge()
    class RevokingModel(AllModel):
        def complete(self,*args):
            turn=super().complete(*args)
            r.revoke_grant(m['grant_id'])  # only original save root, not child's all-local grant
            return turn
    model=RevokingModel()
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'never.db',
        semantic_store=semantic,usage_budget=budget,model_factory=lambda:model,
        knowledge_factory=lambda _:knowledge,adapter=adapter)
    result=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish).execute_once()
    assert result['state']=='needs_review' and model.calls==1
    assert knowledge.questions==[] and backend.posts==1
    assert not bridge.followup_anchor_current(anchor['root_task_id'],anchor['prior_reply_sha256'])
    assert a.get_reply(result['agent_task_id']) is None and adapter.submits==1


def test_long_valid_parent_proof_retains_once_send_and_binding_identity(tmp_path):
    from tests.test_conversation_followup_evidence import binding,detail
    b=binding();a=AgentStore(tmp_path/'agent.sqlite3');a.put_binding(b.to_dict())
    d=detail();d['evidence']['prior_reply_text']='合成的已发表回答。'*800
    d['evidence']['prior_reply_sha256']=hashlib.sha256(d['evidence']['prior_reply_text'].encode()).hexdigest()
    env=make_followup_envelope(d,b)
    wrong=deepcopy(env);wrong['command_id']='wrong-bound-command'
    with pytest.raises(AgentStoreError,match='invalid_followup_binding'):
        a.enqueue(b.binding_id,wrong)
    assert a.list_tasks()==[]
    task=a.enqueue(b.binding_id,env);claim=a.claim(task['task_id'])
    a.complete(task['task_id'],claim['claim_token'],{'kind':'answer','text':'合成的当前新答复。','sources':[],'source_ids':[]})
    adapter=FollowupAdapter();target=a.get_reply(task['task_id'])['target']
    verification=adapter.prepare_reply(target,'合成的当前新答复。')
    assert len(json.dumps(verification,ensure_ascii=False))>8192
    reserved=a.reserve_send(task['task_id'],verification)
    assert reserved is not None and a.reserve_send(task['task_id'],verification) is None
