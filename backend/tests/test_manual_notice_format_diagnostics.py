"""Synthetic manual-viewer diagnostics; no browser, account or real store."""
from copy import deepcopy
import json
import re
import shutil
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest
from app.assistant import __main__ as owner
from app.assistant import manual_verification as manual
from app.assistant.manual_verification_page import SCRIPT
from app.assistant.native_lane import NativeSessionPaused
from app.assistant.native_lifecycle import SessionLifecycleError
from app.assistant.native_receiver import NativeReceiverError
from app.assistant.native_transport import NativeTransportError
from app.mention_receiver.browser import ReceiverInterventionRequired

CODE='invalid_notice_identifier'
REASON='native_notice_format_unavailable'
RUN='a'*32
FACTS={'endpoint_role':'identity','http_status':200,'api_status_code':18}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    def deny(*a,**kw):raise AssertionError('synthetic tests cannot use network')
    monkeypatch.setattr(socket.socket,'connect',deny)
    monkeypatch.setattr(socket,'getaddrinfo',deny)


def status(**changes):
    return {'state':'paused','reason_code':CODE,'generation':1,'refresh_count':1,
        'source':'same_context','seal_state':'sealed','transient_failures':0,
        'last_transient_code':None,'retry_after_seconds':0,**changes}


def setup_finish():
    current={'run_id':RUN,'process_alive':True,'state':'paused','desired_state':'paused',
        'receiver_state':'verification_required','worker_state':'idle',
        'publication_busy':False,'queue_worker_busy':False}
    server=manual.ManualVerificationServer(RUN,allowed=lambda:dict(current),clock=lambda:1000.)
    headers={'host':'127.0.0.1:8767','origin':server.origin,'content-type':'application/json'}
    code,_,html=server.request('GET','/',headers);assert code==200
    headers['x-douyinmind-manual']=re.search(rb'name="manual-nonce" content="([a-f0-9]{64})"',html)[1].decode()
    code,_,body=server.request('POST','/api/begin',headers,b'{}');assert code==202
    session=json.loads(body)['session_id'];server.process_one(lambda *_:{'ok':True})
    action={'session_id':session,'action':'finish','payload':{}}
    assert server.request('POST','/api/action',headers,json.dumps(action).encode())[0]==202
    return server,current


@pytest.mark.parametrize('error_type',[NativeReceiverError,SessionLifecycleError])
def test_finish_typed_format_failure_preserves_reason_without_old_api_facts(error_type):
    server,_=setup_finish()
    def handler(*_):raise error_type(CODE,transport_facts=FACTS)
    result=server.process_one(handler)
    assert result['state']=='verification_failed' and result['active'] is False
    assert result['reason_code']==REASON and 'transport_facts' not in result
    assert server.session_active is False and server.owner_finalization_pending is True


@pytest.mark.parametrize('error',[ValueError(CODE),ReceiverInterventionRequired('网页要求本人核验')])
def test_text_and_browser_challenge_are_not_native_format_authority(error):
    error.code=CODE
    server,_=setup_finish()
    def handler(*_):raise error
    result=server.process_one(handler)
    assert result['reason_code']=='action_failed' and 'transport_facts' not in result


def test_fake_exception_properties_are_never_read():
    class FakeError(Exception):
        @property
        def code(self):raise AssertionError('untrusted code property')
        @property
        def transport_facts(self):raise AssertionError('untrusted private property')
    server,_=setup_finish()
    def handler(*_):raise FakeError('synthetic nonpublic text')
    result=server.process_one(handler)
    assert result['reason_code']=='action_failed'
    assert 'synthetic nonpublic text' not in json.dumps(result)


def test_current_identity_api_error_remains_separate():
    server,_=setup_finish()
    def handler(*_):raise NativeTransportError('api_error',transport_facts=FACTS)
    result=server.process_one(handler)
    assert result['reason_code']=='native_api_error' and result['transport_facts']==FACTS


def test_format_safe_projection_does_not_read_numeric_or_private_fields():
    class Tripwire:
        def __str__(self):raise AssertionError('private value read')
    assert manual.project_manual_failure_fields({'reason_code':REASON,
        'transport_facts':Tripwire(),'raw_notice':Tripwire()})=={'reason_code':REASON}
    assert manual.project_manual_failure_fields({'reason_code':CODE})=={'reason_code':None}


def test_current_typed_intake_marker_maps_format_error():
    error=NativeSessionPaused(status());error.native_intake_data_code=CODE
    assert owner.intervention_health(error)==('page_unavailable',REASON)


