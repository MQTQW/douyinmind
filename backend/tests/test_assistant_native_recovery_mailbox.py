"""Real temporary IPC files only; no app state, browser, service or account."""
import json
import os
from pathlib import Path
import threading

import pytest

from app.assistant.native_recovery_mailbox import NativeRecoveryMailbox, NativeRecoveryMailboxError

RUN='a'*32
SOURCE='native:'+'b'*64
OP='review_legacy_prequeue'


class Control:
    def __init__(self):
        self.value={'run_id':RUN,'state':'paused','desired_state':'paused','process_alive':True,
                    'worker_state':'idle','pid':os.getpid()}
        self.hook=None
    def snapshot(self):
        if self.hook:self.hook()
        return dict(self.value)


def make(tmp_path):
    control=Control();clock=[1000.0]
    box=NativeRecoveryMailbox(tmp_path,RUN,control,clock=lambda:clock[0])
    return box,control,clock


@pytest.mark.parametrize('source,operation',[
    ('native-hint:'+'c'*64,'review_legacy_prequeue'),
    ('native-hint:'+'c'*64,'resume_interrupted_prequeue'),
    (SOURCE,'request_visibility_ui_review'),
])
def test_operation_and_source_namespaces_cannot_be_swapped(tmp_path,source,operation):
    box,_,_=make(tmp_path)
    with pytest.raises(NativeRecoveryMailboxError):box.submit(source,operation)
    assert list(tmp_path.iterdir())==[]


def test_explicit_visibility_hint_request_is_once_and_never_an_effect(tmp_path):
    box,_,_=make(tmp_path);hint='native-hint:'+'c'*64
    request=box.submit(hint,'request_visibility_ui_review')
    calls=[]
    def handler(operation,source):
        calls.append((operation,source))
        return {'ok':True,'queued_for_fresh_ui':True,'dispatchable':False}
    result=box.process_one(handler)
    assert result['status']=='done' and result['no_model'] and result['no_import'] and result['no_send']
    assert calls==[('request_visibility_ui_review',hint)]
    assert box.process_one(lambda *_:pytest.fail('replay')) is None


def pending(box,request):
    return box.path/(request['request_id']+'.json')


def result_file(box,request):
    return box.path/(request['request_id']+'.result')


def rewrite(box,request,change):
    path=pending(box,request);value=json.loads(path.read_text());change(value)
    path.write_text(json.dumps(value),encoding='utf-8')


def test_constructor_and_idle_owner_create_no_files(tmp_path):
    box,_,_=make(tmp_path)
    assert list(tmp_path.iterdir())==[]
    assert box.process_one(lambda *_:pytest.fail('empty')) is None
    assert list(tmp_path.iterdir())==[]


def test_fixed_current_run_atomic_publish_claim_and_result(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP)
    assert request['status']=='queued' and pending(box,request).stat().st_size<=1024
    seen=[]
    def handler(op,source):
        assert not pending(box,request).exists()
        assert (box.path/(request['request_id']+'.claimed')).is_file()
        seen.append((op,source))
        return {'ok':True,'reviewed':True,'queued_for_fresh_ui':True,'dispatchable':False,'private':'DO-NOT-SAVE'}
    result=box.process_one(handler)
    assert seen==[(OP,SOURCE)] and result['status']=='done' and result['reviewed'] is True
    assert result['no_model'] and result['no_import'] and result['no_send'] and not result['dispatchable']
    assert 'DO-NOT-SAVE' not in result_file(box,request).read_text()
    assert box.process_one(lambda *_:pytest.fail('repeat')) is None


@pytest.mark.parametrize('field,value',[('state','running'),('desired_state','running'),
    ('process_alive',False),('worker_state','processing'),('run_id','c'*32)])
def test_submit_requires_paused_current_idle_registered_process(tmp_path,field,value):
    box,control,_=make(tmp_path);control.value[field]=value
    with pytest.raises(NativeRecoveryMailboxError,match='native_recovery_not_paused'):box.submit(SOURCE,OP)
    assert list(tmp_path.iterdir())==[]


def test_owner_drain_does_not_claim_when_paused_scope_changes(tmp_path):
    box,control,_=make(tmp_path);request=box.submit(SOURCE,OP);control.value['worker_state']='processing'
    assert box.process_one(lambda *_:pytest.fail('busy')) is None
    assert pending(box,request).is_file()


def test_owner_pid_and_thread_are_exact(tmp_path):
    box,control,_=make(tmp_path);request=box.submit(SOURCE,OP)
    control.value['pid']=os.getpid()+100000
    assert box.process_one(lambda *_:pytest.fail('foreign pid')) is None
    control.value['pid']=os.getpid();errors=[]
    def other():
        try:box.process_one(lambda *_:pytest.fail('thread'))
        except NativeRecoveryMailboxError as error:errors.append(error.code)
    thread=threading.Thread(target=other);thread.start();thread.join()
    assert errors==['native_recovery_owner_thread_required'] and pending(box,request).exists()


@pytest.mark.parametrize('source,op',[('https://example.com',OP),(SOURCE,'goto'),
    ('native:'+'B'*64,OP),('native:'+'b'*63,OP)])
def test_no_arbitrary_source_or_operation(tmp_path,source,op):
    box,_,_=make(tmp_path)
    with pytest.raises(NativeRecoveryMailboxError):box.submit(source,op)
    assert list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('change',[
    lambda v:v.update(run_id='c'*32),lambda v:v.update(request_id='d'*32),
    lambda v:v.update(operation='execute_model'),lambda v:v.update(source_id='private text'),
    lambda v:v.update(created_at=True),lambda v:v.update(created_at=1001),
    lambda v:v.update(expires_at=1301),lambda v:v.update(version=True),
    lambda v:v.update(extra='not allowed'),lambda v:v.update(expires_at=float('nan'))])
