"""Synthetic request/AST/fake owner tests; no account, socket, model or store."""
import ast
from copy import deepcopy
from pathlib import Path
import json
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest
from app.assistant import manual_verification as manual
from app.assistant.manual_verification_page import SCRIPT
from app.assistant.native_transport import NativeTransportError
from app.assistant.native_receiver import NativeReceiverError
from app.assistant.native_lifecycle import SessionLifecycleError

RUN='a'*32
FACTS={'endpoint_role':'identity','http_status':200,'api_status_code':18}

def status(**changes):
    return {'state':'paused','reason_code':'api_error','generation':1,'refresh_count':1,
        'source':'same_context','seal_state':'sealed','transient_failures':0,
        'last_transient_code':None,'retry_after_seconds':0,**changes}

def make():
    trusted={'run_id':RUN,'process_alive':True,'state':'paused','desired_state':'paused',
        'receiver_state':'verification_required','worker_state':'idle',
        'publication_busy':False,'queue_worker_busy':False}
    server=manual.ManualVerificationServer(RUN,allowed=lambda:dict(trusted),clock=lambda:1000.)
    headers={'host':'127.0.0.1:8767','origin':server.origin,'content-type':'application/json'}
    code,_,html=server.request('GET','/',headers);assert code==200
    headers['x-douyinmind-manual']=re.search(rb'name="manual-nonce" content="([a-f0-9]{64})"',html)[1].decode()
    code,_,body=server.request('POST','/api/begin',headers,b'{}');assert code==202
    session=json.loads(body)['session_id'];server.process_one(lambda *_:{'ok':True})
    return server,headers,session,trusted

def finish(server,headers,session):
    data={'session_id':session,'action':'finish','payload':{}}
    assert server.request('POST','/api/action',headers,json.dumps(data).encode())[0]==202

def main_functions():
    path=Path(manual.__file__).with_name('__main__.py')
    tree=ast.parse(path.read_text(encoding='utf8'))
    names={'manual_needs_owner','record_manual_verification','pump_manual_verification',
        '_manual_identity_failure_status','explicit_start_failure_health'}
    selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
    scope={'__package__':'app.assistant','utc':lambda:'2030-01-02T10:00:00Z'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[])),str(path),'exec'),scope)
    return scope

def test_finish_api_error_retains_safe_reason_and_numbers():
    server,headers,session,_=make();finish(server,headers,session)
    def handler(*_):raise NativeTransportError('api_error',transport_facts=FACTS)
    result=server.process_one(handler)
    assert result['state']=='verification_failed' and result['active'] is False
    assert result['reason_code']=='native_api_error'
    assert result['transport_facts']==FACTS and server.session_active is False

@pytest.mark.parametrize('error_type',[NativeTransportError,NativeReceiverError,SessionLifecycleError])
def test_only_real_typed_identity_api_error_is_classified(error_type):
    result=manual.native_identity_api_failure(error_type('api_error',transport_facts=FACTS))
    assert result=={'reason_code':'native_api_error','transport_facts':FACTS}

def test_unknown_exception_cannot_self_report_native_identity_facts():
    class FakeError(Exception):
        code='api_error'
        @property
        def transport_facts(self):raise AssertionError('must not read untrusted properties')
    error=FakeError('SYNTHETIC_PRIVATE_MESSAGE')
    assert manual.native_identity_api_failure(error) is None
    server,headers,session,_=make();finish(server,headers,session)
    def handler(*_):raise error
    result=server.process_one(handler)
    assert result['reason_code']=='action_failed' and 'transport_facts' not in result
    assert 'SYNTHETIC_PRIVATE_MESSAGE' not in json.dumps(result)

@pytest.mark.parametrize('facts',[
    {**FACTS,'endpoint_role':'notice_list'}, {**FACTS,'http_status':201},
    {**FACTS,'api_status_code':0}, {**FACTS,'api_status_code':True},
])
def test_inconsistent_or_other_endpoint_facts_do_not_become_identity_diagnostics(facts):
    assert manual.native_identity_api_failure(NativeTransportError('api_error',transport_facts=facts)) is None

def test_non_finish_typed_exception_stays_generic_and_cannot_complete():
    server,headers,session,_=make()
    request={'session_id':session,'action':'screenshot','payload':{}}
    assert server.request('POST','/api/action',headers,json.dumps(request).encode())[0]==202
    def handler(*_):raise NativeTransportError('api_error',transport_facts=FACTS)
    result=server.process_one(handler)
    assert result['state']=='verification_failed' and result['reason_code']=='action_failed'
    assert 'transport_facts' not in result

def test_later_action_or_failure_does_not_reuse_previous_numbers():
    server,headers,session,_=make();finish(server,headers,session)
    def fail(*_):raise NativeTransportError('api_error',transport_facts=FACTS)
    server.process_one(fail)
    assert server.safe_status()['transport_facts']==FACTS
    code,_,body=server.request('POST','/api/begin',headers,b'{}');assert code==202
    assert 'transport_facts' not in server.safe_status()
    session=json.loads(body)['session_id'];server.process_one(lambda *_:{'ok':True})
    assert 'transport_facts' not in server.safe_status()
    finish(server,headers,session)
    def unknown(*_):raise ValueError('SYNTHETIC_PRIVATE_MESSAGE')
    result=server.process_one(unknown)
    assert 'transport_facts' not in result and result['reason_code']=='action_failed'

