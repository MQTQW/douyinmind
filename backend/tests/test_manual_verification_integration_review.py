"""Independent manual-pump/control checks, fake owner and temporary SQLite only."""
from copy import deepcopy
from types import SimpleNamespace
import json
import re
import threading

import pytest

from app.assistant.__main__ import pump_manual_verification
from app.assistant.control import AssistantControl
from app.assistant.manual_verification_owner import ManualVerificationOwner
from app.assistant.manual_verification import ManualVerificationServer
from app.assistant.queue import AssistantQueue, QueueError
from app.assistant.runtime import AssistantRuntime


RUN='a'*32


class MemoryControl:
    def __init__(self):
        self.value={'run_id':RUN,'state':'paused','desired_state':'paused',
            'process_alive':True,'worker_state':'idle','receiver_state':'verification_required'}
        self.events=[]
        self.queue=SimpleNamespace(update_control=self.update)
        self.paused=threading.Event()
    def snapshot(self):return deepcopy(self.value)
    def update(self,fn):self.value=fn(deepcopy(self.value));return deepcopy(self.value)
    def pause(self):
        self.events.append('pause');self.value['desired_state']='paused';self.paused.set()
    def heartbeat(self,run_id):
        assert run_id==RUN
        self.events.append('heartbeat');self.value['state']=self.value['desired_state']


class FakeServer:
    def __init__(self,state='completed',active_after=False):
        self.active=True;self.final=state;self.active_after=active_after;self.calls=[]
        self.finalization_pending=True;self.epoch=1
    @property
    def session_active(self):return self.active
    @property
    def owner_finalization_pending(self):return self.finalization_pending
    @property
    def finalization_epoch(self):return self.epoch
    def acknowledge_finalization(self,expected_epoch):
        if expected_epoch!=self.epoch or self.active:return False
        self.finalization_pending=False;return True
    def process_one(self,handler):
        self.calls.append('process_one');handler('manual-input',{})
        self.active=self.active_after
    def safe_status(self):return {'state':self.final,'active':self.active}


def test_no_manual_session_does_not_change_ordinary_pause():
    control=MemoryControl();hold=threading.Event();server=FakeServer();server.active=False
    server.finalization_pending=False
    assert pump_manual_verification(server,None,control,RUN,threading.RLock(),hold) is False
    assert control.events==[] and not hold.is_set()


@pytest.mark.parametrize('terminal',['completed','cancelled','expired'])
def test_terminal_manual_cycle_drops_a_start_accepted_during_handler_and_remains_paused(terminal):
    control=MemoryControl();hold=threading.Event();server=FakeServer(terminal)
    seen=[]
    def owner(action,payload):
        assert hold.is_set()
        seen.append(action)
        control.value['desired_state']='running'  # Older already-loaded API accepts a concurrent Start.
    assert pump_manual_verification(server,owner,control,RUN,threading.RLock(),hold) is True
    assert seen==['manual-input']
    assert control.value['desired_state']==control.value['state']=='paused'
    assert not hold.is_set() and control.value['manual_verification']['active'] is False
    assert control.events.count('pause')==2


def test_active_session_retains_the_independent_hold_even_when_old_start_changes_control():
    control=MemoryControl();hold=threading.Event();server=FakeServer('active',active_after=True)
    def owner(*_):control.value['desired_state']='running'
    assert pump_manual_verification(server,owner,control,RUN,threading.RLock(),hold) is True
    assert hold.is_set() and control.value['manual_verification']['active'] is True
    # Deliberately do not claim the old API's field is atomically immutable.
    assert control.value['desired_state']=='running'


def test_owner_drain_waits_for_the_same_admission_lock_before_browser_handler():
    control=MemoryControl();hold=threading.Event();server=FakeServer()
    lock=threading.RLock();entered=threading.Event();failures=[]
    lock.acquire()
    def run():
        try:pump_manual_verification(server,lambda *_:entered.set(),control,RUN,lock,hold)
        except BaseException as error:failures.append(type(error).__name__)
    thread=threading.Thread(target=run)
    try:
        thread.start();assert control.paused.wait(1)
        assert not entered.is_set()  # Existing admitted work still owns the lock.
    finally:lock.release()
    thread.join(1)
    assert not thread.is_alive() and failures==[] and entered.is_set()


