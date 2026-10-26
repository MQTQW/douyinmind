"""Only a first-request budget denial may be explicitly reopened.

Temporary SQLite and fictional scopes only. No budget/provider/browser/KB.
The product must independently prove zero budget-ledger request rows.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3
import time

import pytest

from app.mention_agent.store import AgentStore, AgentStoreError


def scope(**changes):
    return {'binding_id':'budget-binding','instance_id':'budget-desktop',
            'sender_profile_url':'https://www.douyin.com/user/budget-sender',
            'receiver_profile_url':'https://www.douyin.com/user/budget-receiver',
            'knowledge_base_id':7,'public_reply_allowed':True,'active':True,**changes}


def observation(**changes):
    return {'command_id':'budget-command','reference_id':'budget-reference',
            'reference_quality':'verified_ui','native_comment_id':None,
            'sender_profile_url':scope()['sender_profile_url'],
            'receiver_profile_url':scope()['receiver_profile_url'],
            'video_id':'7999999000000000011','comment_text':'@虚构助手 请根据测试库回答。',
            'source_mode':'live_ui','evidence':{'real_mention':True,'unique_count':1,
                'sender_verified':True,'video_verified':True,
                'observed_at_utc':datetime.now(timezone.utc).isoformat()},**changes}


@pytest.fixture
def case(tmp_path):
    store=AgentStore(tmp_path/'agent.sqlite3')
    binding,env=scope(),observation()
    store.put_binding(binding)
    task=store.enqueue(binding['binding_id'],env)
    claim=store.claim(task['task_id'])
    return store,binding,env,claim


def finish_denial(case, *, errors=None, expire=True):
    store,_,_,claim=case
    for metadata in errors if errors is not None else [
            {'code':'model_budget_exceeded','round':1,'error_type':'ModelBudgetError'}]:
        store.record_step(claim['task_id'],claim['claim_token'],'error',metadata)
    store.needs_review(claim['task_id'],claim['claim_token'],'model_budget_exceeded')
    if expire:
        with store._tx() as db:
            db.execute('UPDATE agent_tasks SET lease_until=? WHERE task_id=?',(time.time()-1,claim['task_id']))
    return claim['task_id']


def snapshot(store):
    with store._tx() as db:
        return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in ('agent_tasks','agent_steps','agent_replies','agent_bindings')}


def deny_without_mutation(case, *, binding=None, env=None):
    store,original,original_env,claim=case
    before=snapshot(store)
    with pytest.raises(AgentStoreError,match='unstarted_budget_(?:not_safe|scope_changed)'):
        store.resume_unstarted_budget(claim['task_id'],binding=original if binding is None else binding,
                                     envelope=original_env if env is None else env)
    assert snapshot(store)==before


def test_same_task_reopens_only_after_expiry_and_preserves_errors_attempts_and_envelope(case):
    store,binding,env,claim=case
    task_id=finish_denial(case)
    before=store.get_task(task_id);steps=store.list_steps(task_id)
    with store._tx() as db:
        tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    assert store.can_resume_unstarted_budget(task_id)
    queued=store.resume_unstarted_budget(task_id,binding=binding,envelope=env)
    assert queued['task_id']==task_id and queued['state']=='queued'
    assert queued['envelope']==before['envelope']==env
    assert queued['attempts']==before['attempts']==1
    assert queued['claim_token'] is queued['lease_until'] is queued['reason_code'] is None
    assert store.list_steps(task_id)==steps and store.get_reply(task_id) is None
    assert len(store.list_tasks())==1
    assert not store.can_resume_unstarted_budget(task_id),'queued is not a fresh denied task'
    with store._tx() as db:
        assert tables==[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def test_still_live_original_lease_blocks_even_fixed_budget_denial(case):
    store,_,_,claim=case
    task_id=finish_denial(case,expire=False)
    assert store.get_task(task_id)['lease_until']>time.time()
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


def test_repeated_same_scope_queued_noop_is_byte_equivalent_and_concurrent_safe(case):
    store,binding,env,claim=case
    task_id=finish_denial(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(lambda _:AgentStore(store.path).resume_unstarted_budget(
            task_id,binding=binding,envelope=env),range(2)))
    assert rows[0]==rows[1]
    before=snapshot(store)
    assert store.resume_unstarted_budget(task_id,binding=binding,envelope=env)==rows[0]
    assert snapshot(store)==before and len(store.list_tasks())==1


def test_old_owner_cannot_record_or_complete_after_requeue_or_new_claim(case):
    store,binding,env,old=case
    task_id=finish_denial(case)
    store.resume_unstarted_budget(task_id,binding=binding,envelope=env)
    with pytest.raises(AgentStoreError,match='task_not_processing'):
        store.record_step(task_id,old['claim_token'],'model_request',{'round':1})
    new=store.claim(task_id)
    assert new['attempts']==2 and new['claim_token']!=old['claim_token']
    with pytest.raises(AgentStoreError,match='stale_claim_token'):
        store.record_step(task_id,old['claim_token'],'model_request',{'round':1})
    with pytest.raises(AgentStoreError,match='stale_claim_token'):
        store.complete(task_id,old['claim_token'],{'kind':'clarification','text':'合成回复','source_ids':[],'sources':[]})
    assert store.list_steps(task_id)[0]['metadata']['code']=='model_budget_exceeded'
    assert len(store.list_steps(task_id))==1 and store.get_reply(task_id) is None
    deny_without_mutation(case)


@pytest.mark.parametrize('kind',['model_request','model_response','tool_call','tool_result','decision'])
def test_any_historical_execution_event_permanently_blocks_this_entry(case,kind):
    store,_,_,claim=case
    store.record_step(claim['task_id'],claim['claim_token'],kind,{'round':1})
    task_id=finish_denial(case)
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


@pytest.mark.parametrize('metadata',[
    {'code':'model_budget_unknown'}, {'code':'knowledge_query_failed'},
    {'code':'model_request_failed'}, {'code':'model_budget_exceeded','round':2},
    {'code':'model_budget_exceeded','round':True},
    {'code':'model_budget_exceeded','http_status':429},
    {'code':'model_budget_exceeded','request_sent':False},
    {'code':'model_budget_exceeded','error_type':'PRIVATE message'},
])
def test_only_fixed_first_preflight_denial_errors_qualify(case,metadata):
    store,_,_,claim=case
    task_id=finish_denial(case,errors=[metadata,{'code':'model_budget_exceeded','round':1}])
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


def test_no_steps_or_missing_task_never_qualify(case):
    store,_,_,claim=case
    finish_denial(case,errors=[])
    assert not store.can_resume_unstarted_budget(claim['task_id'])
    assert not store.can_resume_unstarted_budget('missing-task')
    deny_without_mutation(case)


@pytest.mark.parametrize('key,value',[
    ('binding_id','other-binding'),('instance_id','other-desktop'),
    ('sender_profile_url','https://www.douyin.com/user/other-sender'),
    ('receiver_profile_url','https://www.douyin.com/user/other-receiver'),
    ('knowledge_base_id',8),('active',False),('public_reply_allowed',False),
])
def test_every_binding_field_is_rechecked_not_only_library(case,key,value):
    finish_denial(case)
    changed={**case[1],key:value}
    deny_without_mutation(case,binding=changed)


def test_trimmed_identifier_is_not_accepted_as_the_exact_original_scope(case):
    finish_denial(case)
    deny_without_mutation(case,binding={**case[1],'instance_id':' '+case[1]['instance_id']})
    deny_without_mutation(case,env={**case[2],'command_id':' '+case[2]['command_id']})


@pytest.mark.parametrize('key,value',[
    ('command_id','other-command'),('reference_id','other-reference'),
    ('reference_quality','native'),('native_comment_id','79999999999'),
    ('sender_profile_url','https://www.douyin.com/user/other-sender'),
    ('receiver_profile_url','https://www.douyin.com/user/other-receiver'),
    ('video_id','7999999000000000022'),('comment_text','另一条问题'),('source_mode','replay'),
    ('evidence',{'real_mention':True,'unique_count':1,'sender_verified':True,'video_verified':True,
                 'observed_at_utc':'2026-10-22T00:00:00+00:00'}),
])
def test_every_original_envelope_field_is_exact_including_observation(case,key,value):
    finish_denial(case)
    changed={**case[2],key:value}
    if key=='reference_quality':changed['native_comment_id']='79999999999'
    deny_without_mutation(case,env=changed)


def test_revoked_current_binding_fails_even_if_old_valid_binding_is_passed(case):
    store,binding,_,claim=case
    task_id=finish_denial(case)
    store.deactivate(binding['binding_id'])
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


@pytest.mark.parametrize('token',[None,'existing-send-token'])
def test_any_existing_reply_or_send_reservation_blocks_even_without_step_history(case,token):
    store,_,_,claim=case
    store.complete(claim['task_id'],claim['claim_token'],{'kind':'clarification','text':'合成回复','source_ids':[],'sources':[]})
    with store._tx() as db:
        db.execute('UPDATE agent_replies SET send_token=? WHERE task_id=?',(token,claim['task_id']))
        db.execute("UPDATE agent_tasks SET state='processing' WHERE task_id=?",(claim['task_id'],))
    task_id=finish_denial(case)
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


def test_ingestion_link_and_orphan_event_barriers_are_not_bypassed(case):
    store,_,_,claim=case
    job=store.ensure_ingestion(claim['task_id'],claim['claim_token'])
    task_id=finish_denial(case)
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)
    with store._tx() as db:
        db.execute('DELETE FROM agent_task_ingestions WHERE task_id=?',(task_id,))
    assert store.get_ingestion(job['job_id']) is not None
    assert not store.can_resume_unstarted_budget(task_id),'task_id on prior ingestion events is still a side-effect barrier'
    deny_without_mutation(case)


@pytest.mark.parametrize('table',['agent_query_retries','agent_reply_hold_releases','agent_reply_revisions','agent_import_reply_updates'])
def test_optional_historical_side_effect_records_block_even_if_reply_was_removed(case,table):
    store,_,_,claim=case
    task_id=finish_denial(case)
    with store._tx() as db:
        # Minimal persisted-history fixture: the method may only observe this
        # task barrier; it must not create/migrate/delete any such table.
        db.execute('CREATE TABLE '+table+'(task_id TEXT PRIMARY KEY)')
        db.execute('INSERT INTO '+table+' VALUES(?)',(task_id,))
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


@pytest.mark.parametrize('state,reason',[
    ('processing','model_budget_exceeded'),('needs_review','model_budget_unknown'),
    ('needs_review','knowledge_query_failed'),('queued','model_budget_exceeded'),
    ('answer_ready','model_budget_exceeded'),
])
def test_other_lifecycle_states_and_unknown_outcomes_do_not_become_resume(case,state,reason):
    store,_,_,claim=case
    task_id=finish_denial(case)
    with store._tx() as db:
        db.execute('UPDATE agent_tasks SET state=?,reason_code=? WHERE task_id=?',(state,reason,task_id))
    assert not store.can_resume_unstarted_budget(task_id)
    deny_without_mutation(case)


def test_plain_never_claimed_queued_task_is_not_treated_as_budget_resume(case):
    store,binding,env,_=case
    second=store.enqueue(binding['binding_id'],observation(command_id='plain-command',reference_id='plain-reference'))
    before=snapshot(store)
    assert not store.can_resume_unstarted_budget(second['task_id'])
    with pytest.raises(AgentStoreError,match='unstarted_budget_not_safe'):
        store.resume_unstarted_budget(second['task_id'],binding=binding,envelope=second['envelope'])
    assert snapshot(store)==before