def test_old_status_without_current_intake_marker_does_not_claim_format_source():
    assert owner.intervention_health(NativeSessionPaused(status()))==('page_unavailable','native_session_needs_review')


def test_real_human_challenge_cannot_be_overridden_by_stale_marker():
    error=NativeSessionPaused(status(reason_code='verification_required'))
    error.native_intake_data_code=CODE
    assert owner.intervention_health(error)==('verification_required','native_verification_required')
    ordinary=ReceiverInterventionRequired('网页要求本人核验')
    ordinary.native_intake_data_code=CODE;ordinary.native_session_status=status()
    assert owner.intervention_health(ordinary)==('verification_required','human_intervention_required')


@pytest.mark.parametrize('error_type',[NativeReceiverError,SessionLifecycleError])
def test_explicit_start_current_typed_format_error_has_safe_status(error_type):
    lane=SimpleNamespace(_public_native_session_status=lambda:status(transport_facts=FACTS))
    state,code,safe=owner.explicit_start_failure_health(error_type(CODE),lane)
    assert (state,code)==('page_unavailable',REASON)
    assert safe==status() and 'transport_facts' not in safe


@pytest.mark.parametrize('error',[ValueError(CODE),NativeReceiverError('verification_required'),
    SessionLifecycleError('session_identity_mismatch')])
def test_explicit_start_unknown_or_auth_errors_do_not_trust_old_format_status(error):
    lane=SimpleNamespace(_public_native_session_status=lambda:status())
    assert owner.explicit_start_failure_health(error,lane)==('verification_required','human_identity_unconfirmed',None)


def test_explicit_start_current_error_does_not_reuse_unrelated_snapshot():
    lane=SimpleNamespace(_public_native_session_status=lambda:status(reason_code='api_error',transport_facts=FACTS))
    assert owner.explicit_start_failure_health(NativeReceiverError(CODE),lane)==('page_unavailable',REASON,None)


def test_finish_format_failure_acknowledges_epoch_before_clearing_manual_hold():
    server,current=setup_finish();calls=[]
    class Control:
        def __init__(self):self.queue=SimpleNamespace(update_control=self.update)
        def update(self,fn):current.update(fn(deepcopy(current)))
        def pause(self):calls.append('pause');current.update(state='paused',desired_state='paused')
        def heartbeat(self,*a,**kw):calls.append('heartbeat')
    def handler(*_):raise NativeReceiverError(CODE)
    hold=threading.Event()
    assert owner.pump_manual_verification(server,handler,Control(),RUN,threading.RLock(),hold)
    assert not hold.is_set() and not server.owner_finalization_pending
    assert current['receiver_state']=='verification_required'
    assert current['state']==current['desired_state']=='paused'
    assert current['manual_verification']['reason_code']==REASON
    assert calls.count('pause')==2


def test_wrong_run_cannot_record_terminal_format_reason():
    current={'run_id':'b'*32}
    control=SimpleNamespace(queue=SimpleNamespace(update_control=lambda fn:fn(current)))
    server=SimpleNamespace(safe_status=lambda:{'state':'verification_failed','reason_code':REASON},
        session_active=False,owner_finalization_pending=False)
    with pytest.raises(ValueError,match='runtime_owner_changed'):
        owner.record_manual_verification(control,RUN,server)
    assert current=={'run_id':'b'*32}


def test_page_explains_data_failure_separately_from_api_and_human_verification():
    node=shutil.which('node')
    if not node:pytest.skip('Node unavailable for isolated viewer script test')
    source=SCRIPT[SCRIPT.index('function failureMessage'):SCRIPT.index('async function request')]
    checks="""const a=failureMessage({state:'verification_failed',reason_code:'native_notice_format_unavailable'});
if(!a||!a.includes('数据格式')||!a.includes('保持暂停')||!a.includes('不能据此判定需要验证码'))process.exit(1);
const b=failureMessage({state:'verification_failed',reason_code:'native_api_error',transport_facts:{endpoint_role:'identity',http_status:200,api_status_code:18}});
if(!b||!b.includes('业务码 18')||b.includes('通知数据格式'))process.exit(2);
if(failureMessage({state:'active',reason_code:'native_notice_format_unavailable'})!==null)process.exit(3);
if(failureMessage({state:'verification_failed',reason_code:'invalid_notice_identifier'})!==null)process.exit(4);
console.log('synthetic_format_page_pass');"""
    result=subprocess.run([node,'--input-type=module','-'],input=source+checks,text=True,
        encoding='utf8',capture_output=True,timeout=10)
    assert result.returncode==0 and result.stdout.strip()=='synthetic_format_page_pass'
