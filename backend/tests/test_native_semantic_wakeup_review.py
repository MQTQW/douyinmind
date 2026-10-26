"""Lossy internal wakeup, real temp receipts/Source/Registry, no provider/network."""
import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import threading
import pytest

from app.assistant import native_lane as lane_module
from app.assistant.native_semantic_inbox import NativeSemanticInbox,native_message_source_key
from app.assistant.semantic_plan_store import SemanticPlanStore
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.mention_agent.model import ModelTurn
from tests.test_assistant_native_visibility_lane import case
from tests.test_assistant_native_receiver import page,B
from tests.test_semantic_planning_store import Budget


def helpers():
    path=Path(lane_module.__file__).with_name('__main__.py')
    tree=ast.parse(path.read_text(encoding='utf-8'))
    names={'receive_bounded_batch','signal_native_ui_wakeup','native_ui_receipt_current','receive_native_ui_wakeup'}
    body=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    ns={'__name__':'app.assistant._isolated_wakeup_review','__package__':'app.assistant'}
    exec(compile(ast.Module(body=body,type_ignores=[]),str(path),'exec'),ns)
    return SimpleNamespace(**{name:ns[name] for name in names}),tree


@pytest.fixture
def ready(case,tmp_path):
    h,_=helpers();c=case
    text='@助手 根据编程知识库解释Git的新概念'
    raw=c.raw('12001','22001',text,created='101')
    projection=c.project(page([raw]),9)
    c.source.record_page(c.source.prepare_channel(9,'601'),projection,next_cursor=('0','0'),has_more=False,allow_live=True)
    message=projection['candidates'][0]
    c.clock[0]=100.5
    inbox=NativeSemanticInbox.prepare(tmp_path/'meaning-inbox.sqlite3',B,c.binding.receiver_profile_url,
        clock=lambda:c.clock[0],source_authorizer=c.source.native_meaning_checkpoint)
    inbox.enable_new_messages();c.clock[0]=102
    [key]=inbox.record_messages([message],page_fingerprint=projection['fingerprint'])
    store=SemanticPlanStore.prepare(tmp_path/'meaning.sqlite3',clock=lambda:c.clock[0])
    class Planner:
        calls=0
        def complete(self,*args):
            self.calls+=1
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'fake-meaning-once',
                'type':'function','function':{'name':'submit_intent','arguments':json.dumps({
                    'action':'query','target_state':'one','grant_id':c.migrated['grant_id']})}}]},
                {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')
    planner=Planner();interpreter=SemanticIntentInterpreter(c.registry,store,planner,Budget())
    interpreter.configure_native(inbox,c.source.native_meaning_checkpoint,source_store=c.source)
    assert interpreter.process_native_once('test-tools')['state']=='understood'
    c.lane.semantic_intake_ready=interpreter.native_intake_ready
    c.lane.semantic_candidate_action=interpreter.native_candidate_action
    c.lane.semantic_ui_source_ready=lambda candidate:h.native_ui_receipt_current(interpreter,candidate)
    c.lane.browser_lane.semantic_resolution_provider=interpreter
    c.queue.update_control(lambda old:{**old,'run_id':c.lane.run_id,'state':'running','desired_state':'running'})
    return SimpleNamespace(c=c,h=h,inbox=inbox,store=store,planner=planner,interpreter=interpreter,
        key=key,message=message,event=threading.Event())


def consume(r,*,stop=lambda:False):
    def receive():return [r.c.queue.enqueue(**row) for row in r.c.lane.collect()]
    return r.h.receive_native_ui_wakeup(r.event,SimpleNamespace(receive_once=receive),r.c.lane,threading.RLock(),stop)


def test_readiness_is_readonly_with_actual_complete_owned_receipt(ready):
    r=ready;paths=(r.c.source.path,r.inbox.path,r.store.path,r.c.queue.path,r.c.registry.path)
    before={p:p.read_bytes() for p in paths}
    assert r.c.lane.ready_persisted_ui_source() is True
    assert before=={p:p.read_bytes() for p in paths}
    assert r.planner.calls==1 and r.c.receiver.calls==0 and r.c.adapter.native_calls==[]


def test_one_ready_signal_admits_once_without_second_model_api_capture_or_capture_record(ready):
    r=ready;r.h.signal_native_ui_wakeup(r.event,{'state':'understood','source_key':r.key})
    [task]=consume(r)
    row=r.c.source.get_source(r.key)
    with r.c.source._connection() as db:attempts=db.execute('SELECT attempts FROM sources WHERE source_id=?',(r.key,)).fetchone()[0]
    assert row['state']=='admitted' and attempts==1 and row['task_id']==task['id']
    assert len(r.c.adapter.native_calls)==1 and r.planner.calls==1 and r.c.receiver.calls==0
    r.event.set();assert consume(r)==[] and not r.event.is_set()
    with r.c.source._connection() as db:attempts=db.execute('SELECT attempts FROM sources WHERE source_id=?',(r.key,)).fetchone()[0]
    assert attempts==1 and len(r.c.adapter.native_calls)==1 and r.planner.calls==1


@pytest.mark.parametrize('problem',['paused','stopped','old_run','stop','manual','pending_ui',
    'held','claimed','historical','admitted','old_identity','revoked','missing_receipt','dict_receipt',
    'inbox_conflict','source_conflict','catalog_changed','no_semantic'])
def test_hint_cannot_bypass_current_owner_receipt_or_source_gates(ready,monkeypatch,problem):
    r=ready;c=r.c
    if problem in {'paused','stopped'}:c.queue.update_control(lambda old:{**old,'state':problem,'desired_state':problem})
    elif problem=='old_run':c.queue.update_control(lambda old:{**old,'run_id':'other-owner'})
    elif problem=='stop':c.lane.should_stop=lambda:True
    elif problem=='manual':
        r.event.set();assert consume(r,stop=lambda:True)==[];assert not r.event.is_set();return
    elif problem=='pending_ui':c.lane._pending['unhanded-proof']={}
    elif problem in {'held','claimed','historical','admitted'}:
        with c.source._connection(write=True) as db:db.execute('UPDATE sources SET state=? WHERE source_id=?',(problem,r.key))
    elif problem=='old_identity':c.clock[0]+=121
    elif problem=='revoked':c.registry.revoke_user(c.migrated['user_id'])
    elif problem=='missing_receipt':monkeypatch.setattr(r.store,'get_native_decision',lambda key:None)
    elif problem=='dict_receipt':monkeypatch.setattr(r.store,'get_native_decision',lambda key:{'action':'query','state':'complete'})
    elif problem=='inbox_conflict':
        m={**r.message,'content':r.message['content']+' changed'};r.inbox.record_messages([m],page_fingerprint='b'*64)
    elif problem=='source_conflict':
        m=c.raw('12002','22001',r.message['content']+' changed',created='101')
        c.source.record_page(c.source.prepare_channel(9,'601'),c.project(page([m]),9),next_cursor=('0','0'),has_more=False,allow_live=True)
    elif problem=='catalog_changed':
        context=r.interpreter._context(r.message);u,d,g=deepcopy(context);g[0]['name']='different current catalog'
        monkeypatch.setattr(r.interpreter,'_context',lambda message:(u,d,g))
    elif problem=='no_semantic':c.lane.semantic_ui_source_ready=None
    assert c.lane.ready_persisted_ui_source() is False
    r.event.set();assert consume(r)==[]
    assert r.planner.calls==1 and c.receiver.calls==0 and c.adapter.native_calls==[]


@pytest.mark.parametrize('outcome',[None,{},True,{'state':'held'},{'state':'pending'}])
def test_only_internal_understood_result_signals(outcome):
    h,_=helpers();event=threading.Event();h.signal_native_ui_wakeup(event,outcome)
    assert not event.is_set()


def test_wakeup_preserves_three_candidate_batch_limit():
    h,_=helpers();event=threading.Event();event.set();calls=[];admitted=[]
    lane=SimpleNamespace(ready_persisted_ui_source=lambda:True,has_buffered_notices=True,
        admitted=lambda rows:admitted.extend(rows),clear_buffer=lambda:None)
    runtime=SimpleNamespace(receive_once=lambda:calls.append(len(calls)) or [len(calls)])
    assert h.receive_native_ui_wakeup(event,runtime,lane,threading.RLock(),lambda:False)==[1,2,3]
    assert calls==[0,1,2] and admitted==[1,2,3]


def test_pause_race_after_ready_probe_still_prevents_claim(ready,monkeypatch):
    r=ready;original=r.c.lane.ready_persisted_ui_source
    def race():
        valid=original()
        r.c.queue.update_control(lambda old:{**old,'state':'paused','desired_state':'paused'})
        return valid
    monkeypatch.setattr(r.c.lane,'ready_persisted_ui_source',race)
    r.event.set();assert consume(r)==[]
    assert r.c.source.get_source(r.key)['state']=='fresh'
    with r.c.source._connection() as db:
        assert db.execute('SELECT attempts FROM sources WHERE source_id=?',(r.key,)).fetchone()[0]==0
    assert r.c.adapter.native_calls==[] and r.planner.calls==1


@pytest.mark.parametrize('human',[False,True])
def test_actual_wakeup_error_branch_preserves_capture_deadline_and_propagates_human_pause(human):
    h,tree=helpers()
    gate=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.unparse(n.test)=='now >= next_capture')
    branch=gate.orelse[0];assert ast.unparse(branch.test)=='native_ui_wakeup.is_set()'
    module=ast.Module(body=[branch],type_ignores=[]);ast.fix_missing_locations(module)
    event=threading.Event();event.set();health=[]
    failure=lane_module.ReceiverInterventionRequired('synthetic challenge') if human else RuntimeError('synthetic page race')
    def fail():raise failure
    lane=SimpleNamespace(ready_persisted_ui_source=lambda:True,admitted=lambda rows:None,clear_buffer=lambda:None)
    ns={'native_ui_wakeup':event,'runtime':SimpleNamespace(receive_once=fail),'lane':lane,
        'admission_lock':threading.RLock(),'page_operations_stop':lambda:False,
        'receive_native_ui_wakeup':h.receive_native_ui_wakeup,'next_capture':45,'run_id':'fake',
        'ReceiverInterventionRequired':lane_module.ReceiverInterventionRequired,
        'control':SimpleNamespace(set_receiver_health=lambda *a,**k:health.append(k['reason_code']))}
    code=compile(module,'actual-wakeup-error-branch','exec')
    if human:
        with pytest.raises(lane_module.ReceiverInterventionRequired):exec(code,ns)
        assert health==[]
    else:
        exec(code,ns);assert health==['native_ui_wakeup_unconfirmed']
    assert not event.is_set() and ns['next_capture']==45


