"""Synthetic public-wire and reopen contracts, independent of private history.

These tests exercise the current published Queue against fictional temporary
Agent records. They do not load or attest a historical running main process.
The original historical loader/tests/failures remain in local private evidence.
"""
import json

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.queue import AssistantQueue, normalized_result
from tests.test_original_target_unsent_recovery import held_case
from tests.test_reply_readback_ledger import setup_unknown, setup_queue


def test_synthetic_unsent_recovery_wire_reopens_without_execute_replay(tmp_path):
    agent,queue,model,adapter,bridge,runtime,ready=held_case(tmp_path)
    result=bridge.reconcile(queue.get_task(ready['id']))
    # Serialize the actual Bridge result through the public JSON boundary.
    normalized_result(json.loads(json.dumps(result,ensure_ascii=False,allow_nan=False)))
    runtime.reconcile_task(ready['id'])
    reopened=AssistantQueue(queue.path)
    value=reopened.public_task(reopened.get_task(ready['id']),detail=True)
    assert value['reason_code']=='preflight_failed' and value['actions']['can_resume'] is True
    assert value['details']['steps'][-1]['metadata']['code']=='original_notification_not_unique'
    assert queue.claim_next('execute') is None
    assert reopened.claim_next('execute') is None
    assert len(model.requests)==2 and adapter.submits==0


def test_synthetic_confirmed_projection_wire_reopens_without_new_send_action(tmp_path):
    case,env,agent,reply,observation=setup_unknown(tmp_path,weak=True)
    queue,task=setup_queue(tmp_path,case,env,reply)
    credential=agent.record_reply_readback(reply['task_id'],observation)
    queue.project_recorded_delivery_readback(task['id'],credential,run_id='owner')
    reopened=AssistantQueue(queue.path)
    value=reopened.public_task(reopened.get_task(task['id']),detail=True)
    assert value['state']==value['reply_state']=='b_view_visible'
    assert value['actions']['can_resume'] is False and value['actions']['can_retry_query'] is False
    assert agent.get_reply(reply['task_id'])['state']=='delivery_unknown'
    assert reopened.claim_next('execute') is None and reopened.claim_next('publish') is None
    public=json.dumps(value,ensure_ascii=False)
    assert '_delivery_readback_claim_token' not in public and 'send_token' not in public
    # Projection retains the private original step while its public view uses
    # the documented decision/metadata shape, without exposing send credentials.
    detail=LegacyBridge._details(agent.get_reply(reply['task_id']),agent.list_steps(reply['task_id']),'编程库')
    assert detail['steps'][-1]['kind']=='decision'
    assert detail['steps'][-1]['metadata']['kind']=='reply_readback_confirmation'
    normalized_result(json.loads(json.dumps({'state':'b_view_visible','details':detail},ensure_ascii=False)))
