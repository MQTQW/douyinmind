"""Native failure reporting cannot invent a captcha or lift any pause."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import ast
import pytest

from app.assistant import __main__ as owner
from app.assistant import native_lane as lane_module
from app.assistant.native_receiver import NativeReceiverError
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_lifecycle import SessionLifecycleError
from app.assistant.control import AssistantControl
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_native_session_status import MemoryQueue,RUN_ID


def status(code='api_error'):
    return {'state':'paused','reason_code':code,'generation':1,'refresh_count':1,
        'source':'same_context','seal_state':'sealed','transient_failures':0,
        'last_transient_code':None,'retry_after_seconds':0}


def actual_pause(tmp_path,code='api_error',observed=None):
    source=NativeSourceStore(tmp_path/'sources.sqlite3','9001','https://www.douyin.com/user/synthetic-B',create=True,clock=lambda:102)
    source.set_verified();calls=[]
    @contextmanager
    def cycle():yield
    def fail():calls.append(code);raise NativeReceiverError(code)
    fake=SimpleNamespace(_owner=lambda:None,should_stop=lambda:False,
        source_store=source,native_receiver=SimpleNamespace(poll_once=fail),
        session_lifecycle=SimpleNamespace(cycle=cycle,safe_status=lambda:deepcopy(status(code) if observed is None else observed)),
        _with_native_session_status=lambda error:error)
    with pytest.raises(ReceiverInterventionRequired) as caught:lane_module.NativeLane.poll_native(fake)
    return caught.value,source,calls


def test_api_error_is_review_not_sms_and_source_stays_paused(tmp_path):
    error,source,calls=actual_pause(tmp_path)
    assert owner.intervention_health(error)==('page_unavailable','native_api_error')
    assert source.safe_status()['pause_code']=='api_error' and calls==['api_error']
    with source._connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='verified_at'").fetchone()[0]=='0'
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0


@pytest.mark.parametrize('code,expected',[
    ('verification_required',('verification_required','native_verification_required')),
    ('session_invalid',('login_required','native_session_invalid')),
    ('session_expired',('login_required','native_session_invalid')),
    ('authorization_expired',('login_required','native_session_invalid')),
    ('session_identity_mismatch',('login_required','native_identity_mismatch')),
    ('session_existing_draft',('page_unavailable','native_session_needs_review')),
    ('session_refresh_unavailable',('page_unavailable','native_session_needs_review')),
    ('invalid_parameters',('page_unavailable','native_session_needs_review')),
    ('session_transient_retry_exhausted',('page_unavailable','native_session_needs_review'))])
def test_actual_native_pause_fixed_classification_never_clears_pause(tmp_path,code,expected):
    error,source,calls=actual_pause(tmp_path,code)
    assert owner.intervention_health(error)==expected
    assert source.safe_status()['pause_code']==code and calls==[code]


@pytest.mark.parametrize('code',['invalid_native_visibility','group_schema_unknown'])
def test_actual_native_format_marker_keeps_existing_format_hold(tmp_path,code):
    error,source,_=actual_pause(tmp_path,code,status('session_refresh_unavailable'))
    assert owner.intervention_health(error)==('page_unavailable','native_notice_format_unavailable')
    assert source.safe_status()['pause_code']==code


@pytest.mark.parametrize('bad',[None,{}, {'state':'paused'}, {**status(),'generation':True},
    {**status(),'state':'ready','reason_code':None}])
def test_invalid_native_status_still_uses_human_pause_path_without_inventing_captcha(tmp_path,bad):
    error=lane_module.NativeSessionPaused(bad)
    assert owner.intervention_health(error)==('page_unavailable','native_failure_unconfirmed')
    assert isinstance(error,ReceiverInterventionRequired)


def test_changed_typed_status_cannot_be_reported_as_current_api_error(tmp_path):
    error,source,_=actual_pause(tmp_path)
    error.native_session_status=status('verification_required')
    assert owner.intervention_health(error)==('page_unavailable','native_failure_unconfirmed')
    assert source.safe_status()['pause_code']=='api_error'


@pytest.mark.parametrize('old_status',[status(),{},None])
def test_browser_challenge_cannot_be_downgraded_by_attached_old_api_status(old_status):
    error=ReceiverInterventionRequired('网页要求人工验证或访问受限')
    error.native_session_status=old_status
    assert owner.intervention_health(error)==('verification_required','human_intervention_required')


def test_legacy_browser_login_still_requires_login():
    assert owner.intervention_health(ReceiverInterventionRequired('需要本人登录'))==('login_required','human_intervention_required')


def test_browser_challenge_with_old_data_marker_is_not_reclassified():
    error=ReceiverInterventionRequired('网页要求人工核验')
    error.native_intake_data_code='invalid_notice_page'
    error.native_session_status=status('api_error')
    assert owner.intervention_health(error)==('verification_required','human_intervention_required')


def test_actual_owner_except_keeps_control_paused_with_new_review_label(tmp_path):
    error,source,calls=actual_pause(tmp_path)
    queue=MemoryQueue(tmp_path/'not-created.sqlite3')
    control=AssistantControl(queue,tmp_path,process_probe=lambda pid:True)
    tree=ast.parse(Path(owner.__file__).read_text(encoding='utf-8'))
    handler=next(n for n in ast.walk(tree) if isinstance(n,ast.ExceptHandler) and n.name=='error'
        and ast.unparse(n.type)=='ReceiverInterventionRequired'
        and any(isinstance(x,ast.Call) and isinstance(x.func,ast.Attribute) and x.func.attr=='heartbeat' for x in ast.walk(n)))
    module=ast.Module(body=[ast.Try(body=[ast.Raise(exc=ast.Name(id='failure',ctx=ast.Load()),cause=None)],
        handlers=[handler],orelse=[],finalbody=[])],type_ignores=[]);ast.fix_missing_locations(module)
    import threading
    ns={'failure':error,'control':control,'run_id':RUN_ID,'ReceiverInterventionRequired':ReceiverInterventionRequired,
        'intervention_health':owner.intervention_health,'browser_intervention':threading.Event()}
    exec(compile(module,'actual-owner-native-pause','exec'),ns)
    assert queue.value['desired_state']=='paused'
    assert queue.value['receiver_state']=='page_unavailable' and queue.value['reason_code']=='native_api_error'
    assert queue.value['native_session_status']['reason_code']=='api_error'
    assert calls==['api_error'] and source.safe_status()['pause_code']=='api_error'
    assert ns['browser_intervention'].is_set() and not ns['data_intervention']
    assert not (tmp_path/'not-created.sqlite3').exists()


def test_existing_explicit_resume_does_not_clear_api_error():
    from tests.test_assistant_native_lifecycle import CandidateTests
    fixture=CandidateTests();fixture.setUp();lifecycle=fixture.lifecycle()
    try:
        lifecycle._blocked='api_error'
        with pytest.raises(SessionLifecycleError,match='api_error'):
            lifecycle.resume_after_explicit_start()
        assert lifecycle.safe_status()['reason_code']=='api_error'
    finally:fixture.doCleanups()