def test_safe_projection_keeps_only_fixed_reason_and_current_numeric_facts():
    class Tripwire:
        def __str__(self):raise AssertionError('do not stringify unknown data')
    raw={'reason_code':'native_api_error','transport_facts':{**FACTS,'raw_body':Tripwire()},'cookie':Tripwire()}
    assert manual.project_manual_failure_fields(raw)=={'reason_code':'native_api_error','transport_facts':FACTS}
    assert manual.project_manual_failure_fields({'reason_code':['api_error'],'transport_facts':Tripwire()})=={'reason_code':None}

def test_record_manual_projects_safe_reason_without_changing_receiver_health():
    scope=main_functions();current={'run_id':RUN,'receiver_state':'verification_required'}
    def update(change):current.update(change(deepcopy(current)))
    control=SimpleNamespace(queue=SimpleNamespace(update_control=update))
    server=SimpleNamespace(safe_status=lambda:{'state':'verification_failed','reason_code':'native_api_error','transport_facts':FACTS},
        session_active=False,owner_finalization_pending=False)
    scope['record_manual_verification'](control,RUN,server)
    assert current['manual_verification']['reason_code']=='native_api_error'
    assert current['manual_verification']['transport_facts']==FACTS
    assert current['receiver_state']=='verification_required'

def test_failed_finish_preserves_original_ack_and_pause_hold_finalization():
    scope=main_functions();server,headers,session,current=make();finish(server,headers,session)
    calls=[]
    class Control:
        queue=None
        def __init__(self):self.queue=SimpleNamespace(update_control=self.update)
        def update(self,change):current.update(change(deepcopy(current)))
        def pause(self):calls.append('pause');current.update(state='paused',desired_state='paused')
        def heartbeat(self,*args,**kwargs):calls.append('heartbeat')
        def snapshot(self):return dict(current)
    def handler(*_):raise NativeReceiverError('api_error',transport_facts=FACTS)
    hold=threading.Event()
    assert scope['pump_manual_verification'](server,handler,Control(),RUN,threading.RLock(),hold) is True
    assert server.owner_finalization_pending is False and server.session_active is False and hold.is_set() is False
    assert current['state']=='paused' and current['desired_state']=='paused'
    assert current['receiver_state']=='verification_required'
    assert current['manual_verification']['reason_code']=='native_api_error'
    assert calls.count('pause')==2

def test_start_classifies_current_typed_api_error_without_reusing_old_numbers():
    scope=main_functions();lane=SimpleNamespace(_public_native_session_status=lambda:status(transport_facts={**FACTS,'api_status_code':99}))
    state,code,safe=scope['explicit_start_failure_health'](NativeReceiverError('api_error',transport_facts=FACTS),lane)
    assert (state,code)==('page_unavailable','native_api_error')
    assert safe['state']=='paused' and safe['reason_code']=='api_error' and safe['transport_facts']==FACTS
    _,_,safe=scope['explicit_start_failure_health'](SessionLifecycleError('api_error'),lane)
    assert 'transport_facts' not in safe

@pytest.mark.parametrize('error',[ValueError('SYNTHETIC_PRIVATE_MESSAGE'),SessionLifecycleError('session_identity_mismatch'),NativeReceiverError('verification_required')])
def test_unknown_or_real_auth_identity_failures_keep_the_original_conservative_gate(error):
    scope=main_functions();lane=SimpleNamespace(_public_native_session_status=lambda:status(transport_facts=FACTS))
    assert scope['explicit_start_failure_health'](error,lane)==('verification_required','human_identity_unconfirmed',None)

def test_viewer_explains_api_failure_using_fixed_safe_facts_without_claiming_captcha():
    node=shutil.which('node')
    if not node:pytest.skip('Node not installed; browser script fixture is optional')
    source=SCRIPT[SCRIPT.index('function failureMessage'):SCRIPT.index('async function request')]
    checks="""const a=failureMessage({state:'verification_failed',reason_code:'native_api_error',transport_facts:{endpoint_role:'identity',http_status:200,api_status_code:18}});
if(!a.includes('业务码 18')||!a.includes('保持暂停')||!a.includes('不证明需要验证码'))process.exit(1);
if(failureMessage({state:'verification_failed',reason_code:'SYNTHETIC_PRIVATE_MESSAGE'})!==null)process.exit(2);
console.log('synthetic_viewer_reason_pass');"""
    result=subprocess.run([node,'--input-type=module','-'],input=source+checks,text=True,encoding='utf8',capture_output=True,timeout=10)
    assert result.returncode==0 and result.stdout.strip()=='synthetic_viewer_reason_pass'
