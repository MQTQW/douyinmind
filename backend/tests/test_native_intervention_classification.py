"""Classification must use this exception's provenance, never old state."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest

from app.assistant.__main__ import intervention_health
from app.assistant.native_lane import NativeLane
from app.assistant.native_receiver import NativeReceiverError
from app.mention_receiver.browser import ReceiverInterventionRequired


def test_real_browser_intervention_is_not_downgraded_by_old_native_status():
    error=ReceiverInterventionRequired('网页要求人工验证或访问受限')
    error.native_session_status={'state':'paused','reason_code':'session_refresh_unavailable'}
    assert intervention_health(error)==('verification_required','human_intervention_required')


def test_login_and_unrecognized_marker_still_require_human_identity_check():
    error=ReceiverInterventionRequired('需要本人登录')
    error.native_intake_data_code='session_identity_mismatch'
    assert intervention_health(error)==('login_required','human_intervention_required')


@pytest.mark.parametrize('code',['invalid_native_visibility','group_schema_unknown'])
def test_native_poll_attaches_data_provenance_to_the_actual_failure(code):
    @contextmanager
    def cycle():yield
    def fail():raise NativeReceiverError(code)
    pauses=[]
    fake=SimpleNamespace(_owner=lambda:None,should_stop=lambda:False,
       source_store=SimpleNamespace(safe_status=lambda:{'armed':True},pause=pauses.append),
       native_receiver=SimpleNamespace(poll_once=fail),
       session_lifecycle=SimpleNamespace(cycle=cycle,safe_status=lambda:{'state':'paused'}),
       _with_native_session_status=lambda error:error)
    with pytest.raises(ReceiverInterventionRequired) as caught:
        NativeLane.poll_native(fake)
    assert caught.value.native_intake_data_code==code
    assert intervention_health(caught.value)==('page_unavailable','native_notice_format_unavailable')
    assert pauses==[code]


def test_native_identity_failure_never_becomes_a_data_format_problem():
    @contextmanager
    def cycle():yield
    def fail():raise NativeReceiverError('session_identity_mismatch')
    fake=SimpleNamespace(_owner=lambda:None,should_stop=lambda:False,
       source_store=SimpleNamespace(safe_status=lambda:{'armed':True},pause=lambda code:None),
       native_receiver=SimpleNamespace(poll_once=fail),
       session_lifecycle=SimpleNamespace(cycle=cycle,safe_status=lambda:{'state':'paused'}),
       _with_native_session_status=lambda error:error)
    with pytest.raises(ReceiverInterventionRequired) as caught:NativeLane.poll_native(fake)
    assert not hasattr(caught.value,'native_intake_data_code')
    assert intervention_health(caught.value)[0]=='verification_required'
