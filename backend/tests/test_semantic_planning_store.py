"""Real temporary owned plan ledgers, fake models; no application state/network."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from app.assistant.semantic_plan_store import (SemanticPlanStore,SemanticPlanError,RecordedSemanticDecision,
    budget_scope_for,ui_semantic_digest,decision_metadata,verify_semantic_decision)
from app.assistant.semantic_intent_model import parse_intent_turn,SemanticIntentError,default_semantic_model
from app.assistant.usage_budget import UsageBudget,UsageBudgetExceeded
from app.mention_agent.contracts import Binding
from app.mention_agent.model import ModelTurn


A='https://www.douyin.com/user/semantic-synthetic-A'
B='https://www.douyin.com/user/semantic-synthetic-B'
OWNER={'user_id':'synthetic-user','device_id':'synthetic-device','instance_id':'synthetic-instance',
       'sender_profile_url':A,'receiver_profile_url':B,'video_id':'7999999000000000001',
       'root_reference_id':'reference:synthetic-root','reference_id':'reference:synthetic-root'}
CATALOG={'revision':'catalog-1','options':[{'grant_id':'grant-one','name':'学习资料','aliases':[],
    'can_query':True,'can_import':True,'ordinal':1}]}
TEXT='@6 这条挺实用，帮我收进学习资料里吧，谢谢'
ENV={'command_id':'command:ignored-binding-dependent','reference_id':OWNER['reference_id'],
     'sender_profile_url':A,'receiver_profile_url':B,'video_id':OWNER['video_id'],'native_comment_id':None,
     'comment_text':TEXT,'source_mode':'isolated','evidence':{'real_mention':True,'sender_verified':True,
     'receiver_verified':True,'video_verified':True,'unique_count':1,'observed_at_utc':'2026-10-26T00:00:00Z',
     'diagnostics':{'render_count':1}}}
BINDING=Binding('semantic-binding','synthetic-instance',A,B,5,True)


def turn(decision=None,*,usage=None,args=None):
    decision=decision or {'action':'save','target_state':'one','grant_id':'grant-one'}
    return ModelTurn({'role':'assistant','content':'provider text must not be persisted',
        'reasoning_content':'private-model-thought-not-stored','tool_calls':[{'id':'fakecall','type':'function',
            'function':{'name':'submit_intent','arguments':json.dumps(decision) if args is None else args}}]},
        {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20} if usage is None else usage,'tool_calls')


class Model:
    def __init__(self,result=None,hook=None,error=None):self.calls=0;self.result=result or turn();self.hook=hook;self.error=error
    def complete(self,messages,tools):
        self.calls+=1
        assert len(tools)==1 and tools[0]['function']['name']=='submit_intent'
        assert messages[-1]['content']==TEXT
        if self.hook:self.hook()
        if self.error:raise self.error
        return deepcopy(self.result)


class Budget:
    def __init__(self):self.rows={};self.calls=[]
    def reserve(self,scope,key,estimate):
        self.calls.append((scope,key,estimate))
        if key in self.rows:return {'may_request':False}
        self.rows[key]={'state':'reserved','task_id':scope};return {'may_request':True}
    def settle(self,key,usage):
        self.rows[key].update(state='settled',total_tokens=usage.get('total_tokens'),
            input_tokens=usage.get('prompt_tokens'),output_tokens=usage.get('completion_tokens'))
        return dict(self.rows[key])
    def mark_unknown(self,key,reason):
        if self.rows[key]['state']!='settled':self.rows[key]['state']='unknown'


@pytest.fixture
def case(tmp_path):
    store=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    return store,Model(),Budget()


def run(store,model,budget,**changes):
    args={'source_key':'native:synthetic-comment','frozen_ui_digest':ui_semantic_digest(ENV),
          'owner_scope':deepcopy(OWNER),'catalog_snapshot':deepcopy(CATALOG),'mode':'root','original_text':TEXT,
          'model':model,'budget':budget,'scope_authorizer':lambda:True,'should_stop':lambda:False}
    args.update(changes)
    return store.run_once(**args)


def test_natural_text_produces_private_cached_receipt_once_without_action(case):
    store,model,budget=case
    receipt=run(store,model,budget)
    assert type(receipt) is RecordedSemanticDecision
    assert receipt.decision=={'action':'save','target_state':'one','grant_id':'grant-one'}
    assert receipt.budget_scope_id==budget_scope_for(OWNER)
    assert receipt.usage['total_tokens']==20
    assert run(store,model,budget).plan_id==receipt.plan_id and model.calls==1 and len(budget.calls)==1
    assert store.get_recorded_decision(ENV,binding=BINDING).plan_id==receipt.plan_id
    assert store.lookup_receipt('native:synthetic-comment')['dispatchable'] is False
    content=store.path.read_bytes()
    assert b'private-model-thought' not in content and b'provider text' not in content


@pytest.mark.parametrize('change',['owner','catalog','ui','mode','text'])
def test_same_source_scope_or_directory_change_never_pays_again(case,change):
    store,model,budget=case;run(store,model,budget)
    kwargs={}
    if change=='owner':kwargs['owner_scope']={**OWNER,'user_id':'other-user'}
    if change=='catalog':kwargs['catalog_snapshot']={**CATALOG,'revision':'catalog-2'}
    if change=='ui':kwargs['frozen_ui_digest']='f'*64
    if change=='mode':kwargs['mode']='choice'
    if change=='text':kwargs['original_text']=TEXT+' extra'
    with pytest.raises(SemanticPlanError,match='scope_conflict'):run(store,model,budget,**kwargs)
    assert model.calls==1 and len(budget.calls)==1


def test_ui_digest_ignores_observation_time_and_command_but_not_comment_identity(case):
    store,model,budget=case;receipt=run(store,model,budget)
    later=deepcopy(ENV);later['command_id']='another-binding-command';later['evidence']['observed_at_utc']='2030-01-01T00:00:00Z';later['evidence']['diagnostics']={}
    assert ui_semantic_digest(later)==ui_semantic_digest(ENV)
    assert store.get_recorded_decision(later,binding=BINDING).plan_id==receipt.plan_id
    later['native_comment_id']='7999999999999999999'
    assert store.get_recorded_decision(later,binding=BINDING) is None


@pytest.mark.parametrize('gate',['scope','stop','throws'])
def test_no_provider_or_budget_before_fresh_owner_gate(case,gate):
    store,model,budget=case
    def failure():raise RuntimeError('credential-not-recorded')
    kwargs={'scope_authorizer':lambda:False} if gate=='scope' else {'should_stop':lambda:True} if gate=='stop' else {'scope_authorizer':failure}
    with pytest.raises(SemanticPlanError,match='scope_unavailable'):run(store,model,budget,**kwargs)
    assert model.calls==0 and budget.calls==[] and store.lookup_receipt('native:synthetic-comment') is None


def test_revoke_after_response_preserves_cost_without_action_or_second_provider(case):
    store,model,budget=case;active=[True];model.hook=lambda:active.__setitem__(0,False)
    with pytest.raises(SemanticPlanError,match='scope_unavailable'):run(store,model,budget,scope_authorizer=lambda:active[0])
    row=store.lookup_receipt('native:synthetic-comment');assert row['state']=='unknown'
    assert next(iter(budget.rows.values()))['state']=='settled'
    active[0]=True
    with pytest.raises(SemanticPlanError):run(store,model,budget)
    assert model.calls==1


def test_timeout_is_durable_unknown_not_a_retry_on_restart(case):
    store,model,budget=case;model.error=TimeoutError('private-url-auth-error')
    with pytest.raises(SemanticPlanError,match='request_unknown'):run(store,model,budget)
    reopened=SemanticPlanStore(store.path)
    with pytest.raises(SemanticPlanError):run(reopened,Model(),budget)
    assert model.calls==1 and next(iter(budget.rows.values()))['state']=='unknown'
    assert 'private-url-auth-error' not in json.dumps(reopened.lookup_receipt('native:synthetic-comment'))


def test_reserved_dispatch_crash_cannot_be_claimed_again(case,monkeypatch):
    store,model,budget=case
    def crash(*a,**kw):raise SystemExit('synthetic-exit-before-provider')
    monkeypatch.setattr(budget,'reserve',crash)
    with pytest.raises(SystemExit):run(store,model,budget)
    with pytest.raises(SemanticPlanError):run(SemanticPlanStore(store.path),Model(),Budget())
    assert model.calls==0


@pytest.mark.parametrize('bad',[{'action':'save','target_state':'one','grant_id':'foreign-user-grant'},
    {'action':'save','target_state':'one','grant_id':'grant-one','authorization':True},
    {'action':'save','target_state':'unspecified','grant_id':'grant-one'},
    {'action':'delete','target_state':'unspecified','grant_id':None}])
def test_model_fields_cannot_choose_foreign_scope_or_supply_permissions(case,bad):
    store,model,budget=case;model.result=turn(bad)
    with pytest.raises(SemanticPlanError,match='response_invalid'):run(store,model,budget)
    assert store.lookup_receipt('native:synthetic-comment')['state']=='held'
    assert next(iter(budget.rows.values()))['state']=='settled'
    with pytest.raises(SemanticPlanError):run(store,model,budget)
    assert model.calls==1


@pytest.mark.parametrize('raw',['{"action":"save","action":"query","target_state":"one","grant_id":"grant-one"}',
    '{"action":"query","target_state":"unspecified","grant_id":NaN}'])
def test_duplicate_or_nonfinite_json_is_rejected(raw):
    with pytest.raises(SemanticIntentError):parse_intent_turn(turn(args=raw),CATALOG)


def test_one_provider_across_concurrent_store_instances(case):
    store,model,budget=case;entered=threading.Event();release=threading.Event()
    model.hook=lambda:(entered.set(),release.wait(5))
    other=SemanticPlanStore(store.path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(run,store,model,budget);assert entered.wait(5)
        second=pool.submit(run,other,Model(),budget)
        with pytest.raises(SemanticPlanError):second.result(timeout=5)
        release.set();assert first.result(timeout=5).decision['action']=='save'
    assert model.calls==1 and len(budget.calls)==1


def test_private_receipt_not_dict_and_agent_scope_mapping_checks_exact_envelope_and_binding(case):
    store,model,budget=case;receipt=run(store,model,budget)
    with pytest.raises(SemanticPlanError):decision_metadata(dict(decision_metadata(receipt)))
    with pytest.raises(SemanticPlanError):RecordedSemanticDecision(store,'x','y')
    assert verify_semantic_decision(receipt,mode='root',original_text=TEXT,grant_id='grant-one')['action']=='save'
    assert store.bind_agent_budget(receipt,'synthetic-agent',ENV,BINDING) is True
    assert store.agent_budget_scope('synthetic-agent',envelope=ENV,binding=BINDING)==receipt.budget_scope_id
    assert store.agent_budget_scope('unmapped-agent',envelope=ENV,binding=BINDING) is None
    with pytest.raises(SemanticPlanError):store.agent_budget_scope('synthetic-agent')
    with pytest.raises(SemanticPlanError):store.agent_budget_scope('synthetic-agent',envelope={**ENV,'comment_text':'different'},binding=BINDING)
    other=Binding('other-binding','synthetic-instance',A,B,7,True)
    with pytest.raises(SemanticPlanError):store.bind_agent_budget(receipt,'synthetic-agent',ENV,other)


def test_original_and_choice_share_command_budget_but_not_source_plan_identity(case):
    store,model,budget=case;root=run(store,model,budget)
    choice_owner={**OWNER,'reference_id':'reference:actual-choice'}
    choice=run(store,model,budget,source_key='native:actual-choice',owner_scope=choice_owner,mode='choice')
    assert root.plan_id!=choice.plan_id and root.budget_scope_id==choice.budget_scope_id
    new_command={**OWNER,'reference_id':'reference:new-root','root_reference_id':'reference:new-root'}
    assert budget_scope_for(new_command)!=root.budget_scope_id


def test_real_budget_scope_charges_planning_and_blocks_an_excess_followup(tmp_path):
    store=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3');budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    budget.set_limits(per_task_tokens=100000)
    receipt=run(store,Model(),budget)
    assert budget.summary(receipt.budget_scope_id)['task']['actual_tokens']==20
    with pytest.raises(UsageBudgetExceeded):budget.reserve(receipt.budget_scope_id,'agent-next-request',99990)


def test_budget_denial_creates_no_provider_request_and_does_not_auto_restart(tmp_path):
    store=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3');budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');budget.set_limits(per_task_tokens=0)
    model=Model()
    with pytest.raises(SemanticPlanError,match='budget_exceeded'):run(store,model,budget)
    budget.set_limits(per_task_tokens=100000)
    with pytest.raises(SemanticPlanError):run(store,model,budget)
    assert model.calls==0 and budget.summary()['daily']['request_count']==0


@pytest.mark.parametrize('kind',['empty','foreign','orphan'])
def test_unknown_existing_paths_are_not_adopted_or_recovered(tmp_path,kind):
    p=tmp_path/'semantic.sqlite3'
    if kind=='empty':p.write_bytes(b'')
    elif kind=='foreign':
        with sqlite3.connect(p) as db:db.execute('CREATE TABLE unrelated(x)')
    else:(tmp_path/'semantic.sqlite3-journal').write_bytes(b'unknown-journal')
    before={file.name:file.read_bytes() for file in tmp_path.iterdir()}
    with pytest.raises(SemanticPlanError):SemanticPlanStore(p,create=True)
    for name,data in before.items():assert (tmp_path/name).read_bytes()==data
    assert not (tmp_path/'semantic.sqlite3.owner.json').exists()


def test_default_model_is_lazy_bounded_existing_adapter():
    model=default_semantic_model()
    assert model.timeout==15.0 and model.thinking is False and model.client is None


def test_scope_revoked_after_budget_reserve_still_does_not_touch_provider(case):
    store,model,budget=case;checks=[0]
    def authorized():checks[0]+=1;return checks[0]<3
    with pytest.raises(SemanticPlanError,match='scope_unavailable'):run(store,model,budget,scope_authorizer=authorized)
    assert model.calls==0 and len(budget.calls)==1
    assert next(iter(budget.rows.values()))['state']=='unknown'


def test_lost_budget_reservation_ack_never_dispatches_or_rereserves(case,monkeypatch):
    store,model,budget=case;reserve=budget.reserve
    def lose_ack(*args):reserve(*args);raise RuntimeError('synthetic-reservation-ack-lost')
    monkeypatch.setattr(budget,'reserve',lose_ack)
    with pytest.raises(SemanticPlanError):run(store,model,budget)
    with pytest.raises(SemanticPlanError):run(SemanticPlanStore(store.path),model,budget)
    assert model.calls==0 and len(budget.calls)==1


def test_frozen_catalog_cannot_be_retargeted_by_caller_mutation_during_model(case):
    store,model,budget=case;catalog=deepcopy(CATALOG)
    model.hook=lambda:catalog['options'][0].update(grant_id='foreign-after-request')
    receipt=run(store,model,budget,catalog_snapshot=catalog)
    assert receipt.decision['grant_id']=='grant-one'
    assert decision_metadata(receipt)['catalog_snapshot']==CATALOG


def test_budget_scope_separates_same_reference_on_different_video():
    assert budget_scope_for(OWNER)!=budget_scope_for({**OWNER,'video_id':'7999999000000000002'})


@pytest.mark.parametrize('change',['multiple_calls','plain_text','wrong_tool'])
def test_no_plain_text_or_extra_tool_can_end_interpretation(change):
    value=turn()
    if change=='multiple_calls':value.message['tool_calls'].append(deepcopy(value.message['tool_calls'][0]))
    if change=='plain_text':value=ModelTurn({'role':'assistant','content':'已为你保存'},value.usage,'stop')
    if change=='wrong_tool':value.message['tool_calls'][0]['function']['name']='save_current_video'
    with pytest.raises(SemanticIntentError):parse_intent_turn(value,CATALOG)


@pytest.mark.parametrize('operation',['read','write'])
def test_cached_plan_store_rejects_foreign_hot_journal_without_recovery(tmp_path,operation):
    # Owned crash fixture only; no real ledger or user path is opened.
    from tests.test_cached_sqlite_foreign_review import foreign_hot_pair
    import shutil
    store=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    foreign,journal,_child=foreign_hot_pair(tmp_path,'semantic_meta')
    shutil.copyfile(foreign,store.path)
    target_journal=store.path.with_name(store.path.name+'-journal');shutil.copyfile(journal,target_journal)
    before=(store.path.read_bytes(),target_journal.read_bytes())
    with pytest.raises(SemanticPlanError):
        if operation=='read':store.lookup_receipt('native:synthetic-comment')
        else:run(store,Model(),Budget())
    assert (store.path.read_bytes(),target_journal.read_bytes())==before
