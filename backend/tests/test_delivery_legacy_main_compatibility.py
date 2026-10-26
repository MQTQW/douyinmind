"""Attested entry Queue code accepts new-helper public records unchanged.

The reconstructed file hashes to the root entry snapshot. Actual loaded main
process compatibility is additionally checked through its real HTTP GETs.
"""
import hashlib
import json
from pathlib import Path
import sys
import types

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from tests.test_original_target_unsent_recovery import held_case
from tests.test_reply_readback_ledger import setup_unknown, setup_queue


def legacy_queue_module():
    root=Path(__file__).resolve().parents[1]
    phase=root/'app/storage/continuous_testing/2026-10-19/assistant-stability-3h-2026-10-22'
    source=phase/'delivery-ledger/queue.before-readback-reconstructed-verified.py'
    baseline=json.loads((phase/'session-baseline.json').read_text(encoding='utf-8'))
    expected=next(value for key,value in baseline['source_at_snapshot'].items()
        if key.replace('\\','/')=='app/assistant/queue.py')
    assert hashlib.sha256(source.read_bytes()).hexdigest()==expected
    name='app.assistant._attested_entry_queue_compat'
    module=types.ModuleType(name)
    module.__file__=str(source)
    module.__package__='app.assistant'
    sys.modules[name]=module
    exec(compile(source.read_bytes(),str(source),'exec'),module.__dict__)
    return module


def test_normalized_unsent_recovery_uses_only_legacy_wire_and_still_forbids_execute_replay(tmp_path):
    old=legacy_queue_module()
    agent,queue,model,adapter,bridge,runtime,ready=held_case(tmp_path)
    result=bridge.reconcile(queue.get_task(ready['id']))
    old.normalized_result(result)
    runtime.reconcile_task(ready['id'])
    legacy=old.AssistantQueue(queue.path)
    value=legacy.public_task(legacy.get_task(ready['id']),detail=True)
    assert value['reason_code']=='preflight_failed' and value['actions']['can_resume'] is True
    assert value['details']['steps'][-1]['metadata']['code']=='original_notification_not_unique'
    assert queue.claim_next('execute') is None
    assert len(model.requests)==2 and adapter.submits==0


def test_confirmed_projection_and_safe_event_are_readable_by_legacy_get_without_new_send_action(tmp_path):
    old=legacy_queue_module()
    case,env,agent,reply,observation=setup_unknown(tmp_path,weak=True)
    queue,task=setup_queue(tmp_path,case,env,reply)
    credential=agent.record_reply_readback(reply['task_id'],observation)
    queue.project_recorded_delivery_readback(task['id'],credential,run_id='owner')
    legacy=old.AssistantQueue(queue.path)
    value=legacy.public_task(legacy.get_task(task['id']),detail=True)
    assert value['state']==value['reply_state']=='b_view_visible'
    assert value['actions']['can_resume'] is False and value['actions']['can_retry_query'] is False
    assert agent.get_reply(reply['task_id'])['state']=='delivery_unknown'
    assert legacy.claim_next('execute') is None and legacy.claim_next('publish') is None
    public=json.dumps(value,ensure_ascii=False)
    assert '_delivery_readback_claim_token' not in public and 'send_token' not in public
    # Snapshot projection retains the private original step but uses only a
    # pre-existing public decision/metadata shape for the old HTTP service.
    detail=LegacyBridge._details(agent.get_reply(reply['task_id']),agent.list_steps(reply['task_id']),'编程库')
    assert detail['steps'][-1]['kind']=='decision'
    assert detail['steps'][-1]['metadata']['kind']=='reply_readback_confirmation'
    old.normalized_result({'state':'b_view_visible','details':detail})