@pytest.mark.parametrize('lost_event',[False,True])
def test_actual_owner_schedule_wakes_ui_without_more_polling_or_capture_timestamp(lost_event,monkeypatch):
    h,tree=helpers()
    gate=next(n for n in ast.walk(tree) if isinstance(n,ast.If) and ast.unparse(n.test)=='now >= next_capture')
    wrapped=ast.Module(body=[ast.For(target=ast.Name(id='_once',ctx=ast.Store()),
        iter=ast.List(elts=[ast.Constant(1)],ctx=ast.Load()),body=[gate],orelse=[])],type_ignores=[])
    ast.fix_missing_locations(wrapped);code=compile(wrapped,'actual-capture-gate','exec')
    from app.assistant import native_visibility_diagnostics
    monkeypatch.setattr(native_visibility_diagnostics,'record_visibility_probe',lambda *a:None)
    clock=SimpleNamespace(now=0);event=threading.Event();polls=[];collects=[];captures=[];pending=[True]
    lane=SimpleNamespace(poll_native=lambda:polls.append(clock.now) or {'status':{'armed':True}},
        ready_persisted_ui_source=lambda:pending[0] and clock.now>=2,has_buffered_notices=False,
        admitted=lambda rows:None,clear_buffer=lambda:None)
    def receive():
        collects.append(clock.now)
        if clock.now>=2:pending[0]=False
        return []
    control=SimpleNamespace(record_capture=lambda *a,**k:captures.append(clock.now),
        set_receiver_health=lambda *a,**k:None)
    ns={'__name__':'app.assistant._isolated_wakeup_schedule','__package__':'app.assistant',
        'lane':lane,'control':control,'run_id':'synthetic','runtime':SimpleNamespace(receive_once=receive),
        'time':SimpleNamespace(monotonic=lambda:clock.now),'next_capture':0,'poll_seconds':45,
        'native_ui_wakeup':event,'admission_lock':threading.RLock(),'should_stop':lambda:False,
        'page_operations_stop':lambda:False,'ReceiverInterventionRequired':lane_module.ReceiverInterventionRequired,
        'receive_bounded_batch':h.receive_bounded_batch,'receive_native_ui_wakeup':h.receive_native_ui_wakeup}
    for now in (0,2,10,44.75,45):
        clock.now=now;ns['now']=now
        if now==2 and not lost_event:h.signal_native_ui_wakeup(event,{'state':'understood'})
        exec(code,ns)
        if now==2:assert ns['next_capture']==45
    assert polls==captures==[0,45]
    assert collects==([0,45] if lost_event else [0,2,45])
    assert pending==[False]
