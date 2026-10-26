from datetime import datetime, timedelta, timezone
import json

from app.assistant import runtime_diagnostics as d


def test_startup_fingerprint_does_not_change_when_disk_changes(tmp_path, monkeypatch):
    package=tmp_path/'app/assistant'; package.mkdir(parents=True)
    code=package/'control.py'; code.write_text('old source',encoding='utf-8')
    monkeypatch.setattr(d,'BACKEND',tmp_path)
    original=d.capture_build('helper')
    code.write_text('new source',encoding='utf-8')
    now=datetime.now(timezone.utc)
    result=d.diagnostics({'runtime_build':original,'process_alive':True,'state':'running',
                         'heartbeat_at':now.isoformat(),'last_capture_at':now.isoformat()},
                        {'b_view_visible':3},now=now,backend_build=original)
    assert result['helper_build']['source_version']==original['source_version']
    assert result['helper_build']['matches_disk'] is False
    assert result['backend_build']['matches_disk'] is False
    assert result['delivery']['recipient_visibility']=='not_automatically_verified'
    assert result['intake_scope']['all_notifications_confirmed'] is False


def test_missing_version_is_unknown_not_loaded_success(monkeypatch):
    monkeypatch.setattr(d,'source_version',lambda:'a'*16)
    result=d.diagnostics({'process_alive':True,'state':'paused'}, {}, backend_build={})
    assert result['helper_build']['matches_disk'] is None
    assert result['backend_build']['matches_disk'] is None
    assert {n['code'] for n in result['notices']}=={'helper_version_unrecorded'}


def test_old_or_future_timestamps_warn_without_restarting_or_changing_task(monkeypatch):
    monkeypatch.setattr(d,'source_version',lambda:'a'*16)
    now=datetime(2026,10,22,4,tzinfo=timezone.utc)
    control={'state':'running','heartbeat_at':(now-timedelta(seconds=181)).isoformat(),
             'last_capture_at':(now+timedelta(seconds=8)).isoformat()}
    before=dict(control)
    result=d.diagnostics(control,{'delivery_unknown':1,'verification_required':1},now=now,
                         backend_build={'source_version':'a'*16})
    assert {n['code'] for n in result['notices']}=={'heartbeat_needs_review','capture_needs_review'}
    assert control==before
    assert result['delivery']['requires_review']==2


def test_ready_fresh_capture_does_not_assert_notification_completeness(monkeypatch):
    monkeypatch.setattr(d,'source_version',lambda:'a'*16)
    now=datetime.now(timezone.utc)
    control={'state':'running','process_alive':True,'runtime_build':{'source_version':'a'*16},
             'heartbeat_at':now.isoformat(),'last_capture_at':now.isoformat()}
    result=d.diagnostics(control,{},now=now,backend_build={'source_version':'a'*16})
    assert not result['notices']
    assert result['intake_scope']['visible_candidates_per_batch']==3
    assert result['intake_scope']['all_notifications_confirmed'] is False


def test_failure_chain_never_serializes_secret_message_or_arbitrary_class():
    class PrivateCustomerNameError(Exception): pass
    try:
        try: raise TimeoutError('Bearer PRIVATE-KEY https://private.example?secret=123')
        except TimeoutError: raise PrivateCustomerNameError('prompt and account') from None
    except Exception as error:
        value=d.safe_failure_chain(error)
    assert value==['OtherError','TimeoutError']
    output=json.dumps(value)
    for secret in ('Bearer','PRIVATE','private.example','account','CustomerName'):
        assert secret not in output


def test_failure_cycle_is_bounded():
    error=ValueError('private'); error.__context__=error
    assert d.safe_failure_chain(error)==['ValueError']
