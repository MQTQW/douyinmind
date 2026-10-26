"""Real owned Registry/planning/budget, fake provider; no account or network."""
from types import SimpleNamespace
import pytest
from app.assistant.commands import CommandResolver
from app.assistant.registry import Registry
from app.assistant.semantic_plan_store import SemanticPlanStore,decision_metadata
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.contracts import Binding
from tests.test_mention_agent_store import envelope
from tests.test_semantic_planning_store import turn


@pytest.fixture
def case(tmp_path):
    registry=Registry(tmp_path/'registry.sqlite3',catalog_reader=lambda:[
        {'id':5,'name':'测试长视频'},{'id':6,'name':'学习笔记'}])
    binding=Binding('semantic-route-binding','semantic-route-instance',
        'https://www.douyin.com/user/semantic-route-A','https://www.douyin.com/user/semantic-route-B',5,True)
    migrated=registry.import_legacy(binding,verified_by='test-local-owner',allow_import=True)
    registry.grant_library(migrated['user_id'],migrated['device_id'],6,can_import=True)
    registry.set_local_owner_access(migrated['user_id'],migrated['device_id'],query_all_local=True,
        catalog_read_allowed=True,public_reply_allowed=True,grant_current_libraries=True,verified_by='test-owner')
    store=SemanticPlanStore.prepare(tmp_path/'semantic-plans.sqlite3')
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    user=registry.snapshot()['users'][0];device=user['devices'][0]
    grant=next(g for g in device['libraries'] if g['knowledge_base_id']==5)
    env=envelope(sender_profile_url=binding.sender_profile_url,receiver_profile_url=binding.receiver_profile_url,
        video_id='7999999000000000001',comment_text='@6 能不能帮我把这条收进测试长视频里呀？',
        reference_id='reference:semantic-route-source',command_id='semantic-route-command')
    class Model:
        def __init__(self):
            self.requests=[];self.hook=None
            self.decision={'action':'save','target_state':'one','grant_id':grant['grant_id']}
        def complete(self,messages,tools):
            self.requests.append((messages,tools))
            if self.hook:self.hook()
            return turn(self.decision)
    model=Model();stopped=[False]
    router=SemanticIntentInterpreter(registry,store,model,budget,should_stop=lambda:stopped[0])
    return SimpleNamespace(registry=registry,store=store,budget=budget,model=model,router=router,
        env=env,grant=grant,migrated=migrated,stopped=stopped)


def test_polite_question_can_mean_save_and_original_reaches_model_once(case):
    assert CommandResolver(case.registry).resolve(case.env).action!='save'
    result=case.router(case.env,source_checkpoint=lambda:True)
    assert result.action=='save' and result.binding.knowledge_base_id==5
    assert case.model.requests[0][0][-1]['content']==case.env['comment_text']
    receipt=case.store.get_recorded_decision(case.env,binding=result.binding)
    assert decision_metadata(receipt)['action']=='save'
    assert case.router(case.env,source_checkpoint=lambda:True).binding==result.binding
    assert len(case.model.requests)==1 and case.budget.summary()['all_time']['request_count']==1


@pytest.mark.parametrize('action,expected',[('query','query'),('save','clarify')])
def test_missing_library_obeys_user_defaults(case,action,expected):
    case.model.decision={'action':action,'target_state':'unspecified','grant_id':None}
    result=case.router(case.env,source_checkpoint=lambda:True)
    assert result.action==expected
    if action=='query':assert result.binding.is_all_local and result.query_scope=='all_local'
    else:
        assert result.reason_code=='save_library_required' and result.catalog_count==2
        assert result.binding is not None and result.choice_intent['kind']=='save_library_choice'


def test_foreign_or_json_receipt_cannot_grant_save(case):
    resolver=CommandResolver(case.registry)
    for value in (None,{'action':'save','grant_id':case.grant['grant_id']}):
        result=resolver.resolve_verified_intent(case.env,value)
        assert result.binding is None and result.action=='ignore'


@pytest.mark.parametrize('block',['pause','source'])
def test_stopped_or_missing_source_proof_never_bills_model(case,block):
    case.stopped[0]=block=='pause'
    result=case.router(case.env,source_checkpoint=lambda:block!='source')
    assert case.model.requests==[] and case.budget.summary()['all_time']['request_count']==0
    if block=='pause':assert result is None
    else:assert result.action!='save'


def test_permission_change_after_provider_does_not_fall_back_to_old_grammar(case):
    case.model.hook=lambda:case.registry.revoke_grant(case.grant['grant_id'])
    result=case.router(case.env,source_checkpoint=lambda:True)
    assert result.action!='save' and len(case.model.requests)==1
    again=case.router(case.env,source_checkpoint=lambda:True)
    assert again.action!='save' and len(case.model.requests)==1


@pytest.mark.parametrize('target',['ambiguous','unknown'])
def test_model_cannot_choose_first_library_when_target_is_unclear(case,target):
    case.model.decision={'action':'save','target_state':target,'grant_id':None}
    result=case.router(case.env,source_checkpoint=lambda:True)
    assert result.action=='clarify' and result.reason_code in {'semantic_library_ambiguous','unknown_library'}


def test_cancel_intent_cannot_create_a_save(case):
    case.model.decision={'action':'cancel','target_state':'unspecified','grant_id':None}
    result=case.router(case.env,source_checkpoint=lambda:True)
    assert result.action=='clarify' and result.reason_code=='semantic_cancelled'
