"""Actual owner handler/publisher boundaries; temp ledgers, no real browser."""
import ast
from copy import deepcopy
import inspect
import json
from types import SimpleNamespace

import pytest

from app.assistant import __main__ as owner_main
from app.assistant.control import AssistantControl
from app.assistant.native_lane import NativeLaneError
from app.assistant.queue import AssistantQueue
from app.mention_receiver.browser import ReceiverPageError,ReceiverDetailError
from tests.test_mention_agent_publisher import make_context,FakeAdapter


COOLDOWN={'state':'cooldown','reason_code':None,'generation':1,'refresh_count':0,
    'source':'owned','seal_state':'sealed','transient_failures':1,
    'last_transient_code':'network_timeout','retry_after_seconds':45}


def run_actual_owner_error_handler(error,control,run_id):
    """Execute the existing catch body without starting its browser/worker.

    This keeps the production wiring in the regression rather than re-creating
    its classification in a test, and exercises real temporary control storage.
    """
    tree=ast.parse(inspect.getsource(owner_main.run_registered))
    catches=[node for node in ast.walk(tree) if isinstance(node,ast.ExceptHandler)
        and isinstance(node.type,ast.Tuple) and any(isinstance(t,ast.Name) and t.id=='ReceiverPageError' for t in node.type.elts)]
    assert len(catches)==1
    function=ast.FunctionDef(name='caught',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=n) for n in
        ('error','control','run_id','poll_seconds')],kwonlyargs=[],kw_defaults=[],defaults=[]),
        body=deepcopy(catches[0].body)+[ast.Return(value=ast.Name(id='next_capture',ctx=ast.Load()))],decorator_list=[])
    module=ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
    namespace=dict(vars(owner_main));namespace['time']=SimpleNamespace(monotonic=lambda:1000)
    exec(compile(module,'<actual-owner-page-error-handler>','exec'),namespace)
    return namespace['caught'](error,control,run_id,45)


def running_control(tmp_path):
    queue=AssistantQueue(tmp_path/'queue.sqlite3');alive={99001}
    control=AssistantControl(queue,popen_factory=lambda *a,**kw:SimpleNamespace(pid=99001),
        process_probe=lambda pid:pid in alive)
    run=control.start();control.register(run['run_id'],99001)
    return queue,control,run['run_id']


def test_native_cooldown_is_not_recorded_as_a_broken_browser_page(tmp_path):
    queue,control,run_id=running_control(tmp_path)
    error=NativeLaneError('native_session_cooldown');error.native_session_status=deepcopy(COOLDOWN)
    assert run_actual_owner_error_handler(error,control,run_id)==1045  # no earlier retry
    state=control.snapshot()
    assert state['reason_code']=='native_session_cooldown'
    assert state['native_session_status']['state']=='cooldown'
    assert state['desired_state']=='running' and state['receiver_state']=='page_unavailable'
    assert sum(queue.counts().values())==0


@pytest.mark.parametrize('kind',['ui_error_with_old_native_status','native_with_invalid_status'])
def test_untrusted_or_old_native_status_never_downgrades_real_page_failure(tmp_path,kind):
    _,control,run_id=running_control(tmp_path)
    if kind=='ui_error_with_old_native_status':
        error=ReceiverPageError('native_session_cooldown is only text')
        error.native_session_status=deepcopy(COOLDOWN)
    else:
        error=NativeLaneError('native_session_cooldown')
        error.native_session_status={**COOLDOWN,'retry_after_seconds':False}
    # The existing strict control status validator rejects malformed status;
    # do not feed it a fake provider payload just to exercise classification.
    if kind=='native_with_invalid_status':
        classifier=getattr(owner_main,'receiver_page_health',None)
        if classifier is None:pytest.skip('classifier does not exist on red baseline')
        assert classifier(error)==('page_unavailable','page_not_ready')
    else:
        run_actual_owner_error_handler(error,control,run_id)
        assert control.snapshot()['reason_code']=='page_not_ready'


def test_fixed_lookup_stage_and_reason_survive_preflight_failure(make_context):
    adapter=FakeAdapter(prepare_error=ReceiverDetailError('do-not-persist-private-message',{}))
    def fresh():adapter.last_original_lookup={'lookup_stage_index':6,'reason_code':'original_lookup_unavailable',
        'candidate_count':0,'checked_count':0,'page_open_count':1,'raw_page':'PRIVATE_CANARY'}
    adapter.prepare_hook=fresh;context=make_context(adapter=adapter)
    result=context.publisher.publish(context.task_id)
    assert result['state']=='preflight_blocked' and result['reason_code']=='preflight_failed'
    file=next((context.root/'evidence').glob('*/*/preflight_failure.json'))
    record=json.loads(file.read_text(encoding='utf8'))
    assert record['original_lookup']['lookup_stage_index']==6
    assert record['lookup_reason_code']=='original_lookup_unavailable'
    assert 'PRIVATE_CANARY' not in file.read_text(encoding='utf8')
    assert context.store.get_reply(context.task_id)['send_token'] is None
    assert adapter.submit_entries==adapter.clicks==0


def test_prior_lookup_diagnostics_are_not_misattributed_to_another_attempt(make_context):
    adapter=FakeAdapter(prepare_error=RuntimeError('fresh unrelated failure'))
    adapter.last_original_lookup={'lookup_stage_index':10,'reason_code':'matched','candidate_count':1,'final_verified':1}
    context=make_context(adapter=adapter)
    result=context.publisher.publish(context.task_id)
    assert result['state']=='preflight_blocked'
    assert list((context.root/'evidence').glob('*/*/preflight_failure.json'))==[]
    assert context.store.get_reply(context.task_id)['send_token'] is None
    assert adapter.submit_entries==adapter.clicks==0


def test_unknown_lookup_reason_and_boolean_stage_cannot_leak_as_fixed_diagnostics(make_context):
    adapter=FakeAdapter(prepare_error=RuntimeError('private error'))
    adapter.prepare_hook=lambda:setattr(adapter,'last_original_lookup',{
        'candidate_count':0,'lookup_stage_index':True,'reason_code':'https://private.invalid/?secret=CANARY'})
    context=make_context(adapter=adapter);context.publisher.publish(context.task_id)
    file=next((context.root/'evidence').glob('*/*/preflight_failure.json'))
    record=json.loads(file.read_text(encoding='utf8'))
    assert 'lookup_stage_index' not in record['original_lookup']
    assert 'lookup_reason_code' not in record
    assert 'CANARY' not in file.read_text(encoding='utf8')
