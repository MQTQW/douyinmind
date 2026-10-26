"""Explicit B-view inspection; temporary ledgers and fake UI/providers only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from app.mention_receiver.browser import ReceiverDetailError,ReceiverInterventionRequired
from tests.test_assistant_product_bridge import system,receive,QueryModel,QueryKnowledge,Adapter
from tests.test_assistant_native_recovery_mailbox import make
from tests.test_native_recovery_owner import case as owner_case


def rows(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        data={t:list(db.execute('SELECT * FROM '+t+' ORDER BY rowid')) for t in tables}
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


@pytest.fixture
def case(tmp_path):
    b,a,e,r,m,q,h,c=system(tmp_path)
    c.snapshot=lambda:{'desired_state':c.desired,'state':c.desired,'run_id':'run-test'}
    e['comment_text']='@助手 请根据编程知识库说明Git。'
    task=receive(q,h,r,e);model=QueryModel();source=[True]
    class ReadAdapter(Adapter):
        def __init__(self):
            super().__init__();self.opens=[];self.reads=0;self.hook=lambda:None
            self.value={'ok':True,'current_receiver_verified':True,'matching_reply_count':1,'existing_matching_reply_count':1}
        def check_intervention(self):pass
        def open_original_from_notifications(self,target,*,max_candidates):
            assert max_candidates==3
            self.opens.append(deepcopy(target))
            self.last_original_lookup={'candidate_limit':3,'candidate_count':1,'checked_count':1,'match_count':1,
                'final_rechecks':1,'final_verified':1,'page_open_count':2,'lookup_stage_index':10,'reason_code':'matched'}
            return {}
        def _reply_state(self,target,text):self.reads+=1;self.hook();return dict(self.value)
    adapter=ReadAdapter()
    bridge=LegacyBridge(r,q,c,agent_path=a.path,conversation=h,database=tmp_path/'unused-business.db',
        model_factory=lambda:model,knowledge_factory=lambda _:QueryKnowledge(),adapter=adapter,
        source_authorizer=lambda task:source[0])
    runtime=AssistantRuntime(q,c,execute=bridge.execute,publish=bridge.publish)
    assert runtime.execute_once()['state']=='ready_to_publish'
    published=runtime.publish_once();assert published['state']=='b_view_visible'
    c.desired='paused'
    return SimpleNamespace(bridge=bridge,adapter=adapter,queue=q,agent=a,control=c,model=model,
        task=published,registry=r,migrated=m,source=source)


def deny_effects(case,monkeypatch):
    def denied(*a,**k):pytest.fail('inspection entered a write/model/publish path')
    for name in ('get_task','get_reply','lookup_existing','record_reply_readback','list_steps'):
        monkeypatch.setattr(case.bridge.agent,name,denied)
    for name in ('prepare_reply','submit_reply','save_reply_evidence'):
        monkeypatch.setattr(case.adapter,name,denied)
    for name in ('reconcile','publish','execute'):monkeypatch.setattr(case.bridge,name,denied)
    monkeypatch.setattr(case.model,'complete',denied)


def test_fresh_read_of_old_b_visible_keeps_every_task_and_reply_row_unchanged(case,monkeypatch):
    before=(rows(case.agent.path),rows(case.queue.path));paid=len(case.model.requests)
    deny_effects(case,monkeypatch)
    result=case.bridge.inspect_existing_reply(case.task['id'])
    assert result=={'source_id':'assistant-task:'+case.task['id'],'status':'assistant_reply_inspected','dispatchable':False}
    assert len(case.adapter.opens)==1 and case.adapter.reads==1 and case.adapter.submits==1
    diagnostic=case.bridge.last_reply_inspection_diagnostic
    assert diagnostic['b_view_visible_now'] is True and diagnostic['a_visibility_verified'] is False
    assert diagnostic['scope']=='b_view_only' and diagnostic['lookup_counts']['page_open_count']==2
    assert len(case.model.requests)==paid and (rows(case.agent.path),rows(case.queue.path))==before


@pytest.mark.parametrize('counts',[(0,0),(1,2),(2,2)])
def test_zero_or_ambiguous_current_DOM_does_not_rewrite_old_success(case,counts):
    case.adapter.value.update(matching_reply_count=counts[0],existing_matching_reply_count=counts[1])
    before=(rows(case.agent.path),rows(case.queue.path))
    case.bridge.inspect_existing_reply(case.task['id'])
    assert case.bridge.last_reply_inspection_diagnostic['b_view_visible_now'] is False
    assert case.bridge.last_reply_inspection_diagnostic['code']=='reply_not_observed'
    assert case.queue.get_task(case.task['id'])['state']=='b_view_visible'
    assert (rows(case.agent.path),rows(case.queue.path))==before


@pytest.mark.parametrize('problem',['wrong_link','changed_target','no_gate','no_send_token','wrong_sha','registry_revoked','source_revoked','running'])
def test_bad_frozen_scope_or_authority_never_reads_page(case,problem):
    if problem=='wrong_link':
        with case.queue._tx() as db:db.execute('UPDATE assistant_tasks SET agent_task_id=? WHERE id=?',('f'*32,case.task['id']))
    elif problem=='registry_revoked':case.registry.revoke_grant(case.migrated['grant_id'])
    elif problem=='source_revoked':case.source[0]=False
    elif problem=='running':case.control.desired='running'
    else:
        with case.agent._tx() as db:
            reply=db.execute('SELECT * FROM agent_replies WHERE task_id=?',(case.task['agent_task_id'],)).fetchone()
            if problem=='no_send_token':db.execute('UPDATE agent_replies SET send_token=NULL WHERE task_id=?',(case.task['agent_task_id'],))
            elif problem=='changed_target':
                target=json.loads(reply['target']);target['video_id']='7999999000000000999'
                db.execute('UPDATE agent_replies SET target=? WHERE task_id=?',(json.dumps(target),case.task['agent_task_id']))
            else:
                evidence=json.loads(reply['evidence'])
                evidence['submit_gate_passed']=False if problem=='no_gate' else evidence['submit_gate_passed']
                if problem=='wrong_sha':evidence['reply_sha256']='0'*64
                db.execute('UPDATE agent_replies SET evidence=? WHERE task_id=?',(json.dumps(evidence),case.task['agent_task_id']))
    before=(rows(case.agent.path),rows(case.queue.path))
    with pytest.raises(ValueError):case.bridge.inspect_existing_reply(case.task['id'])
    assert case.adapter.opens==[] and (rows(case.agent.path),rows(case.queue.path))==before


def test_wrong_thread_and_task_namespace_do_not_open(case):
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(ValueError):pool.submit(case.bridge.inspect_existing_reply,case.task['id']).result()
    with pytest.raises(ValueError):case.bridge.inspect_existing_reply('assistant-task:'+case.task['id'])
    assert not case.adapter.opens


def test_revocation_after_read_and_challenge_leave_barrier_unchanged(case):
    before=(rows(case.agent.path),rows(case.queue.path))
    case.adapter.hook=lambda:case.source.__setitem__(0,False)
    with pytest.raises(ValueError):case.bridge.inspect_existing_reply(case.task['id'])
    assert (rows(case.agent.path),rows(case.queue.path))==before
    case.source[0]=True
    def challenge():raise ReceiverInterventionRequired('synthetic challenge; do not persist')
    case.adapter.check_intervention=challenge
    with pytest.raises(ReceiverInterventionRequired):case.bridge.inspect_existing_reply(case.task['id'])
    assert (rows(case.agent.path),rows(case.queue.path))==before


def test_lookup_failure_records_fixed_counts_without_raw_exception(case):
    def failed(*args,**kwargs):
        case.adapter.last_original_lookup={'candidate_count':0,'page_open_count':1,'reason_code':'original_lookup_unavailable',
            'secret':'DO-NOT-SAVE','checked_count':True,'exception_message':'DO-NOT-SAVE'}
        raise ReceiverDetailError('DO-NOT-SAVE',{})
    case.adapter.open_original_from_notifications=failed
    before=(rows(case.agent.path),rows(case.queue.path))
    with pytest.raises(ValueError):case.bridge.inspect_existing_reply(case.task['id'])
    diagnostic=case.bridge.last_reply_inspection_diagnostic
    assert diagnostic['lookup_counts']=={'candidate_count':0,'page_open_count':1}
    assert diagnostic['exception_type']=='ReceiverDetailError' and 'DO-NOT-SAVE' not in json.dumps(diagnostic)
    assert (rows(case.agent.path),rows(case.queue.path))==before


def test_mailbox_task_namespace_is_separate_and_once(tmp_path):
    box,_,_=make(tmp_path);task='assistant-task:'+'c'*32
    for source,op in [('native:'+'c'*64,'inspect_reply'),('native-hint:'+'c'*64,'inspect_reply'),
                      (task,'inspect_visibility_hint'),(task,'recover_visibility_hint')]:
        with pytest.raises(Exception):box.submit(source,op)
    box.submit(task,'inspect_reply');seen=[]
    def inspect(op,source):seen.append((op,source));return {'ok':True,'queued_for_fresh_ui':False,'dispatchable':False}
    result=box.process_one(inspect)
    assert result['code']=='assistant_reply_inspected' and result['queued_for_fresh_ui'] is False
    assert result['no_model'] and result['no_import'] and result['no_send']
    assert box.process_one(inspect) is None and seen==[('inspect_reply',task)]


def test_owner_uses_same_bound_bridge_callback_with_paused_flags_and_fixedB(owner_case):
    case=owner_case;seen=[];writes=[];task='c'*32
    case.owner.control.queue=SimpleNamespace(update_control=lambda fn:writes.append(fn(dict(case.state))))
    case.lane._recovery_identity=lambda:seen.append('fixedB')
    class Inspector:
        last_reply_inspection_diagnostic={'stage':'reply_inspect_complete','code':'reply_visible_now','scope':'b_view_only',
            'b_view_visible_now':True,'a_visibility_verified':False,'reply_sha256':'1'*64,
            'observed_at_utc':'2026-10-26T00:00:00+00:00','reply_counts':{'matching_reply_count':1},'private':'DO-NOT-SAVE'}
        def inspect(self,raw):
            assert raw==task and case.flags[0].is_set() and case.flags[1].is_set()
            self.last_reply_inspection_diagnostic={**type(self).last_reply_inspection_diagnostic}
            seen.append('inspect');return {'source_id':'assistant-task:'+raw,'status':'assistant_reply_inspected','dispatchable':False}
    case.owner.reply_inspector=Inspector().inspect
    result=case.owner('inspect_reply','assistant-task:'+task)
    assert result['ok'] is True and result['queued_for_fresh_ui'] is False and seen==['fixedB','inspect']
    assert case.state['desired_state']=='paused' and not case.flags[0].is_set()
    assert writes[0]['native_recovery']['diagnostic']['a_visibility_verified'] is False
    assert 'DO-NOT-SAVE' not in json.dumps(writes)


def test_identity_failure_cannot_reuse_previous_positive_task_diagnostic(owner_case):
    case=owner_case;writes=[]
    case.owner.control.queue=SimpleNamespace(update_control=lambda fn:writes.append(fn(dict(case.state))))
    class Inspector:
        last_reply_inspection_diagnostic={'stage':'reply_inspect_complete','code':'reply_visible_now',
            'b_view_visible_now':True,'reply_sha256':'1'*64,'a_visibility_verified':False}
        def inspect(self,*args):pytest.fail('identity failure cannot open page')
    inspector=Inspector();case.owner.reply_inspector=inspector.inspect
    def fail():raise ReceiverInterventionRequired('do not save this error')
    case.lane._recovery_identity=fail
    result=case.owner('inspect_reply','assistant-task:'+'c'*32)
    assert result['ok'] is False
    assert writes[0]['native_recovery']['diagnostic']['b_view_visible_now'] is False
    assert 'reply_sha256' not in writes[0]['native_recovery']['diagnostic']
    assert writes[0]['native_recovery']['diagnostic']['stage']=='reply_inspect_identity'


def test_CLI_raw32_only_registers_inspection_intent(tmp_path,monkeypatch):
    from app.assistant import __main__ as main
    from app.assistant import native_recovery_mailbox as mailbox
    task='c'*32;calls=[]
    monkeypatch.setattr(main,'STATE',tmp_path)
    monkeypatch.setattr(main,'AssistantQueue',lambda *a,**k:object())
    monkeypatch.setattr(main,'AssistantControl',lambda *a,**k:SimpleNamespace(snapshot=lambda:{'run_id':'a'*32}))
    monkeypatch.setattr(main,'start_guard',lambda:None)
    class Box:
        def __init__(self,*args):pass
        def submit(self,source,op):calls.append((source,op));return {'status':'queued'}
    monkeypatch.setattr(mailbox,'NativeRecoveryMailbox',Box)
    assert main.main(['--state-dir',str(tmp_path),'--inspect-assistant-reply',task])==0
    assert calls==[('assistant-task:'+task,'inspect_reply')]
