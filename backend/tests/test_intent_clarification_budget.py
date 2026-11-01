"""Prompt context participates in pre-request budget admission; all providers fake."""
from copy import deepcopy
import json

import pytest

from app.assistant.semantic_intent_model import ClarificationIntentModel,intent_messages,intent_tools
from app.assistant.semantic_plan_store import SemanticPlanStore,SemanticPlanError
from app.assistant.usage_budget import UsageBudgetExceeded
from app.mention_agent.contracts import canonical_json
from app.mention_agent.model import ModelTurn


OWNER={'user_id':'synthetic-budget-user','device_id':'synthetic-budget-device','instance_id':'synthetic-local',
    'sender_profile_url':'https://www.douyin.com/user/intent-budget-A',
    'receiver_profile_url':'https://www.douyin.com/user/intent-budget-B','video_id':'7999999000000000011',
    'reference_id':'synthetic:clarification-reply','root_reference_id':'synthetic:clarification-reply'}
CATALOG={'revision':'catalog-synthetic','options':[]}
TEXT='请先保存，不需要提问。'
UI='a'*64


class Provider:
    def __init__(self):self.calls=[]
    def complete(self,messages,tools):
        self.calls.append((deepcopy(messages),deepcopy(tools)))
        return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'synthetic-meaning',
            'type':'function','function':{'name':'submit_intent','arguments':json.dumps({
                'action':'clarify','target_state':'unspecified','grant_id':None})}}]},
            {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')


class Budget:
    def __init__(self,cap=None):self.cap=cap;self.reservations=[];self.settlements=[]
    def reserve(self,scope,key,estimate):
        self.reservations.append((scope,key,estimate))
        if self.cap is not None and estimate>self.cap:raise UsageBudgetExceeded('model_budget_exceeded')
        return {'may_request':True}
    def settle(self,key,usage):
        self.settlements.append(key)
        return {'state':'settled','input_tokens':12,'output_tokens':8,'total_tokens':20}
    def unknown(self,*args,**kwargs):raise AssertionError('known budget rejection is not an unknown provider request')


def wrapper(provider,ui=UI):
    session={'state':'responding','root':{'allowed_actions':['save','clarify','cancel'],
        'envelope':{'comment_text':'合成原请求背景。'*300}},
        'published_reply':{'text':'请明确是否保存这条合成视频。'}}
    return ClarificationIntentModel(provider,session,ui)


def run(store,model,budget):
    return store.run_once(source_key='synthetic-clarification-budget',frozen_ui_digest=UI,
        owner_scope=OWNER,catalog_snapshot=CATALOG,mode='root',original_text=TEXT,model=model,budget=budget,
        scope_authorizer=lambda:True,should_stop=lambda:False)


def test_full_context_is_prepared_before_budget_and_delegate_receives_exact_prepared_input(tmp_path):
    store=SemanticPlanStore.prepare(tmp_path/'meaning.sqlite3');provider=Provider();model=wrapper(provider);budget=Budget()
    receipt=run(store,model,budget)
    assert len(provider.calls)==len(budget.reservations)==len(budget.settlements)==1
    messages,tools=provider.calls[0]
    assert '合成原请求背景。'*300 in messages[0]['content'] and messages[-1]['content']==TEXT
    assert tools[0]['function']['parameters']['properties']['action']['enum']==['save','clarify','cancel']
    assert budget.reservations[0][2]>=len(canonical_json({'messages':messages,'tools':tools}).encode())
    assert run(store,model,budget).plan_id==receipt.plan_id
    assert len(provider.calls)==len(budget.reservations)==1


def test_context_that_exceeds_original_plain_input_budget_is_rejected_before_model(tmp_path):
    plain=len(canonical_json({'messages':intent_messages('root',TEXT,CATALOG),'tools':intent_tools()}).encode())+3548
    provider=Provider();budget=Budget(cap=plain)
    with pytest.raises(SemanticPlanError):run(SemanticPlanStore.prepare(tmp_path/'meaning.sqlite3'),wrapper(provider),budget)
    assert provider.calls==[] and budget.settlements==[]
    assert len(budget.reservations)==1 and budget.reservations[0][2]>plain


def test_wrong_ui_wrapper_cannot_reuse_context_on_another_request(tmp_path):
    provider=Provider();budget=Budget()
    with pytest.raises(SemanticPlanError):run(SemanticPlanStore.prepare(tmp_path/'meaning.sqlite3'),wrapper(provider,'b'*64),budget)
    assert provider.calls==[] and budget.reservations==[]


def test_arbitrary_provider_prepare_hook_is_not_called_before_budget(tmp_path):
    class Ordinary(Provider):
        def prepare_input(self,*args):raise AssertionError('only exact trusted wrapper type can prepare context')
    provider=Ordinary();budget=Budget()
    run(SemanticPlanStore.prepare(tmp_path/'meaning.sqlite3'),provider,budget)
    assert len(provider.calls)==len(budget.reservations)==1
