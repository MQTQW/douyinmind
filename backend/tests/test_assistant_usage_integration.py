from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import threading
from types import SimpleNamespace

import pytest

from app.assistant.usage_budget import UsageBudget
from app.assistant.usage_model import BudgetedModel, ModelBudgetError, sync_existing_usage
from app.mention_agent.service import AgentService
from app.mention_agent.store import AgentStore
from tests.test_mention_agent_service import binding, envelope, Model, Knowledge, answer_turns


def prepared(tmp_path): return UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')


def test_zero_cap_denies_before_provider_step_or_knowledge(tmp_path):
    budget=prepared(tmp_path); budget.set_limits(per_task_tokens=0)
    store=AgentStore(tmp_path/'agent.db'); model=Model(answer_turns()); knowledge=Knowledge()
    result=AgentService(store,binding(),knowledge=knowledge,model=model,usage_budget=budget).process(envelope())
    assert result['state']=='needs_review' and result['reason_code']=='model_budget_exceeded'
    assert model.requests==knowledge.queries==[]
    assert not any(step['kind']=='model_request' for step in store.list_steps(result['task_id']))
    assert budget.summary()['all_time']['request_count']==0


def test_real_reported_usage_and_old_failure_remain_after_history_sync(tmp_path):
    budget=prepared(tmp_path); store=AgentStore(tmp_path/'agent.db')
    old=AgentService(store,binding(),knowledge=Knowledge(fail=True),model=Model(answer_turns())).process(envelope())
    assert old['state']=='needs_review'
    sync_existing_usage(budget,store)
    assert budget.summary()['all_time']['actual_tokens']==7
    new_env=envelope(command_id='new',reference_id='new-reference')
    result=AgentService(store,binding(),knowledge=Knowledge(),model=Model(answer_turns()),usage_budget=budget).process(new_env)
    assert result['state']=='answer_ready'
    assert budget.summary()['all_time']['actual_tokens']==21
    assert budget.summary()['all_time']['request_count']==3
    sync_existing_usage(budget,store); sync_existing_usage(budget,store)
    assert budget.summary()['all_time']['actual_tokens']==21
    assert budget.summary()['all_time']['request_count']==3


def test_provider_exception_consumes_reservation_once(tmp_path):
    class Provider:
        calls=0
        def complete(self,*args): self.calls+=1; raise TimeoutError('secret-url')
    provider=Provider(); budget=prepared(tmp_path); model=BudgetedModel(provider,budget,'task')
    model.preflight([],[])
    with pytest.raises(TimeoutError): model.complete([],[])
    with pytest.raises(ModelBudgetError): model.complete([],[])
    with pytest.raises(ModelBudgetError): model.preflight([],[])
    assert provider.calls==1
    summary=budget.summary()['all_time']
    assert summary['request_count']==summary['unsettled_requests']==1
    assert summary['held_estimated_tokens']>0


def test_two_complete_callers_cannot_share_one_admission(tmp_path):
    entered=threading.Event(); release=threading.Event()
    class Provider:
        calls=0
        def complete(self,*args):
            self.calls+=1; entered.set(); assert release.wait(5)
            return SimpleNamespace(usage={'total_tokens':9})
    provider=Provider(); budget=prepared(tmp_path); model=BudgetedModel(provider,budget,'task')
    model.preflight([],[])
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(model.complete,[],[]); assert entered.wait(5)
        second=pool.submit(model.complete,[],[])
        with pytest.raises(ModelBudgetError): second.result(5)
        release.set(); assert first.result(5).usage['total_tokens']==9
    assert provider.calls==1 and budget.summary()['all_time']['request_count']==1


def test_reservation_callback_cannot_expand_estimated_payload(tmp_path,monkeypatch):
    budget=prepared(tmp_path); messages=[{'role':'user','content':'small'}]
    reserve=budget.reserve
    def mutate(*args):
        row=reserve(*args); messages[0]['content']='large'*1000; return row
    monkeypatch.setattr(budget,'reserve',mutate)
    provider=Model([]); model=BudgetedModel(provider,budget,'task'); model.preflight(messages,[])
    with pytest.raises(ModelBudgetError): model.complete(messages,[])
    assert provider.requests==[] and budget.summary()['all_time']['unsettled_requests']==1


def test_provider_receives_frozen_payload_not_mutable_caller_reference(tmp_path):
    budget=prepared(tmp_path); messages=[{'role':'user','content':'fixed'}]
    class Provider:
        def complete(self,actual,tools):
            messages[0]['content']='changed after validation'
            assert actual[0]['content']=='fixed'
            assert actual is not messages
            return SimpleNamespace(usage={'total_tokens':10})
    model=BudgetedModel(Provider(),budget,'task'); model.preflight(messages,[]); model.complete(messages,[])
    assert budget.summary()['all_time']['actual_tokens']==10


def test_settled_provider_usage_survives_agent_response_write_failure(tmp_path,monkeypatch):
    budget=prepared(tmp_path); store=AgentStore(tmp_path/'agent.db')
    original=store.record_step
    def fail_response(task,claim,kind,metadata):
        if kind=='model_response': raise RuntimeError('synthetic response write failure')
        return original(task,claim,kind,metadata)
    monkeypatch.setattr(store,'record_step',fail_response)
    model=Model(answer_turns())
    result=AgentService(store,binding(),knowledge=Knowledge(),model=model,usage_budget=budget).process(envelope())
    assert result['state']=='needs_review' and len(model.requests)==1
    assert budget.summary()['all_time']['actual_tokens']==7
    sync_existing_usage(budget,store)
    assert budget.summary()['all_time']['actual_tokens']==7
    assert budget.summary()['all_time']['request_count']==1
    assert budget.summary()['all_time']['unsettled_requests']==0


def test_late_historical_response_reconciles_same_original_request_identity(tmp_path):
    budget=prepared(tmp_path)
    request={'id':1,'kind':'model_request','created_at':datetime.now(timezone.utc).timestamp(),'metadata':{}}
    steps=[request]
    store=SimpleNamespace(list_tasks=lambda:[{'task_id':'old'}],list_steps=lambda _:deepcopy(steps))
    sync_existing_usage(budget,store)
    assert budget.summary()['all_time']['unknown_unbounded_requests']==1
    steps.append({'id':2,'kind':'model_response','created_at':request['created_at']+1,'metadata':{'usage':{'total_tokens':1140}}})
    sync_existing_usage(budget,store)
    result=budget.summary()['all_time']
    assert result['actual_tokens']==1140 and result['request_count']==1 and result['unsettled_requests']==0