def test_unexpected_owner_failure_does_not_clear_the_effect_hold():
    control=MemoryControl();hold=threading.Event();server=FakeServer()
    def failed(*_):raise RuntimeError('synthetic')
    with pytest.raises(RuntimeError):pump_manual_verification(server,failed,control,RUN,threading.RLock(),hold)
    assert hold.is_set() and control.value['desired_state']=='paused'


def test_future_loaded_control_rejects_start_with_active_manual_record(tmp_path):
    queue=AssistantQueue(tmp_path/'queue.sqlite3')
    queue.update_control(lambda value:{**value,'run_id':RUN,'pid':123,'state':'paused',
        'desired_state':'paused','manual_verification':{'active':True}})
    before=queue.get_control();spawned=[]
    control=AssistantControl(queue,tmp_path,process_probe=lambda _:True,
        popen_factory=lambda *_args,**_kwargs:spawned.append(True))
    with pytest.raises(QueueError,match='manual_verification_active'):control.start()
    assert spawned==[] and queue.get_control()==before


def test_already_dispatched_ingestion_does_not_deadlock_manual_begin_or_get_repeated():
    control=MemoryControl()
    page=SimpleNamespace(context=object(),url='https://www.douyin.com/user/self',is_closed=lambda:False)
    adapter=SimpleNamespace(page=page)
    queue=SimpleNamespace(counts=lambda:{'waiting_ingestion':1})
    owner=ManualVerificationOwner(adapter,SimpleNamespace(),control,queue,RUN)
    assert owner.allowed()['queue_worker_busy'] is False
    assert owner('begin',{})=={'ok':True}
    reconciled=[]
    runtime=AssistantRuntime(queue,control,reconcile=lambda task:reconciled.append(task))
    assert runtime.poll_waiting_once() is None and reconciled==[]


def test_owner_rechecks_operation_after_a_blocking_queue_read():
    control=MemoryControl();active=[True]
    page=SimpleNamespace(context=object(),url='https://www.douyin.com/user/self',is_closed=lambda:False)
    def counts():active[0]=False;return {}
    owner=ManualVerificationOwner(SimpleNamespace(page=page),SimpleNamespace(),control,
        SimpleNamespace(counts=counts),RUN,operation_current=lambda:active[0])
    with pytest.raises(ValueError,match='verification_operation_expired'):owner('begin',{})


def test_cancelled_begin_before_first_pump_still_requires_owner_pause_acknowledgement():
    control=MemoryControl();hold=threading.Event()
    server=ManualVerificationServer(RUN,allowed=lambda:{**control.snapshot(),
        'publication_busy':False,'queue_worker_busy':False})
    headers={'host':'127.0.0.1:8767','origin':server.origin,'content-type':'application/json'}
    code,_,body=server.request('GET','/',headers);assert code==200
    headers['x-douyinmind-manual']=re.search(rb'name="manual-nonce" content="([a-f0-9]{64})"',body)[1].decode()
    code,_,body=server.request('POST','/api/begin',headers,b'{}');assert code==202
    session=json.loads(body)['session_id']
    code,_,_=server.request('POST','/api/action',headers,json.dumps({
        'session_id':session,'action':'cancel','payload':{}}).encode())
    assert code==200 and server.safe_status()['state']=='cancelled'
    control.value['desired_state']='running'  # An old loaded API accepted Start during the short manual request.
    owner_calls=[]
    assert pump_manual_verification(server,lambda *_:owner_calls.append(True),control,RUN,threading.RLock(),hold) is True
    assert control.value['desired_state']=='paused' and owner_calls==[]
    assert not server.owner_finalization_pending and not hold.is_set()
    assert control.value['manual_verification']['active'] is False


def test_new_finalization_during_owner_ack_keeps_the_hold_for_another_cycle():
    control=MemoryControl();hold=threading.Event();server=FakeServer()
    original=server.acknowledge_finalization
    def newer(expected):
        server.epoch+=1;server.finalization_pending=True
        return original(expected)
    server.acknowledge_finalization=newer
    assert pump_manual_verification(server,lambda *_:None,control,RUN,threading.RLock(),hold) is True
    assert server.owner_finalization_pending and hold.is_set()
    assert control.value['desired_state']=='paused'
