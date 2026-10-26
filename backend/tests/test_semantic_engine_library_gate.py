"""Only persisted semantic proof replaces raw-name parsing; all tools stay bound."""
from copy import deepcopy

import pytest

from app.assistant.semantic_plan_store import SemanticPlanStore, decision_metadata
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.engine import AgentEngine
from app.mention_agent.imports import ImportTools
from app.mention_agent.model import ModelTurn
from app.mention_agent.service import AgentService
from tests.test_mention_agent_import_end_to_end import scope, Backend, Knowledge, Model
from tests.test_assistant_legacy_extensions import QueryKnowledge
from tests.test_semantic_budget_integration import real_semantic_receipt, MeasuredQueryModel


class MeasuredImportModel(Model):
    def complete(self,*args):
        turn=super().complete(*args)
        return ModelTurn(turn.message,{'prompt_tokens':4,'completion_tokens':3,'total_tokens':7},turn.finish_reason)


def setup(tmp_path,*,action='save',original=None):
    binding,agent,env=scope(tmp_path)
    env['comment_text']=original or ('@助手 把这条放进刚才那个知识库吧' if action=='save' else '@助手 用你上面说的那个知识库解释 Git')
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    grant='synthetic-selected-grant'
    receipt=real_semantic_receipt(semantic,budget,env,binding,library_name='测试库',grant_id=grant,
        decision={'action':action,'target_state':'one','grant_id':grant})
    owner=decision_metadata(receipt)['owner_scope']
    backend=Backend();model=MeasuredImportModel([('save_current_video',{'knowledge_base_id':7})]) if action=='save' else MeasuredQueryModel()
    def link(task_id,actual,current,scope_id):
        assert scope_id==receipt.budget_scope_id
        return semantic.bind_agent_budget(receipt,task_id,actual,current)
    options={'knowledge':Knowledge() if action=='save' else QueryKnowledge(),'model':model,'usage_budget':budget,
        'budget_scope_id':receipt.budget_scope_id,'budget_scope_linker':link,
        'trusted_semantic_decision':receipt,'trusted_semantic_grant_id':grant,'trusted_semantic_owner_scope':owner,
        'scope_authorizer':lambda current:current==binding}
    if action=='save':
        options.update(allow_import=True,automatic_import_receipt=True,import_wait_seconds=0,
            explicit_save_check=lambda actual,current:actual==env and current==binding,
            import_factory=lambda *a,**kw:ImportTools(*a,**kw,client=backend))
    return binding,agent,env,semantic,receipt,budget,model,backend,options


@pytest.mark.parametrize('action',['save','query'])
def test_real_persisted_proof_allows_same_bound_library_despite_old_text_regex(tmp_path,action):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path,action=action)
    with pytest.raises(ValueError,match='library_not_allowed'):
        AgentEngine._check_named_library(env['comment_text'],'测试库')
    result=AgentService(agent,binding,**options).process(env)
    assert result['state']=='answer_ready'
    assert backend.posts==(1 if action=='save' else 0)
    if action=='save':
        assert backend.body['knowledge_base_id']==7 and backend.body['video_id']==env['video_id']
        assert model.count==1 and result['reply']['kind']=='import_status'
    else:assert len(model.requests)==2
    assert agent.get_task(result['task_id'])['envelope']['comment_text']==env['comment_text']


@pytest.mark.parametrize('bad',['json','owner','grant','mapped_binding','ui','action','missing_mapping'])
def test_fake_or_mismatched_semantic_proof_blocks_before_provider(tmp_path,bad):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path)
    if bad=='json':options['trusted_semantic_decision']={'action':'save','grant_id':'synthetic-selected-grant','verified':True}
    elif bad=='owner':options['trusted_semantic_owner_scope']={**options['trusted_semantic_owner_scope'],'user_id':'another-user'}
    elif bad=='grant':options['trusted_semantic_grant_id']='another-grant'
    elif bad=='ui':env['comment_text']+=' different actual comment'
    elif bad=='action':
        # The private decision stays save, while a readonly execution cannot
        # turn that into a query merely by presenting the receipt.
        options.pop('allow_import');options.pop('import_factory');options.pop('automatic_import_receipt')
    elif bad=='missing_mapping':options['budget_scope_linker']=lambda *_:True
    else:
        def wrong_map(task_id,actual,current,scope_id):
            from dataclasses import replace
            return semantic.bind_agent_budget(receipt,task_id,actual,replace(current,knowledge_base_id=8))
        options['budget_scope_linker']=wrong_map
    result=AgentService(agent,binding,**options).process(env)
    assert result['state']=='needs_review' and model.count==0 and backend.posts==0
    assert budget.summary()['all_time']['request_count']==1


def test_without_receipt_preserves_old_name_guard(tmp_path):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path)
    for key in ('trusted_semantic_decision','trusted_semantic_grant_id','trusted_semantic_owner_scope'):
        options.pop(key)
    result=AgentService(agent,binding,**options).process(env)
    assert result['state']=='needs_review' and backend.posts==0
    assert model.count>=1


def test_semantic_receipt_cannot_override_fixed_tool_library(tmp_path):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path)
    options['model']=MeasuredImportModel([('save_current_video',{'knowledge_base_id':8})])
    result=AgentService(agent,binding,**options).process(env)
    assert result['state']=='needs_review' and backend.posts==0


