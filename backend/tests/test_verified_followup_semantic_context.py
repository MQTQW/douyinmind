"""Issuer-backed followup planning, only temp ledgers and fake provider turns."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.assistant.semantic_intent_model import (
    FOLLOWUP_CONTEXT, INTENT_TOOLS, SemanticIntentError, intent_messages,
    intent_tools, parse_intent_turn,
)
from app.assistant.semantic_plan_store import (
    SemanticPlanError, SemanticPlanStore, decision_metadata, ui_semantic_digest,
)
from app.assistant.semantic_resolution import (
    catalog_snapshot_from_grants, owner_scope_from_envelope, semantic_source_key,
)
from app.mention_agent.followup_evidence import make_followup_envelope
from tests.test_semantic_command_resolution import case as semantic_case
from tests.test_semantic_planning_store import turn
from tests.test_thread_followup_owner_review import case


def prepared(case, text='帮我总结下'):
    detail=deepcopy(case.detail)
    detail['command_text']=detail['evidence']['followup_text']=text
    env=make_followup_envelope(detail,case.binding)
    user=case.c.registry.snapshot()['users'][0];device=user['devices'][0]
    owner=owner_scope_from_envelope(env,user,device)
    catalog=catalog_snapshot_from_grants([g for g in device['libraries']
        if g.get('active') and g.get('library_exists')])
    return env,owner,catalog


def context(case,env,owner,*,checkpoint=lambda:True):
    return case.c.store.verified_followup_context(env,binding=case.binding,
        owner_scope=owner,source_checkpoint=checkpoint,should_stop=lambda:False)


def run(case,env,owner,catalog,**changes):
    args=dict(source_key=semantic_source_key(env),frozen_ui_digest=ui_semantic_digest(env),
        owner_scope=owner,catalog_snapshot=catalog,mode='root',original_text=env['comment_text'],
        model=case.c.model,budget=case.c.budget,scope_authorizer=lambda:True,should_stop=lambda:False)
    args.update(changes)
    return case.c.store.run_once(**args)


@pytest.mark.parametrize('text',['帮我总结下','展开讲讲','具体说说'])
def test_verified_short_followup_reaches_fake_model_with_current_video_and_query_only_tools(case,text):
    env,_,_=prepared(case,text)
    result=case.c.router(env,source_checkpoint=lambda:True)
    assert result.action=='query' and result.binding.is_all_local
    messages,tools=case.c.model.requests[0]
    assert messages[-1]=={'role':'user','content':text}
    assert '同一视频线程中的普通追问' in messages[0]['content']
    assert '省略宾语时默认承接当前已核对视频' in messages[0]['content']
    assert '不按词表强制转成查询' in messages[0]['content']
    assert tools[0]['function']['parameters']['properties']['action']['enum']==['query','clarify']
    receipt=case.c.store.get_recorded_decision(env,binding=result.binding)
    assert receipt.decision['action']=='query'


def test_generic_root_native_and_choice_keep_original_tools_and_messages():
    catalog={'revision':'catalog-test','options':[]}
    for mode in ('root','native','choice'):
        assert intent_tools()==INTENT_TOOLS
        messages=intent_messages(mode,'原话',catalog)
        assert messages[-1]['content']=='原话'
        assert '同一视频线程中的普通追问' not in messages[0]['content']
        assert 'save表示明确请求保存' in messages[0]['content']
    changed=intent_tools(followup_context=deepcopy(FOLLOWUP_CONTEXT))
    changed[0]['function']['parameters']['properties']['action']['enum'].append('save')
    assert intent_tools(followup_context=deepcopy(FOLLOWUP_CONTEXT))[0]['function']['parameters']['properties']['action']['enum']==['query','clarify']


def test_json_or_bool_flags_cannot_mint_private_followup_context(case):
    env,owner,catalog=prepared(case)
    for fake in (True,False,1,{},deepcopy(FOLLOWUP_CONTEXT)):
        with pytest.raises(SemanticPlanError,match='semantic_followup_context_required'):
            run(case,env,owner,catalog,followup_context=fake)
    assert not case.c.model.requests
    assert case.c.budget.summary()['all_time']['request_count']==0
    assert case.c.store.lookup_receipt(semantic_source_key(env)) is None
    bad=deepcopy(FOLLOWUP_CONTEXT);bad['version']=True
    with pytest.raises(SemanticIntentError):intent_tools(followup_context=bad)
    bad=deepcopy(FOLLOWUP_CONTEXT);bad['query_only']=1
    with pytest.raises(SemanticIntentError):intent_messages('root','原话',catalog,followup_context=bad)


def test_only_strict_live_ui_proof_and_exact_true_checkpoint_can_issue_context(case):
    env,owner,_=prepared(case)
    for checkpoint in (None,True,lambda:False,lambda:1):
        with pytest.raises(SemanticPlanError):context(case,env,owner,checkpoint=checkpoint)
    for stop in (None,False,lambda:True,lambda:0,lambda:None):
        with pytest.raises(SemanticPlanError):
            case.c.store.verified_followup_context(env,binding=case.binding,owner_scope=owner,
                source_checkpoint=lambda:True,should_stop=stop)
    for key,value in [('query_only',1),('real_mention',True),('unique_count',True),
            ('sender_verified',False),('prior_reply_sha256','0'*64),('instance_id','other')]:
        bad=deepcopy(env);bad['evidence'][key]=value
        with pytest.raises(SemanticPlanError):context(case,bad,owner)
    for seconds in (-121,20):
        bad=deepcopy(env)
        bad['evidence']['observed_at_utc']=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
        with pytest.raises(SemanticPlanError):context(case,bad,owner)
    assert not case.c.model.requests


def test_context_is_bound_to_issuer_snapshot_owner_ui_and_raw_text(case,tmp_path):
    env,owner,catalog=prepared(case)
    foreign=SemanticPlanStore.prepare(tmp_path/'foreign-context.sqlite3')
    token=context(case,env,owner)
    args=dict(source_key=semantic_source_key(env),frozen_ui_digest=ui_semantic_digest(env),
        owner_scope=owner,catalog_snapshot=catalog,mode='root',original_text=env['comment_text'],
        model=case.c.model,budget=case.c.budget,scope_authorizer=lambda:True,should_stop=lambda:False,
        followup_context=token)
    with pytest.raises(SemanticPlanError):foreign.run_once(**args)
    for changes in ({'frozen_ui_digest':'0'*64},{'original_text':'不同原话'},
            {'owner_scope':{**owner,'user_id':'different-user'}},{'mode':'choice'}):
        with pytest.raises(SemanticPlanError):run(case,env,owner,catalog,followup_context=token,**changes)
    for attribute,value in (('_snapshot','{}'),('_checkpoint',lambda:True),('_stop',lambda:False),('_seal',b'fake')):
        altered=context(case,env,owner);setattr(altered,attribute,value)
        with pytest.raises(SemanticPlanError):run(case,env,owner,catalog,followup_context=altered)
    assert not case.c.model.requests
    assert case.c.store.lookup_receipt(semantic_source_key(env)) is None


def test_source_checkpoint_is_rechecked_before_fee_and_after_provider(case):
    env,owner,catalog=prepared(case)
    current=[True]
    token=context(case,env,owner,checkpoint=lambda:current[0])
    current[0]=False
    with pytest.raises(SemanticPlanError):run(case,env,owner,catalog,followup_context=token)
    assert not case.c.model.requests
    current[0]=True
    token=context(case,env,owner,checkpoint=lambda:current[0])
    case.c.model.hook=lambda:current.__setitem__(0,False)
    with pytest.raises(SemanticPlanError):run(case,env,owner,catalog,followup_context=token)
    assert len(case.c.model.requests)==1
    row=case.c.store.lookup_receipt(semantic_source_key(env))
    assert row['state']=='unknown' and row['usage']['total_tokens']==20
    assert case.c.store.get_recorded_decision(env) is None


def test_fresh_ui_time_reuses_same_key_same_context_and_never_calls_twice(case):
    env,owner,catalog=prepared(case)
    receipt=run(case,env,owner,catalog,followup_context=context(case,env,owner))
    metadata=decision_metadata(receipt)
    newer=deepcopy(env);newer['evidence']['observed_at_utc']=datetime.now(timezone.utc).isoformat()
    again=run(case,newer,owner,catalog,followup_context=context(case,newer,owner))
    assert again.plan_id==receipt.plan_id and again.request_key==receipt.request_key
    assert decision_metadata(again)==metadata and len(case.c.model.requests)==1
    assert case.c.budget.summary()['all_time']['request_count']==1


@pytest.mark.parametrize('state',['complete','held','unknown','reserved'])
def test_old_same_source_without_context_is_never_reinterpreted_or_rekeyed(case,state):
    env,owner,catalog=prepared(case)
    case.c.model.decision={'action':'clarify','target_state':'unspecified','grant_id':None}
    old=run(case,env,owner,catalog)
    request_key=old.request_key
    assert 'followup_context' not in decision_metadata(old)
    if state=='complete':
        assert '保存' in case.c.router.recorded_resolution({'envelope':env,'binding':case.binding.to_dict()}).clarification
    if state!='complete':
        with case.c.store._connection(write=True) as db:
            db.execute('UPDATE semantic_plans SET state=? WHERE plan_id=?',(state,old.plan_id))
    before=case.c.store.path.read_bytes();fees=deepcopy(case.c.budget.summary())
    with pytest.raises(SemanticPlanError,match='semantic_plan_scope_conflict'):
        run(case,env,owner,catalog,followup_context=context(case,env,owner))
    assert case.c.store.path.read_bytes()==before
    assert case.c.budget.summary()==fees and len(case.c.model.requests)==1
    row=case.c.store.lookup_receipt(semantic_source_key(env))
    assert row['plan_id']==old.plan_id and row['request_key']==request_key and row['state']==state
    if state=='complete':
        assert '保存' in case.c.router.recorded_resolution({'envelope':env,'binding':case.binding.to_dict()}).clarification


@pytest.mark.parametrize('action',['save','cancel'])
def test_provider_cannot_escape_query_only_tool_even_if_it_returns_old_action(case,action):
    env,owner,catalog=prepared(case)
    case.c.model.decision={'action':action,'target_state':'unspecified','grant_id':None}
    with pytest.raises(SemanticPlanError,match='semantic_response_invalid'):
        run(case,env,owner,catalog,followup_context=context(case,env,owner))
    row=case.c.store.lookup_receipt(semantic_source_key(env))
    assert row['state']=='held' and row['usage']['total_tokens']==20
    assert row['dispatchable'] is False and case.c.store.get_recorded_decision(env) is None
    with pytest.raises(SemanticPlanError):
        run(case,env,owner,catalog,followup_context=context(case,env,owner))
    assert len(case.c.model.requests)==1
    assert parse_intent_turn(turn(case.c.model.decision),catalog)['action']==action


def test_explicit_authorized_library_remains_available_to_query_only_planner(case):
    env,_,_=prepared(case,'在学习笔记里再展开讲讲')
    case.c.model.decision={'action':'query','target_state':'one','grant_id':case.c.grant['grant_id']}
    result=case.c.router(env,source_checkpoint=lambda:True)
    assert result.action=='query' and result.binding.knowledge_base_id==5
    assert result.query_scope=='library' and not result.choice_options


def test_old_completed_query_receipt_keeps_readonly_authority_and_is_not_replanned(case):
    env,owner,catalog=prepared(case)
    old=run(case,env,owner,catalog)
    original=decision_metadata(old);before=case.c.store.path.read_bytes()
    # Introducing the stronger input contract never creates a new request for
    # an already paid source, nor invalidates its existing verified receipt.
    result=case.c.router(env,source_checkpoint=lambda:True)
    assert result.action not in {'query','save'} and len(case.c.model.requests)==1
    recorded=case.c.router.recorded_resolution({'envelope':env,'binding':case.binding.to_dict()})
    assert recorded.action=='query'
    assert decision_metadata(case.c.store.get_recorded_decision(env,binding=recorded.binding))==original
    assert case.c.store.path.read_bytes()==before


def test_only_new_context_clarification_uses_query_followup_copy(case):
    env,_,_=prepared(case,'我不太确定')
    case.c.model.decision={'action':'clarify','target_state':'unspecified','grant_id':None}
    result=case.c.router(env,source_checkpoint=lambda:True)
    assert result.reason_code=='semantic_intent_unclear'
    assert '当前视频' in result.clarification and '保存' not in result.clarification
    receipt=case.c.store.get_recorded_decision(env,binding=case.binding)
    assert decision_metadata(receipt)['followup_context']==FOLLOWUP_CONTEXT
    # Re-reading the durable immutable identity preserves this contract even
    # across process/store reconstruction, without changing old paid rows.
    reopened=SemanticPlanStore(case.c.store.path)
    recovered=reopened.get_recorded_decision(env,binding=case.binding)
    assert decision_metadata(recovered)['followup_context']==FOLLOWUP_CONTEXT
    assert case.c.router.resolver.resolve_verified_intent(env,recovered).clarification==result.clarification
    historic=case.c.router.recorded_resolution({'envelope':env,'binding':case.binding.to_dict()})
    assert historic.clarification==result.clarification and len(case.c.model.requests)==1


def test_external_flags_cannot_relabel_an_old_paid_clarification(case):
    env,owner,catalog=prepared(case)
    case.c.model.decision={'action':'clarify','target_state':'unspecified','grant_id':None}
    receipt=run(case,env,owner,catalog)
    for fake in (True,deepcopy(FOLLOWUP_CONTEXT)):
        decision={**case.c.model.decision,'followup_context':fake}
        with pytest.raises(SemanticIntentError):parse_intent_turn(turn(decision),catalog)
        # Also exercise an untrusted metadata-shaped value in a legacy row:
        # only the original immutable hash can establish the new marker.
        with case.c.store._connection(write=True) as db:
            db.execute('UPDATE semantic_plans SET decision=? WHERE plan_id=?',(json.dumps(decision),receipt.plan_id))
        assert 'followup_context' not in decision_metadata(receipt)
        result=case.c.router.recorded_resolution({'envelope':env,'binding':case.binding.to_dict()})
        assert '保存' in result.clarification
    assert len(case.c.model.requests)==1


@pytest.mark.parametrize('target',['ambiguous','unknown'])
def test_new_context_library_clarification_stays_query_only_after_reload(case,target):
    env,_,_=prepared(case,'那个资料库呢')
    case.c.model.decision={'action':'clarify','target_state':target,'grant_id':None}
    result=case.c.router(env,source_checkpoint=lambda:True)
    assert result.action=='clarify' and '查询' in result.clarification and '保存' not in result.clarification
    again=case.c.router.recorded_resolution({'envelope':env,'binding':case.binding.to_dict()})
    assert again.clarification==result.clarification and len(case.c.model.requests)==1
