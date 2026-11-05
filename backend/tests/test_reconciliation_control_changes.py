"""Deterministic control changes use fake adapters and task-local state only."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.assistant.choice_controller import ChoiceController
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime, ReconciliationInterrupted
from app.assistant.queue import AssistantQueue
from tests.test_assistant_queue import binding, envelope


def poll_case(change=None, *, boundary='adapter', adapter_failure=False):
    control_state={'run_id':'a'*32,'desired_state':'running'}
    task={'id':'b'*32,'state':'waiting_library','poll_token':'synthetic-poll-token'}
    calls=[]
    def interrupt():
        if change=='run':control_state['run_id']='c'*32
        elif change is not None:control_state['desired_state']=change
    def claim():
        calls.append('claim')
        if boundary=='claim':interrupt()
        return deepcopy(task)
    def reconcile(current):
        calls.append('reconcile')
        if boundary=='adapter':interrupt()
        if adapter_failure:raise ValueError('synthetic stopped observation')
        return {'state':'needs_review','reason_code':'binding_changed'}
    def apply(task_id,result,*,poll_token):
        assert task_id==task['id'] and poll_token==task['poll_token']
        calls.append('apply')
        task.update(result)
        return deepcopy(task)
    queue=SimpleNamespace(claim_waiting=claim,apply_reconciliation=apply)
    control=SimpleNamespace(snapshot=lambda:dict(control_state))
    runtime=AssistantRuntime(queue,control,reconcile=reconcile,run_id='a'*32)
    return SimpleNamespace(runtime=runtime,control=control_state,task=task,calls=calls)


@pytest.mark.parametrize('change',['stopped','paused','run'])
@pytest.mark.parametrize('boundary',['claim','adapter'])
def test_control_change_during_waiting_poll_cannot_apply_binding_failure(change,boundary):
    case=poll_case(change,boundary=boundary)
    before=deepcopy(case.task)
    assert case.runtime.poll_waiting_once() is None
    assert case.task==before and 'apply' not in case.calls
    assert case.calls==(['claim'] if boundary=='claim' else ['claim','reconcile'])


@pytest.mark.parametrize('change',['stopped','paused','run'])
def test_adapter_exception_after_control_change_preserves_library_wait(change):
    case=poll_case(change,adapter_failure=True)
    before=deepcopy(case.task)
    assert case.runtime.poll_waiting_once() is None
    assert case.task==before and case.calls==['claim','reconcile']


def test_authorization_failure_while_same_run_is_running_still_applies_review():
    case=poll_case()
    result=case.runtime.poll_waiting_once()
    assert case.calls==['claim','reconcile','apply']
    assert result['state']=='needs_review' and result['reason_code']=='binding_changed'


def test_ordinary_query_failure_while_running_retains_existing_fallback():
    case=poll_case(adapter_failure=True)
    result=case.runtime.poll_waiting_once()
    assert result['state']=='waiting_ingestion'
    assert result['reason_code']=='ingestion_status_unavailable'
    assert case.calls==['claim','reconcile','apply']


def test_stopped_runtime_does_not_claim_a_waiting_task():
    case=poll_case()
    case.control['desired_state']='stopped'
    assert case.runtime.poll_waiting_once() is None and case.calls==[]


def snapshot_case():
    stopped=[False]
    bridge=LegacyBridge.__new__(LegacyBridge)
    bridge.should_stop=lambda:stopped[0]
    bridge._binding=lambda task:SimpleNamespace(public_reply_allowed=True)
    bridge._find=lambda task:None
    bridge._semantic_steps=lambda task:[]
    bridge.choices=None
    return bridge,stopped


def test_stopped_authorization_is_not_projected_as_a_binding_change():
    bridge,stopped=snapshot_case()
    def authorized(task):
        stopped[0]=True
        return False
    bridge._authorized=authorized
    with pytest.raises(ReconciliationInterrupted,match='reconciliation_control_changed'):
        bridge.reconcile({'id':'b'*32})


def test_stop_during_choice_decoration_cannot_return_a_persistable_snapshot():
    bridge,stopped=snapshot_case()
    bridge._authorized=lambda task:True
    def decorate(task,result):
        stopped[0]=True
        return result
    bridge.choices=SimpleNamespace(decorate_result=decorate)
    with pytest.raises(ReconciliationInterrupted,match='reconciliation_control_changed'):
        bridge.reconcile({'id':'b'*32})


def test_actual_binding_failure_still_projects_needs_review():
    bridge,_=snapshot_case()
    bridge._authorized=lambda task:False
    result=bridge.reconcile({'id':'b'*32})
    assert result['state']=='needs_review' and result['reason_code']=='binding_changed'
    assert result['binding_active'] is False and result['safe_to_execute'] is False


@pytest.mark.parametrize('boundary',['before','authorization','decoration'])
def test_page_hold_while_control_running_never_projects_an_interrupted_snapshot(boundary):
    case=poll_case()
    before=deepcopy(case.task)
    bridge,stopped=snapshot_case()
    stopped[0]=boundary=='before'
    def authorized(task):
        if boundary=='authorization':stopped[0]=True
        return False
    def decorate(task,result):
        if boundary=='decoration':stopped[0]=True
        return result
    bridge._authorized=authorized
    bridge.choices=SimpleNamespace(decorate_result=decorate)
    case.runtime.reconcile_adapter=bridge.reconcile
    assert case.runtime.poll_waiting_once() is None
    assert case.control['desired_state']=='running' and case.task==before
    assert case.calls==['claim']


def choice_case():
    stopped=[False]
    calls=[]
    session={'choice_id':'choice','state':'waiting_library','options':[],
        'selected_kb_id':None,'child_task_id':None}
    def hold(choice_id,reason):
        calls.append(('hold',reason))
        session['state']='needs_review'
    controller=ChoiceController.__new__(ChoiceController)
    controller.should_stop=lambda:stopped[0]
    controller.store=SimpleNamespace(get_for_root_task=lambda task_id:deepcopy(session),
        get=lambda choice_id:deepcopy(session),hold_waiting=hold)
    controller.after_publish=lambda task,result:None
    controller.last_issue_code=None
    return controller,stopped,session,calls


def test_stopped_choice_cannot_durably_hold_a_waiting_session():
    controller,stopped,session,calls=choice_case()
    stopped[0]=True
    controller._hold(session)
    assert session['state']=='waiting_library' and calls==[] and controller.last_issue_code is None


def test_stop_during_fresh_root_validation_never_becomes_a_choice_hold():
    controller,stopped,session,calls=choice_case()
    def fresh_root(current):
        stopped[0]=True
        return None
    controller._fresh_root=fresh_root
    result=controller.decorate_result({'id':'b'*32},{'state':'waiting_library'})
    assert session['state']=='waiting_library' and calls==[]
    assert result['state']=='waiting_library'


def test_invalid_choice_scope_while_running_keeps_original_hold_behavior():
    controller,_,session,calls=choice_case()
    controller._fresh_root=lambda current:None
    result=controller.decorate_result({'id':'b'*32},{'state':'waiting_library'})
    assert session['state']=='needs_review' and calls==[('hold','binding_changed')]
    assert result['state']=='needs_review' and result['reason_code']=='binding_changed'
    assert result['safe_to_execute'] is result['safe_to_publish'] is False


@pytest.mark.parametrize('interruption',['stopped','page_hold'])
def test_real_queue_preserves_wait_during_stop_and_explicit_reconcile_can_repair_old_projection(tmp_path,interruption):
    queue=AssistantQueue(tmp_path/'queue.sqlite3')
    task=queue.enqueue(binding(),envelope(),kind='clarify')
    waiting={'state':'waiting_library','reason_code':None,'reply_state':'b_view_visible',
        'send_token_present':True,'safe_to_execute':False,'safe_to_publish':False,
        'binding_active':True,'public_reply_allowed':True,
        'details':{'library_choice':{'choice_id':'d'*64,'state':'waiting_library'}}}
    queue.apply_reconciliation(task['id'],waiting)
    before=queue.get_task(task['id'])
    events=queue.list_events(task['id'])
    state={'run_id':'a'*32,'desired_state':'running'}
    stopped_result={**waiting,'reason_code':'binding_changed',
        'binding_active':False,'public_reply_allowed':False}
    def stopped_observation(current):
        if interruption=='page_hold':raise ReconciliationInterrupted('reconciliation_control_changed')
        state['desired_state']='stopped'
        return deepcopy(stopped_result)
    def forbidden(*args):pytest.fail('reconciliation invoked a paid execution or publication')
    runtime=AssistantRuntime(queue,SimpleNamespace(snapshot=lambda:dict(state)),run_id='a'*32,
        reconcile=stopped_observation,execute=forbidden,publish=forbidden)
    assert runtime.poll_waiting_once() is None
    after=queue.get_task(task['id'])
    assert after['state']==before['state']=='waiting_library'
    assert after['reason_code'] is None and queue.list_events(task['id'])==events
    if interruption=='page_hold':assert state['desired_state']=='running'

    # A prior build's bad projection is repaired through the normal trusted
    # reconciliation entry point. It never needs a model retry or send reset.
    queue.apply_reconciliation(task['id'],stopped_result)
    assert queue.get_task(task['id'])['state']=='needs_review'
    state['desired_state']='paused'
    runtime.reconcile_adapter=lambda current:deepcopy(waiting)
    repaired=runtime.reconcile_task(task['id'])
    assert repaired['state']=='waiting_library' and repaired['reason_code'] is None
    assert repaired['execution_attempts']==before['execution_attempts']==0
    assert repaired['publish_attempts']==before['publish_attempts']==0