def test_semantic_receipt_cannot_override_explicit_save_gate(tmp_path):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path)
    options['explicit_save_check']=lambda *_:False
    result=AgentService(agent,binding,**options).process(env)
    assert result['state']=='needs_review' and backend.posts==0


def test_registry_scope_revoked_after_model_still_blocks_tool(tmp_path):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path)
    active=[True]
    class RevokingModel(MeasuredImportModel):
        def complete(self,*args):
            turn=super().complete(*args);active[0]=False;return turn
    options['model']=RevokingModel([('save_current_video',{'knowledge_base_id':7})])
    options['scope_authorizer']=lambda current:current==binding and active[0]
    result=AgentService(agent,binding,**options).process(env)
    assert result['reason_code']=='binding_changed' and backend.posts==0


def test_null_target_cannot_map_unspecified_query_to_specific_library(tmp_path):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path,action='query')
    # A separate actual root plan with no selected library is not a receipt for
    # this library. Even an internal caller cannot treat NULL as a wildcard.
    other=SemanticPlanStore.prepare(tmp_path/'semantic-unspecified.sqlite3')
    actual=real_semantic_receipt(other,budget,env,binding,source='native:unspecified-plan',library_name='测试库')
    options.update(trusted_semantic_decision=actual,trusted_semantic_grant_id=None,
        trusted_semantic_owner_scope=decision_metadata(actual)['owner_scope'],budget_scope_id=actual.budget_scope_id,
        budget_scope_linker=lambda tid,actual_env,current,_:other.bind_agent_budget(actual,tid,actual_env,current))
    result=AgentService(agent,binding,**options).process(env)
    assert result['reason_code']=='semantic_decision_unverified' and model.requests==[] and backend.posts==0


def test_lost_semantic_mapping_after_provider_stops_before_tool_and_further_paid_round(tmp_path):
    binding,agent,env,semantic,receipt,budget,model,backend,options=setup(tmp_path)
    class LosingMappingModel(MeasuredImportModel):
        def complete(self,*args):
            turn=super().complete(*args)
            with semantic._connection(write=True) as db:db.execute('DELETE FROM semantic_agent_links')
            return turn
    losing=LosingMappingModel([('save_current_video',{'knowledge_base_id':7})])
    options['model']=losing
    result=AgentService(agent,binding,**options).process(env)
    assert result['state']=='needs_review' and backend.posts==0
    assert result['model_request_attempts']==1
    assert losing.count==1 and result['reason_code']=='binding_changed'


def test_real_registry_planner_resolver_bridge_import_natural_original_once(tmp_path):
    """All real local gates; fake planning/final providers and fake backend only."""
    import json
    from app.assistant.commands import CommandResolver
    from app.assistant.legacy_bridge import LegacyBridge
    from app.assistant.runtime import AssistantRuntime
    from app.assistant.semantic_routing import SemanticIntentInterpreter
    from tests.test_assistant_product_bridge import system
    binding,agent,env,registry,migrated,queue,history,control=system(tmp_path)
    original='@助手 根据你上面列的第一个知识库，顺手帮我把这个视频收进去吧'
    env['comment_text']=original
    assert CommandResolver(registry).resolve(env).action!='save'
    with pytest.raises(ValueError):AgentEngine._check_named_library(original,'测试库')
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    semantic=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    class Planner:
        calls=0
        def complete(self,messages,tools):
            self.calls+=1;assert messages[-1]['content']==original
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'semantic-natural-one','type':'function',
                'function':{'name':'submit_intent','arguments':json.dumps({'action':'save','target_state':'one',
                    'grant_id':migrated['grant_id']})}}]}, {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')
    planner=Planner();interpreter=SemanticIntentInterpreter(registry,semantic,planner,budget)
    assert sum(queue.counts().values())==0 and agent.list_tasks()==[]
    resolution=interpreter(env,source_checkpoint=lambda:True)
    assert resolution.action=='save' and resolution.binding.knowledge_base_id==7
    assert sum(queue.counts().values())==0  # No frozen query exists to rewrite.
    task=queue.enqueue(resolution.binding,env,kind=resolution.action,user_id=resolution.user_id,
        device_id=resolution.device_id,authorization=resolution.authorization)
    history.bind_task(task['id'],resolution.binding,env,env['reference_id'],
        {'quality':'current_comment','root_reference_id':env['reference_id']})
    backend=Backend();model=MeasuredImportModel([('save_current_video',{'knowledge_base_id':7})])
    bridge=LegacyBridge(registry,queue,control,agent_path=tmp_path/'agent.sqlite3',conversation=history,
        database=tmp_path/'unused-business.db',import_client=backend,model_factory=lambda:model,
        knowledge_factory=lambda _:Knowledge(),usage_budget=budget,semantic_store=semantic,
        command_resolver=CommandResolver(registry))
    runtime=AssistantRuntime(queue,control,execute=bridge.execute,reconcile=bridge.reconcile)
    done=runtime.execute_once()
    assert done['state']=='ready_to_publish'
    assert done['kind']=='save' and done['envelope']['comment_text']==original
    assert planner.calls==model.count==backend.posts==1
    assert done['usage']['total_tokens']==27 and done['usage']['model_responses']==2
    assert budget.summary()['all_time']['actual_tokens']==27 and budget.summary()['all_time']['request_count']==2
    assert runtime.execute_once() is None
    # Re-reading the source meaning and reconciling never calls either model.
    assert interpreter(env,source_checkpoint=lambda:True).action=='save'
    bridge.reconcile(done)
    assert planner.calls==model.count==backend.posts==1
