"""Semantic choice is meaning only: actual temp ledgers, fake model and UI."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.assistant.choice_intent import parse_library_selection,semantic_choice_catalog,semantic_choice_owner
from app.assistant.semantic_plan_store import SemanticPlanStore,ui_semantic_digest,decision_metadata
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.model import ModelTurn
from tests.test_assistant_choice_controller import case,root,publish_fact,Adapter,choice_detail,admit,A,B


TEXT='请帮我放到生活库里吧，谢谢'


class SemanticAdapter(Adapter):
    def __init__(self,case,text=TEXT):
        super().__init__(case,text=text,target=8)
        self.allowed=[];self.fresh_text=None;self.native=None
    def read_choice_replies(self,session,allowed,*,keep_open=False,candidate_text=None):
        self.allowed.append((list(allowed),candidate_text))
        value=super().read_choice_replies(session,allowed,keep_open=keep_open)
        return [choice_detail(self.case,session,text=self.text,target=self.target,native=self.native)] if value and self.native else value
    def observe_current_choice_replies(self,session,allowed,*,candidate_text=None):
        self.current_reads.append(session['choice_id'])
        return [choice_detail(self.case,session,text=self.fresh_text or self.text,target=self.target,native=self.native)]


class Model:
    def __init__(self,grant):self.grant=grant;self.calls=0;self.hook=lambda:None;self.error=None;self.action='save';self.target_state='one';self.expected_text=TEXT
    def complete(self,messages,tools):
        self.calls+=1
        assert [item['function']['name'] for item in tools]==['submit_intent']
        assert messages[-1]['content']==self.expected_text
        self.hook()
        if self.error:raise self.error
        return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'semantic-choice-fake',
            'type':'function','function':{'name':'submit_intent','arguments':json.dumps({
                'action':self.action,'target_state':self.target_state,'grant_id':self.grant})}}]},
            {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')


@pytest.fixture
def semantic(case,tmp_path):
    task,session,_=root(case);publish_fact(case,task,session)
    session=case.store.get(session['choice_id'])
    plans=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    model=Model(session['options'][1]['authorization']['grant_id'])
    case.controller.semantic_choice_planner=lambda **kw:plans.run_once(**kw,model=model,budget=budget)
    case.controller.semantic_choice_receipt_loader=plans.get_recorded_decision
    candidate={'sender_profile_url':A,'receiver_profile_url':B,'video_id':session['root_envelope']['video_id'],
        'native_comment_id':'8999999000000000444','comment_text':TEXT,'choice_id':session['choice_id'],
        'root_reference_id':session['root_envelope']['reference_id']}
    adapter=SemanticAdapter(case)
    calls=[]
    def checkpoint(envelope):
        calls.append(deepcopy(envelope))
        return (envelope['comment_text']==TEXT and envelope['native_comment_id'] in (None,candidate['native_comment_id'])
                and envelope['evidence']['root_reference_id']==candidate['root_reference_id'])
    return SimpleNamespace(case=case,task=task,session=session,plans=plans,budget=budget,model=model,
        candidate=candidate,adapter=adapter,checkpoint=checkpoint,checks=calls)


def collect(s):
    return s.case.controller.collect_pending(s.adapter,'semantic-owner',choice_id=s.session['choice_id'],
        source_checkpoint=s.checkpoint,selection_candidate=s.candidate)


def test_natural_selection_uses_one_recorded_model_then_original_raw_child(semantic):
    s=semantic
    assert parse_library_selection(TEXT,s.session['options']) is None
    rows=collect(s)
    assert len(rows)==1 and s.model.calls==1 and len(s.adapter.current_reads)==1
    assert s.adapter.allowed==[([],TEXT)]
    assert rows[0]['envelope']['comment_text']==TEXT
    assert rows[0]['envelope']['evidence']['real_mention'] is False
    assert not any('semantic' in key for key in rows[0]['envelope']['evidence'])
    record=s.case.store.semantic_selection_record(s.session['choice_id'])
    assert record['selected_grant_id']==s.session['options'][1]['authorization']['grant_id']
    child=admit(s.case,rows)[0]
    assert child['knowledge_base_id']==8
    receipt=s.case.controller.semantic_decision_for_selection(child)
    assert decision_metadata(receipt)['plan_id']==record['plan_id']
    assert s.case.controller.resolution_for_selection(child).action=='save'
    assert collect(s)==[] and s.model.calls==1


@pytest.mark.parametrize('failure',['no_parent','wrong_body','wrong_CID','source_rejected','revoked','fresh_body_changed','unknown_model','forged_receipt'])
def test_semantic_choice_never_reserves_without_actual_source_and_fresh_authority(semantic,failure):
    s=semantic
    if failure=='no_parent':s.adapter.details=False
    if failure=='wrong_body':s.adapter.text='different original reply'
    if failure=='wrong_CID':
        old=s.adapter.read_choice_replies
        def wrong(*a,**kw):
            values=old(*a,**kw);values[0]['native_comment_id']='8999999000000000999';return values
        s.adapter.read_choice_replies=wrong
    if failure=='source_rejected':s.checkpoint=lambda env:False
    if failure=='revoked':s.model.hook=lambda:s.case.registry.revoke_user(s.case.prior['user_id'])
    if failure=='fresh_body_changed':s.model.hook=lambda:setattr(s.adapter,'fresh_text','different later public reply')
    if failure=='unknown_model':s.model.error=TimeoutError('synthetic no provider outcome')
    if failure=='forged_receipt':s.case.controller.semantic_choice_planner=lambda **kw:{'action':'save','grant_id':s.model.grant}
    assert collect(s)==[]
    session=s.case.store.get(s.session['choice_id'])
    assert session['selection_envelope'] is None and session['selected_kb_id'] is None
    assert s.case.queue.list_tasks()['total']==1
    if failure in {'no_parent','wrong_body','wrong_CID','source_rejected'}:assert s.model.calls==0
    if failure=='no_parent':
        status=s.case.controller.last_semantic_choice_status
        assert status['fallback_allowed'] is True and status['parent_proved'] is False and status['model_started'] is False
    if failure=='unknown_model':
        assert collect(s)==[] and s.model.calls==1


@pytest.mark.parametrize(('action','target','grant'),[('clarify','ambiguous',None),('cancel','unspecified',None),('query','unspecified',None)])
def test_other_semantic_actions_do_not_turn_into_import_or_second_model(semantic,action,target,grant):
    s=semantic;s.model.action=action;s.model.target_state=target;s.model.grant=grant
    assert collect(s)==[] and s.model.calls==1
    assert s.case.store.get(s.session['choice_id'])['selection_envelope'] is None
    assert s.case.controller.last_semantic_choice_status['model_started'] is True


def test_written_receipt_cannot_select_another_frozen_library(semantic):
    s=semantic;rows=collect(s);assert rows
    session=s.case.store.get(s.session['choice_id']);record=s.case.store.semantic_selection_record(session['choice_id'])
    receipt=s.plans.get_recorded_decision(session['selection_envelope'])
    other=s.session['options'][0]
    with pytest.raises(Exception):s.case.store.reserve_selection(session['choice_id'],expected_revision=session['revision'],
        selection_ref=session['selection_ref'],selection_envelope=session['selection_envelope'],
        selected_kb_id=other['knowledge_base_id'],selected_authorization=other['authorization'],semantic_decision=receipt)
    assert s.case.store.semantic_selection_record(session['choice_id'])==record


def test_frozen_catalog_keeps_old_ordinals_and_rejects_missing_or_unavailable_grants(semantic):
    options=deepcopy(semantic.session['options']);options[0]['ordinal']=4;options[1]['ordinal']=6
    value=semantic_choice_catalog(options)
    assert [item['ordinal'] for item in value['options']]==[4,6]
    assert all(item['can_query'] is False and item['can_import'] is True for item in value['options'])
    for mutation in ('no_auth','wrong_action','false_import'):
        bad=deepcopy(options)
        if mutation=='no_auth':bad[0].pop('authorization')
        if mutation=='wrong_action':bad[0]['authorization']['action']='query'
        if mutation=='false_import':bad[0]['can_import']=False
        with pytest.raises(ValueError):semantic_choice_catalog(bad)


def test_same_native_source_cannot_pay_again_when_UI_reference_hydrates(semantic):
    s=semantic
    s.model.hook=lambda:setattr(s.adapter,'fresh_text','changed UI after interpretation')
    assert collect(s)==[] and s.model.calls==1
    s.model.hook=lambda:None;s.adapter.fresh_text=None
    s.adapter.native=s.candidate['native_comment_id']
    assert collect(s)==[]
    assert s.model.calls==1
    assert s.case.store.get(s.session['choice_id'])['selection_envelope'] is None


def test_pure_contextual_choice_needs_neither_save_verb_nor_full_library_name(semantic):
    s=semantic;text='就生活那个吧'
    s.candidate['comment_text']=s.adapter.text=s.model.expected_text=text
    s.checkpoint=lambda envelope:(envelope['comment_text']==text
        and envelope['evidence']['root_reference_id']==s.session['root_envelope']['reference_id'])
    assert parse_library_selection(text,s.session['options']) is None
    rows=collect(s)
    assert len(rows)==1 and rows[0]['envelope']['comment_text']==text
    assert rows[0]['binding'].knowledge_base_id==8 and s.model.calls==1