def test_claimed_invalid_shape_run_scope_ttl_finite_fail_closed(tmp_path,change):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP);rewrite(box,request,change)
    result=box.process_one(lambda *_:pytest.fail('invalid'))
    assert result['status']=='rejected' and result['code']=='native_recovery_request_invalid'
    assert (box.path/(request['request_id']+'.claimed')).exists()


def test_duplicate_json_keys_are_not_last_value_wins(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP)
    original=pending(box,request).read_text();pending(box,request).write_text(original[:-1]+',"version":1}')
    assert box.process_one(lambda *_:pytest.fail('duplicate'))['code']=='native_recovery_request_invalid'


def test_expired_request_and_slow_control_read_never_call_handler(tmp_path):
    box,control,clock=make(tmp_path);request=box.submit(SOURCE,OP)
    count=[0]
    def slow():
        count[0]+=1
        if count[0]==2:clock[0]+=301
    control.hook=slow
    assert box.process_one(lambda *_:pytest.fail('expired'))['code']=='native_recovery_request_expired'
    assert result_file(box,request).exists()


def test_handler_crash_keeps_claimed_unknown_and_never_replays(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP)
    class SimulatedCrash(BaseException):pass
    with pytest.raises(SimulatedCrash):box.process_one(lambda *_:(_ for _ in ()).throw(SimulatedCrash()))
    assert (box.path/(request['request_id']+'.claimed')).exists() and not result_file(box,request).exists()
    reopened=NativeRecoveryMailbox(tmp_path,RUN,box.control,clock=box._clock)
    assert reopened.process_one(lambda *_:pytest.fail('unknown retry')) is None


def test_regular_exception_is_fixed_and_never_written_as_text(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP)
    result=box.process_one(lambda *_:(_ for _ in ()).throw(RuntimeError('PRIVATE INPUT')))
    assert result['code']=='native_recovery_handler_failed'
    assert 'PRIVATE INPUT' not in result_file(box,request).read_text()


def test_claimed_or_result_is_an_immutable_replay_fence(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP);body=pending(box,request).read_bytes()
    box.process_one(lambda *_:{'ok':True});before=result_file(box,request).read_bytes()
    pending(box,request).write_bytes(body)
    assert box.process_one(lambda *_:pytest.fail('duplicate')) is None
    assert result_file(box,request).read_bytes()==before


def test_old_run_directory_not_scanned(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP)
    old=tmp_path/'runs'/('c'*32)/'native-review';old.mkdir(parents=True);(old/'evil.json').write_text('private')
    assert box.process_one(lambda *_:{'ok':True})['status']=='done'
    assert (old/'evil.json').read_text()=='private'


def test_foreign_existing_directory_is_not_adopted(tmp_path):
    box,_,_=make(tmp_path);box.path.mkdir(parents=True);(box.path/'foreign.txt').write_text('retain')
    with pytest.raises(NativeRecoveryMailboxError,match='marker_missing'):box.submit(SOURCE,OP)
    assert (box.path/'foreign.txt').read_text()=='retain' and not (box.path/'.owner.json').exists()


def test_oversized_file_is_not_claimed_or_read(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP);pending(box,request).write_bytes(b'x'*1025)
    with pytest.raises(NativeRecoveryMailboxError,match='file_unsafe'):box.process_one(lambda *_:pytest.fail('oversized'))
    assert pending(box,request).stat().st_size==1025


def test_hardlinked_request_is_refused_without_changing_foreign_file(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP);link=tmp_path/'other.json'
    os.link(pending(box,request),link);before=link.read_bytes()
    with pytest.raises(NativeRecoveryMailboxError,match='file_unsafe'):box.process_one(lambda *_:pytest.fail('hardlink'))
    assert link.read_bytes()==before and pending(box,request).exists()


def test_symlink_request_refused_without_reading_target(tmp_path):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP);pending(box,request).unlink()
    foreign=tmp_path/'foreign.txt';foreign.write_text('PRIVATE')
    try:pending(box,request).symlink_to(foreign)
    except OSError:pytest.skip('host lacks symlink privilege')
    with pytest.raises(NativeRecoveryMailboxError):box.process_one(lambda *_:pytest.fail('symlink'))
    assert foreign.read_text()=='PRIVATE'


@pytest.mark.parametrize('returned',[{'ok':True,'dispatchable':True},{'ok':True,'reviewed':'private'},
    {'ok':False,'code':'PRIVATE'},{'status':'done'}])
def test_result_is_fixed_projection_and_cannot_authorize_dispatch(tmp_path,returned):
    box,_,_=make(tmp_path);request=box.submit(SOURCE,OP)
    result=box.process_one(lambda *_:returned)
    assert result['status']=='rejected' and not result['dispatchable']
    assert 'PRIVATE' not in result_file(box,request).read_text()


def test_at_most_one_intent_drained_and_tmp_files_ignored(tmp_path):
    box,_,_=make(tmp_path);box.submit(SOURCE,OP);box.submit(SOURCE,'resume_interrupted_prequeue')
    (box.path/'unpublished.tmp').write_text('ignore')
    seen=[];box.process_one(lambda *args:seen.append(args) or {'ok':True})
    assert len(seen)==1 and len(list(box.path.glob('*.json')))==2  # marker + one request
