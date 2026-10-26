"""Explicit checks use real temporary ledgers and fake read-only page IO."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.queue import QueueError
from app.assistant.runtime import AssistantRuntime
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_reply_readback_ledger import setup_unknown, setup_queue, rows


def setup(tmp_path,monkeypatch,*,observation_error=None):
    case,env,agent,reply,observation=setup_unknown(tmp_path)
    queue,task=setup_queue(tmp_path,case,env,reply)
    bridge=LegacyBridge.__new__(LegacyBridge)
    bridge.agent,bridge.queue=agent,queue
    bridge.adapter=SimpleNamespace(check_intervention=lambda:None)
    bridge.browser_owner_thread_id=threading.get_ident()
    bridge.should_stop=lambda:False
    bridge.control=SimpleNamespace(snapshot=queue.get_control)
    bridge.admission_lock=threading.RLock()
    bridge._find=lambda task:agent.get_task(reply['task_id'])
    bridge._authorized=lambda task:True
    calls=[]
    def observe(adapter,target,text):
        calls.append((target,text))
        if observation_error is not None:raise observation_error
        return observation
    monkeypatch.setattr('app.mention_receiver.choice_browser.observe_existing_choice_reply',observe)
    def forbidden(*args):pytest.fail('explicit readback entered model/import/publish')
    runtime=AssistantRuntime(queue,bridge.control,run_id='owner',execute=forbidden,publish=forbidden,
        reconcile=lambda task:{'state':'delivery_unknown'},
        operator_readback_adapter=bridge.observe_existing_delivery)
    return queue,task,agent,reply,runtime,calls


def test_explicit_check_after_automatic_budget_preserves_original_barriers(tmp_path,monkeypatch):
    queue,task,agent,reply,runtime,calls=setup(tmp_path,monkeypatch)
    before=rows(agent.path,('agent_tasks','agent_replies'))
    real_time=time.time
    clock=real_time()
    monkeypatch.setattr('app.assistant.queue.time.time',lambda:clock)
    for attempt in range(1,4):
        claim=queue.claim_delivery_readback('owner')
        assert claim['_delivery_readback_attempt']==attempt
        queue.finish_delivery_readback(task['id'],claim['_delivery_readback_claim_token'],reason_code='readback_not_visible')
        clock+=121
    assert queue.claim_delivery_readback('owner') is None
    # The UI proof is obtained now, rather than using a future simulated
    # observation time. Durable attempts remain exhausted in the database.
    monkeypatch.setattr('app.assistant.queue.time.time',real_time)
    # Internal projection/retry calls do not acquire a browser observation.
    runtime.reconcile_task(task['id'])
    assert calls==[]
    queue.request_action(task['id'],'reconcile')
    outcome=runtime.process_action_once()
    assert outcome['state']=='done'
    assert queue.get_task(task['id'])['state']=='b_view_visible' and len(calls)==1
    assert rows(agent.path,('agent_tasks','agent_replies'))==before
    current=queue.get_task(task['id'])
    assert current['execution_attempts']==current['publish_attempts']==1
    assert current['publication_token']==task['publication_token']
    queue.request_action(task['id'],'reconcile')
    runtime.process_action_once()
    assert len(calls)==1
    assert queue.claim_next('execute') is None and queue.claim_next('publish') is None


def test_paused_explicit_check_cannot_touch_page(tmp_path,monkeypatch):
    queue,task,agent,reply,runtime,calls=setup(tmp_path,monkeypatch)
    queue.update_control(lambda old:{**old,'state':'paused','desired_state':'paused'})
    queue.request_action(task['id'],'reconcile')
    result=runtime.process_action_once()
    assert result['state']=='failed' and result['reason_code']=='readback_requires_running'
    assert calls==[] and agent.get_reply_readback(reply['task_id']) is None


def test_human_challenge_finishes_action_and_reaches_owner_loop(tmp_path,monkeypatch):
    queue,task,agent,reply,runtime,calls=setup(tmp_path,monkeypatch,
        observation_error=ReceiverInterventionRequired('human_intervention_required'))
    queue.request_action(task['id'],'reconcile')
    with pytest.raises(ReceiverInterventionRequired):runtime.process_action_once()
    assert queue.take_action() is None and len(calls)==1
    assert queue.get_task(task['id'])['state']=='delivery_unknown'
    assert agent.get_reply_readback(reply['task_id']) is None


def test_wrong_thread_cannot_start_explicit_browser_readback(tmp_path,monkeypatch):
    queue,task,agent,reply,runtime,calls=setup(tmp_path,monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(QueueError,match='wrong_thread'):
            pool.submit(runtime.reconcile_task,task['id'],operator_readback=True).result()
    assert calls==[]


def test_ledger_failure_does_not_hide_live_human_challenge(tmp_path,monkeypatch):
    queue,task,agent,reply,runtime,calls=setup(tmp_path,monkeypatch,
        observation_error=ReceiverInterventionRequired('human_intervention_required'))
    queue.request_action(task['id'],'reconcile')
    def unavailable(*args):raise RuntimeError('synthetic ledger write failure')
    monkeypatch.setattr(queue,'finish_action',unavailable)
    with pytest.raises(ReceiverInterventionRequired):runtime.process_action_once()
    assert len(calls)==1 and queue.get_task(task['id'])['state']=='delivery_unknown'


def test_plain_mapping_cannot_confirm_reply(tmp_path,monkeypatch):
    queue,task,agent,reply,runtime,calls=setup(tmp_path,monkeypatch)
    runtime.operator_readback_adapter=lambda task:{'scope':'b_view_only','state':'b_view_visible'}
    queue.request_action(task['id'],'reconcile')
    assert runtime.process_action_once()['state']=='failed'
    assert queue.get_task(task['id'])['state']=='delivery_unknown' and calls==[]
