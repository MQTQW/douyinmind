"""Independent native-origin accounting review; owned temp DBs/fake providers."""
from datetime import datetime, timezone
import json

import pytest

from app.assistant.commands import CommandResolver
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.native_semantic_inbox import native_message_source_key, native_message_digest
from app.assistant.runtime import AssistantRuntime
from app.assistant.semantic_plan_store import SemanticPlanStore, SemanticPlanError, decision_metadata
from app.assistant.semantic_resolution import catalog_snapshot_from_grants, owner_scope_from_envelope
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.model import ModelTurn
from tests.test_assistant_product_bridge import system
from tests.test_mention_agent_import_end_to_end import Backend, Knowledge
from tests.test_semantic_engine_library_gate import MeasuredImportModel


def prepared(tmp_path,*,scope_lost=False):
    binding,agent,env,registry,migrated,queue,history,control=system(tmp_path)
    env['comment_text']='@助手 把这条放进刚才说的那个知识库吧'
    env['evidence']['observed_at_utc']=datetime.now(timezone.utc).isoformat()
    snapshot=registry.snapshot();user=next(u for u in snapshot['users'] if u['user_id']==migrated['user_id'])
    device=next(d for d in user['devices'] if d['device_id']==migrated['device_id'])
    grants=[g for g in device['libraries'] if g['active'] and g['library_exists']]
    catalog=catalog_snapshot_from_grants(grants);ui_owner=owner_scope_from_envelope(env,user,device)
    facts={'namespace':'douyin_web_v1','source_kind':'native_notice_candidate','native_notice_id':'8100001',
        'native_comment_id':'8100002','notice_type':45,'recipient_uid':'9001','sender_uid':'8001',
        'sender_sec_uid':binding.sender_profile_url.rsplit('/',1)[-1],
        'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'video_id':env['video_id'],'content':env['comment_text'],'created_time':str(int(datetime.now(timezone.utc).timestamp())-3),
        'parent_id_hint':None,'relation_status':'unknown','root_comment_id':None,'parent_comment_id':None,
        'dispatchable':False,'at_sub_type':2}
    source=native_message_source_key(facts)
    native_owner={**ui_owner,'reference_id':source,'root_reference_id':source}
    budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3');store=SemanticPlanStore.prepare(tmp_path/'semantic.sqlite3')
    active=[True]
    class Planner:
        calls=0
        def complete(self,*_):
            self.calls+=1
            if scope_lost:active[0]=False
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'native-paid-once','type':'function',
                'function':{'name':'submit_intent','arguments':json.dumps({'action':'save','target_state':'one',
                    'grant_id':migrated['grant_id']})}}]}, {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')
    planner=Planner()
    args={'source_key':source,'frozen_ui_digest':native_message_digest(facts),'owner_scope':native_owner,
        'catalog_snapshot':catalog,'mode':'native','original_text':facts['content'],'native_facts':facts,
        'model':planner,'budget':budget,'scope_authorizer':lambda:active[0],'should_stop':lambda:False}
    if scope_lost:
        with pytest.raises(SemanticPlanError):store.run_once(**args)
        receipt=None
    else:receipt=store.run_once(**args)
    return {'binding':binding,'agent':agent,'env':env,'registry':registry,'migrated':migrated,'queue':queue,
        'history':history,'control':control,'catalog':catalog,'owner':ui_owner,'facts':facts,'source':source,
        'budget':budget,'store':store,'planner':planner,'receipt':receipt}


def origin_row(case):
    with case['store']._connection() as db:
        return tuple(db.execute('SELECT * FROM semantic_plans WHERE source_key=?',(case['source'],)).fetchone())


def queue_for_display(case, resolution):
    q,h,e=case['queue'],case['history'],case['env']
    task=q.enqueue(resolution.binding,e,kind=resolution.action,user_id=resolution.user_id,
        device_id=resolution.device_id,authorization=resolution.authorization)
    h.bind_task(task['id'],resolution.binding,e,e['reference_id'],
        {'quality':'current_comment','root_reference_id':e['reference_id']})
    return task


def test_native_paid_origin_ui_root_and_final_share_unchanged_scope_and_request(tmp_path):
    c=prepared(tmp_path);s,e,b=c['store'],c['env'],c['budget'];origin=origin_row(c);private=c['receipt']
    assert s.get_recorded_decision(e,binding=c['binding']) is None
    assert s.get_recorded_accounting(e,binding=c['binding'],owner_scope=c['owner']) is None
    bound=s.bind_native_to_ui(private,e,owner_scope=c['owner'],catalog_snapshot=c['catalog'],
        source_facts=c['facts'],scope_authorizer=lambda:True,should_stop=lambda:False)
    assert bound.plan_id==private.plan_id and bound.request_key==private.request_key
    assert bound.budget_scope_id==private.budget_scope_id
    assert decision_metadata(private)['mode']=='native' and decision_metadata(bound)['mode']=='root'
    assert decision_metadata(bound)['owner_scope']==c['owner']
    assert origin_row(c)==origin  # UI binding never edits the paid origin row.
    resolution=CommandResolver(c['registry']).resolve_verified_intent(e,bound)
    assert resolution.action=='save' and resolution.binding.knowledge_base_id==7
    task=queue_for_display(c,resolution)
    backend=Backend();model=MeasuredImportModel([('save_current_video',{'knowledge_base_id':7})])
    bridge=LegacyBridge(c['registry'],c['queue'],c['control'],agent_path=tmp_path/'agent.sqlite3',
        conversation=c['history'],database=tmp_path/'unused-business.db',import_client=backend,
        model_factory=lambda:model,knowledge_factory=lambda _:Knowledge(),usage_budget=b,semantic_store=s)
    done=AssistantRuntime(c['queue'],c['control'],execute=bridge.execute).execute_once()
    assert done['state']=='ready_to_publish' and done['usage']['total_tokens']==27
    assert done['usage']['model_responses']==2 and c['planner'].calls==model.count==backend.posts==1
    assert b.summary(private.budget_scope_id)['task']['actual_tokens']==27
    assert b.summary()['all_time']['request_count']==2
    assert origin_row(c)==origin
    fee=s.get_recorded_accounting(e,binding=resolution.binding,owner_scope=c['owner'])
    assert fee['request_keys']==[private.request_key] and fee['dispatchable'] is False
    with s._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM semantic_ui_bindings').fetchone()[0]==1
    # Idempotent actual UI binding and accounting do not pay again.
    s.bind_native_to_ui(private,e,owner_scope=c['owner'],catalog_snapshot=c['catalog'],
        source_facts=c['facts'],scope_authorizer=lambda:True,should_stop=lambda:False)
    bridge.reconcile(done)
    assert c['planner'].calls==model.count==backend.posts==1 and origin_row(c)==origin


def test_native_paid_unknown_can_bind_only_fee_view_not_execute_or_publish(tmp_path):
    c=prepared(tmp_path,scope_lost=True);s,e,b=c['store'],c['env'],c['budget'];origin=origin_row(c)
    assert s.get_native_decision(c['source']) is None and b.summary()['all_time']['actual_tokens']==20
    fee=s.bind_native_accounting_to_ui(c['source'],e,owner_scope=c['owner'],source_facts=c['facts'],
        scope_authorizer=lambda:True,should_stop=lambda:False,binding=c['binding'])
    assert fee['usage']['total_tokens']==20 and fee['state']=='unknown' and fee['dispatchable'] is False
    assert s.get_recorded_decision(e,binding=c['binding']) is None and origin_row(c)==origin
    # A display fixture can expose already measured cost, but cannot create an
    # executable clarification, query or save from that accounting record.
    resolution=CommandResolver(c['registry']).resolve(e)
    task=queue_for_display(c,resolution)
    bridge=LegacyBridge(c['registry'],c['queue'],c['control'],agent_path=tmp_path/'agent.sqlite3',
        conversation=c['history'],database=tmp_path/'unused-business.db',usage_budget=b,semantic_store=s,
        model_factory=lambda:pytest.fail('fee-only view must not construct model'))
    observed=bridge._snapshot(task)
    assert observed['state']=='needs_review' and observed['safe_to_execute'] is False
    assert observed['safe_to_publish'] is False and observed['usage']['total_tokens']==20
    foreign={**task,'user_id':'different-valid-user-id'}
    assert bridge._authorized(foreign) is False
    assert bridge.execute(foreign)['state']=='needs_review'
    done=AssistantRuntime(c['queue'],c['control'],execute=bridge.execute).execute_once()
    assert done['state']=='needs_review' and b.summary()['all_time']['request_count']==1
    assert c['planner'].calls==1 and origin_row(c)==origin


def test_native_catalog_change_is_accounting_only_and_cannot_retarget_paid_origin(tmp_path):
    c=prepared(tmp_path);s,e=c['store'],c['env'];origin=origin_row(c)
    changed={**c['catalog'],'revision':'changed-catalog-revision'}
    with pytest.raises(SemanticPlanError):
        s.bind_native_to_ui(c['receipt'],e,owner_scope=c['owner'],catalog_snapshot=changed,
            source_facts=c['facts'],scope_authorizer=lambda:True,should_stop=lambda:False)
    assert origin_row(c)==origin and s.get_recorded_decision(e,binding=c['binding']) is None
    fee=s.bind_native_accounting_to_ui(c['source'],e,owner_scope=c['owner'],source_facts=c['facts'],
        scope_authorizer=lambda:True,should_stop=lambda:False,binding=c['binding'])
    assert fee['request_keys']==[c['receipt'].request_key] and fee['usage']['total_tokens']==20
    assert s.get_recorded_decision(e,binding=c['binding']) is None
    assert origin_row(c)==origin and c['planner'].calls==1
