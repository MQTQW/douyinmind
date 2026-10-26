"""Shared command budgets and exact request attribution; temporary fake models."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.assistant.legacy_bridge import LegacyBridge, merge_recorded_model_steps, recorded_usage
from app.assistant.usage_budget import UsageBudget
from app.assistant.usage_model import BudgetedModel, ModelBudgetError, sync_existing_usage
from app.mention_agent.model import ModelTurn
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore
from tests.test_mention_agent_service import binding, envelope, Model, Knowledge, answer_turns
from tests.test_assistant_product_bridge import system, receive
from tests.test_assistant_legacy_extensions import QueryKnowledge, QueryModel


SCOPE = 'semantic-command:' + 'a'*64


def planning(budget, *, scope=SCOPE, total=53):
    class Provider:
        def complete(self, messages, tools):
            return SimpleNamespace(usage={'prompt_tokens':total-3,'completion_tokens':3,'total_tokens':total})
    model = BudgetedModel(Provider(), budget, scope)
    model.preflight([], []); turn = model.complete([], [])
    return SimpleNamespace(budget_scope_id=scope, request_key=model.request_key, usage=turn.usage)


def test_bare_scope_or_bare_linker_cannot_override_legacy_budget_identity(tmp_path):
    store = AgentStore(tmp_path/'agent.db')
    for options in ({'budget_scope_id':SCOPE}, {'budget_scope_linker':lambda *_:True}):
        with pytest.raises(ValueError, match='trusted_budget_scope_required'):
            AgentService(store, binding(), knowledge=Knowledge(), model=Model([]), **options)


def test_provider_denied_when_trusted_link_fails_before_reservation(tmp_path):
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');store=AgentStore(tmp_path/'agent.db');model=Model(answer_turns())
    result=AgentService(store,binding(),knowledge=Knowledge(),model=model,usage_budget=budget,
        budget_scope_id=SCOPE,budget_scope_linker=lambda *_:False).process(envelope())
    assert result['state']=='needs_review' and model.requests==[]
    assert budget.summary()['all_time']['request_count']==0


def test_planning_and_final_agent_share_per_command_limit(tmp_path):
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');planning(budget)
    budget.set_limits(per_task_tokens=54)
    store=AgentStore(tmp_path/'agent.db');model=Model(answer_turns());calls=[]
    def link(task_id, actual, current, scope):
        calls.append((task_id, actual, current, scope));return True
    result=AgentService(store,binding(),knowledge=Knowledge(),model=model,usage_budget=budget,
        budget_scope_id=SCOPE,budget_scope_linker=link).process(envelope())
    assert result['reason_code']=='model_budget_exceeded' and model.requests==[]
    assert len(calls)==1 and calls[0][1]==envelope(**{'evidence':calls[0][1]['evidence']})
    assert budget.summary(SCOPE)['task']['actual_tokens']==53
    assert budget.summary()['all_time']['request_count']==1


def test_shared_scope_sync_requires_durable_mapping_and_does_not_charge_twice(tmp_path):
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');planning(budget)
    store=AgentStore(tmp_path/'agent.db');mapping={}
    def link(task_id, actual, current, scope):mapping[task_id]=(deepcopy(actual),current,scope);return True
    result=AgentService(store,binding(),knowledge=Knowledge(),model=Model(answer_turns()),usage_budget=budget,
        budget_scope_id=SCOPE,budget_scope_linker=link).process(envelope())
    assert result['state']=='answer_ready'
    assert budget.summary()['all_time']['request_count']==3
    assert budget.summary()['all_time']['actual_tokens']==67
    with pytest.raises(ModelBudgetError):sync_existing_usage(budget,store)
    def trusted(task_id,*,envelope,binding,for_accounting=False):
        actual,current,scope=mapping[task_id]
        if envelope!=actual or binding!=current:raise ValueError('wrong scope')
        return scope
    sync_existing_usage(budget,store,trusted_scope_for_task=trusted)
    sync_existing_usage(budget,store,trusted_scope_for_task=trusted)
    assert budget.summary()['all_time']['actual_tokens']==67
    assert budget.summary()['all_time']['request_count']==3
    with pytest.raises(ModelBudgetError):
        sync_existing_usage(budget,store,trusted_scope_for_task=lambda *a,**k:'different')


def test_unknown_planning_usage_keeps_shared_budget_closed(tmp_path):
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    budget.reserve(SCOPE,'unknown-planning-request',100)
    budget.mark_unknown('unknown-planning-request',reason='usage_missing')
    budget.set_limits(per_task_tokens=100)
    model=Model(answer_turns());store=AgentStore(tmp_path/'agent.db')
    result=AgentService(store,binding(),knowledge=Knowledge(),model=model,usage_budget=budget,
        budget_scope_id=SCOPE,budget_scope_linker=lambda *_:True).process(envelope())
    assert result['reason_code']=='model_budget_exceeded' and model.requests==[]
    assert budget.summary()['all_time']['unsettled_requests']==1
    assert budget.summary()['all_time']['held_estimated_tokens']==100


def test_merge_deduplicates_exact_request_not_entire_shared_scope():
    def response(key,total):return {'kind':'model_response','metadata':{'budget_request_id':key,'usage':{'total_tokens':total}}}
    own=response('own',5);other=response('other',8)
    merged=merge_recorded_model_steps([own],[deepcopy(own),other])
    assert recorded_usage(merged)=={'model_responses':2,'total_tokens':13}
    conflicted=merge_recorded_model_steps([own],[response('own',6)])
    assert recorded_usage(conflicted)=={'model_responses':1}


class SemanticFixture:
    """Internal trusted-store protocol fake, not a production trust proof."""
    def __init__(self, expected_env, expected_binding, receipt):
        self.env,self.binding,self.receipt=deepcopy(expected_env),expected_binding,receipt
        self.mapping={}
    def get_recorded_decision(self, envelope, *, binding=None, queue_task_id=None):
        return self.receipt if envelope==self.env and binding==self.binding else None
    def bind_agent_budget(self, receipt, agent_task_id, envelope, binding):
        assert receipt is self.receipt and envelope==self.env and binding==self.binding
        self.mapping[agent_task_id]=(deepcopy(envelope),binding,receipt.budget_scope_id)
    def agent_budget_scope(self, agent_task_id, *, envelope=None,binding=None,for_accounting=False):
        if agent_task_id not in self.mapping:return None
        env,current,scope=self.mapping[agent_task_id]
        if env!=envelope or current!=binding:raise ValueError('mapping mismatch')
        return scope


class ResolverFixture:
    def __init__(self, registry, receipt):
        from app.assistant.commands import CommandResolver
        self.original=CommandResolver(registry);self.receipt=receipt;self.verified_calls=0
    def resolve(self,*args,**kwargs):return self.original.resolve(*args,**kwargs)
    def resolve_verified_intent(self,env,receipt,**kwargs):
        assert (receipt.plan_id==self.receipt.plan_id if hasattr(receipt,'plan_id') else receipt is self.receipt)
        self.verified_calls+=1
        return self.original.resolve(env,**kwargs)


class MeasuredQueryModel(QueryModel):
    def complete(self,*args):
        turn=super().complete(*args)
        return ModelTurn(turn.message,{'prompt_tokens':4,'completion_tokens':3,'total_tokens':7},turn.finish_reason)


def test_bridge_projects_only_current_comment_planning_and_agent_usage(tmp_path):
    from app.assistant.runtime import AssistantRuntime
    from app.assistant.semantic_plan_store import SemanticPlanStore
    from app.mention_agent.contracts import Binding
    b,a,e,r,m,q,h,c=system(tmp_path);e['comment_text']='@助手 请根据编程知识库说明Git。'
    task=receive(q,h,r,e);budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    receipt=real_semantic_receipt(semantic,budget,task['envelope'],Binding.from_dict(task['binding']),
        library_name='测试库',grant_id=m['grant_id'],owner_extra={'user_id':task['user_id'],'device_id':task['device_id']},
        decision={'action':'query','target_state':'one','grant_id':m['grant_id']})
    planning(budget,scope=receipt.budget_scope_id,total=101)  # Another comment in same bucket: exclude here.
    resolver=ResolverFixture(r,receipt);model=MeasuredQueryModel()
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),usage_budget=budget,
        semantic_store=semantic,command_resolver=resolver)
    ready=AssistantRuntime(q,c,execute=bridge.execute).execute_once()
    assert ready['state']=='ready_to_publish'
    assert ready['usage']['model_responses']==3 and ready['usage']['total_tokens']==34
    assert budget.summary()['all_time']['actual_tokens']==135
    visible=[s for s in ready['details']['steps'] if s['metadata'].get('kind')=='semantic_planning']
    assert len(visible)==1 and visible[0]['metadata']['name']=='理解指令'
    assert visible[0]['metadata']['usage']['total_tokens']==20
    assert resolver.verified_calls>0
    sync_existing_usage(budget,bridge.agent,trusted_scope_for_task=semantic.agent_budget_scope)
    assert budget.summary()['all_time']['actual_tokens']==135


def test_no_semantic_receipt_keeps_legacy_task_budget_and_no_planning_step(tmp_path):
    from app.assistant.runtime import AssistantRuntime
    b,a,e,r,m,q,h,c=system(tmp_path);e['comment_text']='@助手 请根据编程知识库说明Git。'
    receive(q,h,r,e);budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    model=MeasuredQueryModel()
    bridge=LegacyBridge(r,q,c,agent_path=tmp_path/'agent.sqlite3',conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),usage_budget=budget)
    result=AssistantRuntime(q,c,execute=bridge.execute).execute_once()
    assert result['usage']['total_tokens']==14
    assert budget.summary(result['agent_task_id'])['task']['actual_tokens']==14
    assert not any(s['metadata'].get('kind')=='semantic_planning' for s in result['details']['steps'])
    sync_existing_usage(budget,bridge.agent)


def test_unknown_usage_not_rendered_as_zero_in_planning_step():
    step={'kind':'model_response','metadata':{'phase':'semantic_planning','name':'理解指令','status':'completed','usage':{}},'created_at':None}
    details=LegacyBridge._details(None,[step],'')
    assert details['steps'][0]['metadata']=={'kind':'semantic_planning','name':'理解指令','status':'completed'}
    assert recorded_usage([step])=={'model_responses':1}


def test_planning_step_survives_bounded_public_history():
    planning_step={'kind':'model_response','metadata':{'phase':'semantic_planning','status':'completed','usage':{'total_tokens':3}},'created_at':1}
    other=[{'kind':'tool_result','metadata':{'count':i},'created_at':i} for i in range(50)]
    details=LegacyBridge._details(None,[planning_step]+other,'')
    assert len(details['steps'])==32 and details['steps'][0]['metadata']['kind']=='semantic_planning'


def real_semantic_receipt(store, budget, env, current, *, source='native:synthetic-budget-source',
                          library_name='验收库',grant_id='synthetic-budget-grant',owner_extra=None,decision=None):
    from app.assistant.semantic_plan_store import ui_semantic_digest
    owner={'user_id':'synthetic-budget-user','device_id':'synthetic-budget-device','instance_id':current.instance_id,
        'sender_profile_url':current.sender_profile_url,'receiver_profile_url':current.receiver_profile_url,
        'video_id':env['video_id'],'root_reference_id':env['reference_id'],'reference_id':env['reference_id']}
    if owner_extra:owner.update(owner_extra)
    catalog={'revision':'synthetic-budget-catalog','options':[{'grant_id':grant_id,'name':library_name,
        'aliases':[],'can_query':True,'can_import':True,'ordinal':1}]}
    class Planner:
        def complete(self,messages,tools):
            import json
            assert messages[-1]['content']==env['comment_text']
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'plan-one','type':'function',
                'function':{'name':'submit_intent','arguments':json.dumps(decision or {'action':'query','target_state':'unspecified','grant_id':None})}}]},
                {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')
    return store.run_once(source_key=source,frozen_ui_digest=ui_semantic_digest(env),owner_scope=owner,
        catalog_snapshot=catalog,mode='root',original_text=env['comment_text'],model=Planner(),budget=budget,
        scope_authorizer=lambda:True,should_stop=lambda:False)


def test_actual_semantic_owned_mapping_and_agent_survive_restart_without_recharge(tmp_path):
    from app.assistant.semantic_plan_store import SemanticPlanStore
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    current=binding();env=envelope();receipt=real_semantic_receipt(semantic,budget,env,current)
    agent=AgentStore(tmp_path/'agent.db')
    def link(task_id,actual,actual_binding,scope):
        assert scope==receipt.budget_scope_id
        return semantic.bind_agent_budget(receipt,task_id,actual,actual_binding)
    result=AgentService(agent,current,knowledge=Knowledge(),model=Model(answer_turns()),usage_budget=budget,
        budget_scope_id=receipt.budget_scope_id,budget_scope_linker=link).process(env)
    assert result['state']=='answer_ready'
    assert budget.summary(receipt.budget_scope_id)['task']['actual_tokens']==34
    reopened=SemanticPlanStore(semantic.path)
    sync_existing_usage(budget,agent,trusted_scope_for_task=reopened.agent_budget_scope)
    sync_existing_usage(budget,agent,trusted_scope_for_task=reopened.agent_budget_scope)
    assert budget.summary()['all_time']['actual_tokens']==34
    assert budget.summary()['all_time']['request_count']==3
    changed=deepcopy(agent.get_task(result['task_id'])['envelope']);changed['video_id']='7999999999999999999'
    with pytest.raises(Exception):
        reopened.agent_budget_scope(result['task_id'],envelope=changed,binding=current)


def test_http_like_envelope_budget_hint_never_selects_scope(tmp_path):
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');agent=AgentStore(tmp_path/'agent.db')
    env=envelope();env['evidence']['budget_scope_id']='attacker-requested-free-bucket'
    result=AgentService(agent,binding(),knowledge=Knowledge(),model=Model(answer_turns()),usage_budget=budget).process(env)
    assert result['state']=='answer_ready'
    assert budget.summary(result['task_id'])['task']['actual_tokens']==14
    assert budget.summary('attacker-requested-free-bucket')['task']['request_count']==0


def test_actual_deactivation_keeps_historical_cost_without_new_execution(tmp_path):
    from app.assistant.semantic_plan_store import SemanticPlanStore
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    current=binding();env=envelope();receipt=real_semantic_receipt(semantic,budget,env,current)
    agent=AgentStore(tmp_path/'agent.db');model=Model(answer_turns())
    service=AgentService(agent,current,knowledge=Knowledge(),model=model,usage_budget=budget,
        budget_scope_id=receipt.budget_scope_id,budget_scope_linker=lambda tid,actual,scope,_:semantic.bind_agent_budget(receipt,tid,actual,scope))
    result=service.process(env);assert result['state']=='answer_ready'
    agent.deactivate(current.binding_id)
    inactive=agent.get_binding(current.binding_id)
    with pytest.raises(Exception):semantic.agent_budget_scope(result['task_id'],envelope=env,binding=inactive)
    sync_existing_usage(budget,agent,trusted_scope_for_task=semantic.agent_budget_scope)
    assert budget.summary()['all_time']['actual_tokens']==34 and budget.summary()['all_time']['request_count']==3
    calls=len(model.requests)
    again=service.process(env)
    assert again['reason_code']=='binding_changed' and len(model.requests)==calls
