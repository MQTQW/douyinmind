"""Read-only delivery facts: only temporary ledgers and fictional identities."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import sqlite3
import time

import pytest

from app.assistant.queue import AssistantQueue, QueueError
from app.mention_agent.store import AgentStore, AgentStoreError, verified_reply_readback_metadata
from tests.test_assistant_choice_agent_scope import choice_ready


def stamp():return datetime.now(timezone.utc).isoformat()


def rows(path, names):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        return {name:db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() for name in names}


def setup_unknown(tmp_path, *, weak=False, gate=True):
    case,env,store,reply,verification=choice_ready(tmp_path,parent_scope=(
        'unique_pending_choice_in_verified_root' if weak else 'verified_ask_parent'))
    reserved=store.reserve_send(reply['task_id'],verification)
    store.finish_send(reply['task_id'],reserved['send_token'],'delivery_unknown',{
        'adapter_outcome':'delivery_unknown','submit_gate_passed':gate,
        'exact_body_in_original_thread':False,'reply_sha256':hashlib.sha256(reply['text'].encode()).hexdigest(),
        'observed_at_utc':stamp()})
    observation={'outcome':'b_view_visible','exact_body_in_original_thread':True,
        'verification':{**verification,'observed_at_utc':stamp()},
        'reply_sha256':hashlib.sha256(reply['text'].encode()).hexdigest(),'observed_at_utc':stamp(),
        'readback_quality':'weak_unique_root' if weak else 'direct_selected_parent',
        'counts':{'matching_reply_count':1,'existing_matching_reply_count':1,
            'strict_matching_reply_count':0 if weak else 1,'weak_matching_reply_count':1 if weak else 0,
            'verified_root_count':1,'verified_selection_count':1,'selected_source_count':1}}
    return case,env,store,reply,observation


def setup_queue(tmp_path,case,env,reply):
    queue=AssistantQueue(tmp_path/'queue.sqlite3')
    task=queue.enqueue(case.selected_bindings[7],env,kind='save')
    claim=queue.claim_next(run_id='owner')
    queue.finish(task['id'],claim['execution_token'],'execute',{'state':'answer_ready',
        'agent_task_id':reply['task_id'],'reply_state':'ready','details':{'reply_text':reply['text']}})
    claim=queue.claim_next('publish',run_id='owner')
    task=queue.finish(task['id'],claim['publication_token'],'publish',{'state':'delivery_unknown',
        'agent_task_id':reply['task_id'],'reply_state':'delivery_unknown',
        'details':{'reply_text':reply['text']}})
    queue.update_control(lambda old:{**old,'state':'running','desired_state':'running','run_id':'owner'})
    return queue,task


@pytest.mark.parametrize('weak',[False,True])
def test_append_and_reopen_preserves_original_unknown_bytes(tmp_path,weak):
    _,_,store,reply,observation=setup_unknown(tmp_path,weak=weak)
    before=rows(store.path,('agent_tasks','agent_replies'))
    credential=store.record_reply_readback(reply['task_id'],observation)
    assert credential['scope']=='b_view_only' and credential['readback_quality']==observation['readback_quality']
    assert rows(store.path,('agent_tasks','agent_replies'))==before
    again=AgentStore(store.path).get_reply_readback(reply['task_id'])
    assert credential==again and verified_reply_readback_metadata(again)==dict(credential)
    store.record_reply_readback(reply['task_id'],{**observation,'observed_at_utc':stamp()})
    assert len([s for s in store.list_steps(reply['task_id']) if s['kind']=='reply_readback_confirmation'])==1
    assert store.reserve_send(reply['task_id'],observation['verification']) is None


def test_queue_projection_preserves_original_send_barriers_and_is_idempotent(tmp_path):
    case,env,store,reply,observation=setup_unknown(tmp_path)
    queue,task=setup_queue(tmp_path,case,env,reply)
    agent_before=rows(store.path,('agent_tasks','agent_replies'))
    claim=queue.claim_delivery_readback('owner')
    assert claim['id']==task['id'] and claim['_delivery_readback_attempt']==1
    credential=store.record_reply_readback(reply['task_id'],observation)
    done=queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],credential)
    assert done['state']==done['reply_state']=='b_view_visible'
    for key in ('details','usage','execution_token','publication_token','execution_attempts','publish_attempts','reason_code','immutable'):
        assert done[key]==task[key]
    assert rows(store.path,('agent_tasks','agent_replies'))==agent_before
    assert queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],credential)==done
    assert queue.claim_next('publish') is None and queue.claim_next() is None


@pytest.mark.parametrize('field,value',[
    ('outcome','delivery_unknown'),('exact_body_in_original_thread',False),('readback_quality','unconfirmed'),
    ('reply_sha256','0'*64),('observed_at_utc','2020-01-01T00:00:00+00:00'),
])
def test_incomplete_or_wrong_body_observation_never_confirms(tmp_path,field,value):
    _,_,store,reply,obs=setup_unknown(tmp_path)
    obs[field]=value
    before=rows(store.path,('agent_tasks','agent_replies','agent_steps'))
    with pytest.raises(AgentStoreError):store.record_reply_readback(reply['task_id'],obs)
    assert rows(store.path,('agent_tasks','agent_replies','agent_steps'))==before


@pytest.mark.parametrize('field,value',[
    ('root_reference_id','different-root'),('ask_reply_ref','different-ask'),('current_receiver_verified',False),
    ('sender_profile_url','https://www.douyin.com/user/other-A'),('receiver_profile_url','https://www.douyin.com/user/other-B'),
    ('comment_text','another selection'),('video_id','7999999000000000999'),('unique_count',2),
    ('observed_at_utc','2020-01-01T00:00:00+00:00'),
])
def test_fresh_verification_is_bound_to_original_selection(tmp_path,field,value):
    _,_,store,reply,obs=setup_unknown(tmp_path)
    obs['verification'][field]=value
    with pytest.raises(AgentStoreError):store.record_reply_readback(reply['task_id'],obs)
    assert store.get_reply_readback(reply['task_id']) is None


@pytest.mark.parametrize('field,value',[
    ('matching_reply_count',2),('existing_matching_reply_count',0),('verified_selection_count',True),
    ('verified_root_count',2),('strict_matching_reply_count',1),('selected_source_count',2),
])
def test_weak_confirmation_requires_one_unambiguous_source(tmp_path,field,value):
    _,_,store,reply,obs=setup_unknown(tmp_path,weak=True)
    obs['counts'][field]=value
    with pytest.raises(AgentStoreError):store.record_reply_readback(reply['task_id'],obs)


def test_no_original_submission_gate_cannot_be_confirmed(tmp_path):
    _,_,store,reply,obs=setup_unknown(tmp_path,gate=False)
    with pytest.raises(AgentStoreError):store.record_reply_readback(reply['task_id'],obs)


def test_readback_kind_cannot_be_appended_through_generic_step_api(tmp_path):
    _,_,store,reply,obs=setup_unknown(tmp_path)
    with pytest.raises(AgentStoreError):store.record_step(reply['task_id'],'not-a-claim','reply_readback_confirmation',obs)


def test_plain_or_mutated_credential_cannot_change_queue(tmp_path):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    claim=queue.claim_delivery_readback('owner');cert=store.record_reply_readback(reply['task_id'],obs)
    for fake in (dict(cert),{**cert,'scope':'a_view_visible'}):
        with pytest.raises(QueueError,match='unverified'):queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],fake)
    cert['scope']='a_view_visible'
    with pytest.raises(QueueError,match='unverified'):queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],cert)
    assert queue.get_task(task['id'])['state']=='delivery_unknown'


def test_failures_are_delayed_and_capped_without_send_attempts(tmp_path,monkeypatch):
    case,env,store,reply,_=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    for attempt in range(1,4):
        claim=queue.claim_delivery_readback('owner');assert claim['_delivery_readback_attempt']==attempt
        queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],reason_code='readback_not_visible')
        assert queue.claim_delivery_readback('owner') is None
        clock+=121
    assert queue.claim_delivery_readback('owner') is None
    after=queue.get_task(task['id'])
    assert after['state']=='delivery_unknown' and after['execution_attempts']==after['publish_attempts']==1
    assert after['details']==task['details'] and after['publication_token']==task['publication_token']


def test_crash_after_store_append_recovers_fact_without_new_send(tmp_path,monkeypatch):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    first=queue.claim_delivery_readback('owner',lease_seconds=10)
    cert=store.record_reply_readback(reply['task_id'],obs)
    clock+=121
    restarted=AssistantQueue(queue.path);second=restarted.claim_delivery_readback('owner')
    with pytest.raises(QueueError,match='stale'):restarted.finish_delivery_readback(task['id'],first['_delivery_readback_claim_token'],cert)
    recovered=AgentStore(store.path).get_reply_readback(reply['task_id'])
    assert recovered['observed_at_utc']==obs['observed_at_utc']
    done=restarted.finish_delivery_readback(task['id'],second['_delivery_readback_claim_token'],recovered)
    assert done['state']=='b_view_visible' and done['publish_attempts']==1


def test_concurrent_claims_have_only_one_owner(tmp_path):
    case,env,store,reply,_=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda _:AssistantQueue(queue.path).claim_delivery_readback('owner'),range(2)))
    assert sum(claim is not None for claim in claims)==1


def test_paused_or_replaced_owner_cannot_project_success(tmp_path):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    claim=queue.claim_delivery_readback('owner');cert=store.record_reply_readback(reply['task_id'],obs)
    queue.update_control(lambda old:{**old,'state':'paused'})
    with pytest.raises(QueueError,match='control_paused'):queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],cert)
    queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],reason_code='readback_control_paused')
    assert queue.get_task(task['id'])['state']=='delivery_unknown'


@pytest.mark.parametrize('field',['proposal','target','evidence'])
def test_persisted_confirmation_rejects_original_row_changes(tmp_path,field):
    _,_,store,reply,obs=setup_unknown(tmp_path);store.record_reply_readback(reply['task_id'],obs)
    with sqlite3.connect(store.path) as db:
        value=json.loads(db.execute('SELECT '+field+' FROM agent_replies WHERE task_id=?',(reply['task_id'],)).fetchone()[0])
        value['unexpected_public_field']='changed'
        db.execute('UPDATE agent_replies SET '+field+'=? WHERE task_id=?',(json.dumps(value),reply['task_id']))
    with pytest.raises(AgentStoreError):store.get_reply_readback(reply['task_id'])


def test_notification_not_unique_hold_requires_fresh_target_and_one_release(tmp_path):
    _,_,store,reply,verification=choice_ready(tmp_path)
    store.hold_reply(reply['task_id'],'original_notification_not_unique')
    for proof in (None,{**verification,'video_id':'7999999000000000999'},
                  {**verification,'observed_at_utc':'2020-01-01T00:00:00+00:00'}):
        with pytest.raises(AgentStoreError):store.release_held_reply(reply['task_id'],'operator_verified',verification=proof)
    released=store.release_held_reply(reply['task_id'],'operator_verified',verification=verification)
    assert released['state']=='ready' and released['send_token'] is None
    assert len(store.list_reply_hold_releases(reply['task_id']))==1
    store.hold_reply(reply['task_id'],'original_notification_not_unique')
    with pytest.raises(AgentStoreError,match='limit'):store.release_held_reply(reply['task_id'],'operator_verified',verification=verification)


def test_concurrent_records_append_exactly_one_fact(tmp_path):
    _,_,store,reply,obs=setup_unknown(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values=list(pool.map(lambda _:AgentStore(store.path).record_reply_readback(reply['task_id'],obs),range(2)))
    assert values[0]==values[1]
    assert len([s for s in store.list_steps(reply['task_id']) if s['kind']=='reply_readback_confirmation'])==1


def test_generic_reconciliation_cannot_promote_original_unknown(tmp_path):
    case,env,store,reply,_=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    before=queue.get_task(task['id'])
    assert queue.apply_reconciliation(task['id'],{'state':'b_view_visible','reply_state':'b_view_visible'})==before
    with pytest.raises(QueueError):queue.resume(task['id'])
    assert queue.get_task(task['id'])['publication_token']==task['publication_token']


def test_lease_expiry_and_new_work_priority_preserve_unknown(tmp_path,monkeypatch):
    case,env,store,reply,_=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    from tests.test_assistant_queue import binding,envelope
    new=queue.enqueue(binding(),envelope(),kind='query')
    assert queue.claim_delivery_readback('owner') is None
    with sqlite3.connect(queue.path) as db:db.execute("UPDATE assistant_tasks SET state='cancelled' WHERE id=?",(new['id'],))
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    claim=queue.claim_delivery_readback('owner',lease_seconds=1);clock+=2
    with pytest.raises(QueueError,match='stale'):queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],reason_code='readback_unconfirmed')
    assert queue.get_task(task['id'])['state']=='delivery_unknown'


def test_original_binding_and_body_cannot_change_during_claim(tmp_path):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    claim=queue.claim_delivery_readback('owner');cert=store.record_reply_readback(reply['task_id'],obs)
    with sqlite3.connect(queue.path) as db:
        db.execute('UPDATE assistant_tasks SET details=? WHERE id=?',(json.dumps({'reply_text':'changed'}),task['id']))
    with pytest.raises(QueueError,match='scope_changed'):queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],cert)


def test_valid_credential_for_other_agent_cannot_project_queue(tmp_path):
    one=tmp_path/'one';one.mkdir();two=tmp_path/'two';two.mkdir()
    case,env,store,reply,_=setup_unknown(one);queue,task=setup_queue(one,case,env,reply)
    _,_,other,other_reply,other_obs=setup_unknown(two)
    # A distinct Agent task is a valid local fact but not this product receipt.
    with sqlite3.connect(other.path) as db:
        replacement='f'*32
        db.execute('UPDATE agent_tasks SET task_id=? WHERE task_id=?',(replacement,other_reply['task_id']))
        db.execute('UPDATE agent_replies SET task_id=? WHERE task_id=?',(replacement,other_reply['task_id']))
    cert=other.record_reply_readback(replacement,other_obs);claim=queue.claim_delivery_readback('owner')
    with pytest.raises(QueueError,match='mismatch'):queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],cert)


def test_confirmation_adds_no_schema_and_no_private_capabilities(tmp_path):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    def schema(path):
        with sqlite3.connect(path) as db:return db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    before=(schema(store.path),schema(queue.path));claim=queue.claim_delivery_readback('owner')
    cert=store.record_reply_readback(reply['task_id'],obs)
    queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],cert)
    assert before==(schema(store.path),schema(queue.path))
    public=json.dumps(cert)
    assert not any(term in public for term in ('send_token','claim_token','authorization','cookie','password'))


def test_legacy_ordinary_held_reply_keeps_true_mention_gate(tmp_path):
    from tests.test_mention_agent_store import binding,envelope,proposal,verification
    store=AgentStore(tmp_path/'normal.sqlite3');store.put_binding(binding())
    task=store.enqueue('fixture-binding',envelope());claim=store.claim(task['task_id'])
    reply=store.complete(task['task_id'],claim['claim_token'],proposal())
    store.hold_reply(task['task_id'],'original_notification_not_unique')
    with pytest.raises(AgentStoreError):store.release_held_reply(task['task_id'],'operator_verified',verification=verification(real_mention=False))
    assert store.release_held_reply(task['task_id'],'operator_verified',verification=verification())['state']=='ready'
    reserved=store.reserve_send(task['task_id'],verification())
    assert reserved is not None
    with pytest.raises(AgentStoreError):store.release_held_reply(task['task_id'],'operator_verified',verification=verification())


def test_third_claim_crash_keeps_proof_but_does_not_exceed_observation_cap(tmp_path,monkeypatch):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    for _ in range(2):
        claim=queue.claim_delivery_readback('owner')
        queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],reason_code='readback_not_visible')
        clock+=121
    third=queue.claim_delivery_readback('owner',lease_seconds=1)
    assert third['_delivery_readback_attempt']==3
    obs['observed_at_utc']=stamp();obs['verification']['observed_at_utc']=stamp()
    # time.time is patched on the shared Python module; explicitly use that
    # same synthetic instant for the fresh observation before this fake crash.
    obs['observed_at_utc']=obs['verification']['observed_at_utc']=datetime.fromtimestamp(clock,timezone.utc).isoformat()
    store.record_reply_readback(reply['task_id'],obs)
    clock+=121
    assert AssistantQueue(queue.path).claim_delivery_readback('owner') is None
    assert AgentStore(store.path).get_reply_readback(reply['task_id']) is not None
    assert queue.get_task(task['id'])['state']=='delivery_unknown'


def test_third_claim_crash_has_local_only_recovery_without_fourth_observation(tmp_path,monkeypatch):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    for _ in range(2):
        claim=queue.claim_delivery_readback('owner')
        queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],reason_code='readback_not_visible')
        clock+=121
    third=queue.claim_delivery_readback('owner',lease_seconds=1)
    obs['observed_at_utc']=obs['verification']['observed_at_utc']=datetime.fromtimestamp(clock,timezone.utc).isoformat()
    store.record_reply_readback(reply['task_id'],obs)
    agent_before=rows(store.path,('agent_tasks','agent_replies','agent_steps'))
    clock+=121;restarted=AssistantQueue(queue.path)
    assert restarted.claim_delivery_readback('owner') is None
    candidate=restarted.list_delivery_readback_candidates()[0]
    assert candidate['id']==task['id']
    credential=AgentStore(store.path).get_reply_readback(candidate['agent_task_id'])
    done=restarted.project_recorded_delivery_readback(task['id'],credential,run_id='owner')
    assert done['state']=='b_view_visible'
    assert restarted.project_recorded_delivery_readback(task['id'],credential,run_id='owner')==done
    assert restarted.finish_delivery_readback(task['id'],third['_delivery_readback_claim_token'],credential)==done
    assert rows(store.path,('agent_tasks','agent_replies','agent_steps'))==agent_before
    with sqlite3.connect(queue.path) as db:
        assert db.execute("SELECT COUNT(*) FROM assistant_events WHERE kind='delivery_readback_claimed'").fetchone()[0]==3
        assert db.execute("SELECT COUNT(*) FROM assistant_events WHERE kind='delivery_readback_recovered'").fetchone()[0]==1
    for key in ('details','usage','execution_token','publication_token','execution_attempts','publish_attempts','immutable'):
        assert done[key]==task[key]


def test_local_recovery_rejects_active_observation_and_then_allows_expired_lease(tmp_path,monkeypatch):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    clock=time.time();monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    queue.claim_delivery_readback('owner',lease_seconds=1);cert=store.record_reply_readback(reply['task_id'],obs)
    assert queue.list_delivery_readback_candidates()==[]
    with pytest.raises(QueueError,match='busy'):queue.project_recorded_delivery_readback(task['id'],cert,run_id='owner')
    clock+=2
    assert queue.list_delivery_readback_candidates()[0]['id']==task['id']
    assert queue.project_recorded_delivery_readback(task['id'],cert,run_id='owner')['state']=='b_view_visible'


@pytest.mark.parametrize('change',['body','reference','agent','cancelled'])
def test_local_projection_rejects_changed_scope_or_cancelled_task(tmp_path,change):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    cert=store.record_reply_readback(reply['task_id'],obs)
    with sqlite3.connect(queue.path) as db:
        if change=='body':db.execute('UPDATE assistant_tasks SET details=? WHERE id=?',(json.dumps({'reply_text':'changed'}),task['id']))
        elif change=='agent':db.execute('UPDATE assistant_tasks SET agent_task_id=? WHERE id=?',('f'*32,task['id']))
        elif change=='cancelled':db.execute("UPDATE assistant_tasks SET state='cancelled' WHERE id=?",(task['id'],))
        else:
            changed=deepcopy(env);changed['reference_id']='different-ref'
            db.execute('UPDATE assistant_tasks SET envelope=? WHERE id=?',(json.dumps(changed),task['id']))
    before=rows(queue.path,('assistant_tasks','assistant_events'))
    with pytest.raises(QueueError):queue.project_recorded_delivery_readback(task['id'],cert,run_id='owner')
    assert rows(queue.path,('assistant_tasks','assistant_events'))==before


def test_local_projection_rejects_plain_credential_and_wrong_running_owner(tmp_path):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    cert=store.record_reply_readback(reply['task_id'],obs)
    with pytest.raises(QueueError,match='unverified'):queue.project_recorded_delivery_readback(task['id'],dict(cert),run_id='owner')
    with pytest.raises(QueueError,match='paused'):queue.project_recorded_delivery_readback(task['id'],cert,run_id='other-owner')
    queue.update_control(lambda old:{**old,'state':'paused'})
    with pytest.raises(QueueError,match='paused'):queue.project_recorded_delivery_readback(task['id'],cert,run_id='owner')
    assert queue.get_task(task['id'])['state']=='delivery_unknown'


def test_concurrent_local_projection_is_once_and_candidate_reads_are_pure(tmp_path):
    case,env,store,reply,obs=setup_unknown(tmp_path);queue,task=setup_queue(tmp_path,case,env,reply)
    cert=store.record_reply_readback(reply['task_id'],obs)
    before=hashlib.sha256(queue.path.read_bytes()).hexdigest()
    assert queue.list_delivery_readback_candidates()[0]['id']==task['id']
    assert hashlib.sha256(queue.path.read_bytes()).hexdigest()==before
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:AssistantQueue(queue.path).project_recorded_delivery_readback(task['id'],cert,run_id='owner'),range(2)))
    assert results[0]==results[1] and results[0]['state']=='b_view_visible'
    with sqlite3.connect(queue.path) as db:
        assert db.execute("SELECT COUNT(*) FROM assistant_events WHERE kind='delivery_readback_recovered'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM assistant_events WHERE kind='delivery_readback_claimed'").fetchone()[0]==0
    assert queue.list_delivery_readback_candidates()==[]


@pytest.mark.parametrize('key',['send_token','claim_token','execution_token','publication_token','poll_token','nonce','other_nonce'])
def test_private_capabilities_cannot_be_persisted_in_public_readback_proof(tmp_path,key):
    _,_,store,reply,obs=setup_unknown(tmp_path)
    obs['verification'][key]='fictional-private-marker'
    with pytest.raises(AgentStoreError,match='private_field'):store.record_reply_readback(reply['task_id'],obs)
    assert store.get_reply_readback(reply['task_id']) is None
