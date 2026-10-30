"""V1 saves/Choice through real temp ledgers; fake source capture/model/platform."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import time

import pytest

from app.assistant.clarification_sessions import ClarificationSessionStore,DATABASE_NAME,ClarificationError
from app.assistant.clarification_continuation import ClarificationContinuation
from app.assistant.legacy_bridge import LegacyBridge
from app.assistant.runtime import AssistantRuntime
from app.assistant.native_lane import NativeLane
from app.assistant.semantic_plan_store import SemanticPlanStore
from app.assistant.semantic_routing import SemanticIntentInterpreter
from app.assistant.usage_budget import UsageBudget
from app.mention_agent.model import ModelTurn
from tests.test_assistant_choice_controller import case,A,B
from tests.test_conversation_followup_evidence import detail as physical_detail
from tests.test_semantic_library_choice import SemanticAdapter
from tests.test_mention_agent_store import envelope
from tests.test_mention_agent_import_end_to_end import Backend,Adapter


def child_detail(anchor,*,text):
    detail=physical_detail();root=anchor['root_envelope'];native='7999999000000000092'
    detail.update(sender_profile_url=root['sender_profile_url'],receiver_profile_url=root['receiver_profile_url'],
        video_id=root['video_id'],command_text=text,native_comment_id=native,video_title='合成视频')
    detail['evidence'].update(instance_id=anchor['instance_id'],sender_profile_url=root['sender_profile_url'],
        receiver_profile_url=root['receiver_profile_url'],video_id=root['video_id'],
        root_task_id=anchor['root_task_id'],root_agent_task_id=anchor['root_agent_task_id'],
        root_reference_id=root['reference_id'],root_comment_text=root['comment_text'],
        root_native_comment_id=root['native_comment_id'],prior_reply_text=anchor['prior_reply_text'],
        prior_reply_sha256=anchor['prior_reply_sha256'],followup_text=text,native_comment_id=native,
        observed_at_utc=datetime.now(timezone.utc).isoformat())
    return detail


class SourceLedger:
    """Only synthetic capture facts; production NativeSourceStore is unchanged."""
    def __init__(self,path):
        self.path=Path(path)
        with sqlite3.connect(path) as db:
            db.execute('''CREATE TABLE sources(source_id TEXT PRIMARY KEY,state TEXT,task_id TEXT,
                reference_id TEXT,historical INTEGER,first_seen REAL,attempts INTEGER,candidate TEXT,plan TEXT)''')
    @contextmanager
    def _connection(self):
        db=sqlite3.connect(self.path);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        finally:db.close()
    def add(self,sid,candidate,*,task=None,state='claimed',historical=False,attempts=1):
        with self._connection() as db:
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?)',(sid,state,
                task['id'] if task else None,task['envelope']['reference_id'] if task else None,
                int(historical),time.time(),attempts,json.dumps(candidate),json.dumps(self._plan(task)) if task else None))
    @staticmethod
    def _plan(task):
        item={key:deepcopy(task[key]) for key in ('kind','binding','authorization','user_id','device_id','envelope')}
        return {'reference_id':task['envelope']['reference_id'],'envelope':deepcopy(task['envelope']),'queue_item':item}
    def admit(self,sid,task):
        with self._connection() as db:
            db.execute("UPDATE sources SET state='admitted',task_id=?,reference_id=?,plan=? WHERE source_id=?",
                (task['id'],task['envelope']['reference_id'],json.dumps(self._plan(task)),sid))
    @staticmethod
    def _view(row):
        if row is None:return None
        value=dict(row);value['historical']=bool(value['historical'])
        value['candidate']=json.loads(value['candidate']);value['plan']=json.loads(value['plan']) if value['plan'] else None
        return value
    def find_sources_by_ui_reference(self,reference):
        with self._connection() as db:return [self._view(row) for row in db.execute('SELECT * FROM sources WHERE reference_id=?',(reference,))]
    def lookup_source_for_task(self,task_id):
        with self._connection() as db:rows=list(db.execute('SELECT * FROM sources WHERE task_id=?',(task_id,)))
        return self._view(rows[0]) if len(rows)==1 else None
    def admitted_source_current(self,sid):
        with self._connection() as db:row=self._view(db.execute('SELECT * FROM sources WHERE source_id=?',(sid,)).fetchone())
        return bool(row and row['state']=='admitted' and row['historical'] is False and row['plan'])


class Planner:
    def __init__(self):self.decision={'action':'clarify','target_state':'unspecified','grant_id':None};self.calls=[];self.hook=lambda:None;self.error=None
    def complete(self,messages,tools):
        self.calls.append((deepcopy(messages),deepcopy(tools)));self.hook()
        if self.error:raise self.error
        return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'synthetic-intent',
            'type':'function','function':{'name':'submit_intent','arguments':json.dumps(self.decision)}}]},
            {'prompt_tokens':12,'completion_tokens':8,'total_tokens':20},'tool_calls')


class Platform(Adapter):
    def __init__(self):super().__init__();self.outcome='b_view_visible'
    def prepare_reply(self,target,text):
        proof=target.get('clarification_evidence') or target.get('choice_evidence')
        if proof is None:return super().prepare_reply(target,text)
        return {**deepcopy(proof),**{key:target[key] for key in ('sender_profile_url','receiver_profile_url',
            'video_id','comment_text','reference_id','native_comment_id')},'current_receiver_verified':True,
            'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    def submit_reply(self,target,text,*,before_submit):
        before_submit();self.submits+=1
        return {'outcome':self.outcome,'exact_body_in_original_thread':self.outcome=='b_view_visible'}


@pytest.fixture
def flow(case,tmp_path):
    c=case
    c.controller.clock=time.time
    c.store.clock=time.time
    c.registry.offline_seconds=86400  # Synthetic device stays online during guarded slow I/O.
    store=ClarificationSessionStore.prepare(tmp_path/DATABASE_NAME,enabled=True,clock=lambda:time.time()-1)
    meaning=SemanticPlanStore.prepare(tmp_path/'meaning.sqlite3');budget=UsageBudget.prepare(tmp_path/'usage-budget.sqlite3')
    planner=Planner();interpreter=SemanticIntentInterpreter(c.registry,meaning,planner,budget)
    sources=SourceLedger(tmp_path/'synthetic-capture.sqlite3')
    env=envelope(sender_profile_url=A,receiver_profile_url=B,comment_text='@助手 帮我处理这条合成视频。',
        native_comment_id='7999999000000000091',reference_quality='native',reference_id='v1-future-original',command_id='v1-future-command')
    resolution=interpreter(env,source_checkpoint=lambda:True)
    assert resolution.reason_code=='semantic_intent_unclear'
    task=c.queue.enqueue(resolution.binding,env,kind=resolution.action,user_id=resolution.user_id,
        device_id=resolution.device_id,authorization=resolution.authorization)
    c.history.bind_task(task['id'],resolution.binding,env,env['reference_id'],
        {'quality':'current_comment','root_reference_id':env['reference_id']})
    sources.add('native:'+'a'*64,{'native_comment_id':env['native_comment_id']},task=task,state='admitted')
    backend=Backend();platform=Platform();save_calls=[];save_target=[7]
    class SaveModel:
        def complete(self,messages,tools):
            save_calls.append(1)
            return ModelTurn({'role':'assistant','content':None,'tool_calls':[{'id':'save-synthetic',
                'type':'function','function':{'name':'save_current_video',
                    'arguments':json.dumps({'knowledge_base_id':save_target[0]})}}]},
                {'prompt_tokens':4,'completion_tokens':3,'total_tokens':7},'tool_calls')
    def knowledge(binding):
        name=next(item['name'] for item in c.registry.read_catalog() if item['id']==binding.knowledge_base_id) if hasattr(c.registry,'read_catalog') else ('编程库' if binding.knowledge_base_id==7 else '生活库')
        return SimpleNamespace(describe=lambda:{'id':binding.knowledge_base_id,'name':name,'total_count':0,'ready_count':0},
            query=lambda _:pytest.fail('v1 save must not query or embed'))
    control=SimpleNamespace(snapshot=lambda:{'desired_state':'running','run_id':'synthetic-v1-run'})
    bridge=LegacyBridge(c.registry,c.queue,control,agent_path=c.agent.path,conversation=c.history,
        database=tmp_path/'never-open-business.sqlite3',adapter=platform,import_client=backend,
        model_factory=SaveModel,knowledge_factory=knowledge,choice_controller=c.controller,
        semantic_store=meaning,usage_budget=budget,admission_lock=c.controller.admission_lock)
    c.controller.semantic_root_resolution_provider=interpreter.recorded_resolution
    c.controller.semantic_choice_receipt_loader=meaning.get_recorded_decision
    c.controller.semantic_choice_planner=lambda **kw:meaning.run_once(**kw,model=planner,budget=budget)
    lane=NativeLane.__new__(NativeLane)
    lane.browser_lane=SimpleNamespace(pending={},choice_controller=c.controller)
    lane.source_store=sources;lane.queue=c.queue;lane.registry=c.registry;lane.choice_controller=c.controller
    lane.followup_coordinator=None;lane.visibility_hint_store=None
    lane.clarification_coordinator=None
    coordinator=ClarificationContinuation(store,bridge,lane,interpreter)
    lane.clarification_coordinator=coordinator
    # These are the REAL application authority methods. Only capture/UI and
    # provider effects are simulated, not the dispatcher permission spine.
    interpreter.native_inbox=SimpleNamespace(lookup=lambda _:None)
    interpreter.native_source_store=sources
    source_checks=[];semantic_checks=[]
    def authority(task):
        source_checks.append(task['id'])
        if lane.authority_current(task) is not True:return False
        semantic_checks.append(task['id'])
        return interpreter.native_authority_current(task) is True
    bridge.source_authorizer=authority
    bridge.clarifications=coordinator;c.controller.intent_origin_for_task=coordinator.origin_for_task
    runtime=AssistantRuntime(c.queue,control,execute=bridge.execute,publish=bridge.publish,reconcile=bridge.reconcile)
    coordinator.admitted([task])
    assert runtime.execute_once()['state']=='ready_to_publish'
    assert runtime.publish_once()['state']=='b_view_visible'
    session=store.for_root(task['id']);assert session['state']=='awaiting_reply'
    return SimpleNamespace(c=c,store=store,meaning=meaning,budget=budget,planner=planner,interpreter=interpreter,
        sources=sources,root=c.queue.get_task(task['id']),session=session,backend=backend,platform=platform,
        bridge=bridge,coordinator=coordinator,runtime=runtime,save_target=save_target,save_calls=save_calls,
        lane=lane,source_checks=source_checks,semantic_checks=semantic_checks)


def prepare(c,*,text='请保存到编程库。',decision=None,detail_hook=None):
    sid='native:'+'b'*64
    candidate={'source_kind':'native_reply_notice_candidate','sender_profile_url':A,'receiver_profile_url':B,
        'video_id':c.root['envelope']['video_id'],'native_comment_id':'7999999000000000092',
        'parent_id_hint':c.root['envelope']['native_comment_id'],'content':text}
    c.sources.add(sid,candidate)
    c.planner.decision=decision or {'action':'save','target_state':'one','grant_id':c.c.prior['grant_id']}
    reads=[]
    def read(anchor,observed):
        reads.append(1);detail=child_detail(anchor,text=text)
        if detail_hook:detail_hook(detail,len(reads))
        return detail
    result=c.coordinator.prepare({'source_id':sid,'candidate':candidate},checkpoint=lambda:True,read=read)
    return sid,candidate,result,reads


def admit(c,sid,result):
    rows,thread,_,failure=result
    assert failure is None and len(rows)==1
    row=rows[0];task=c.c.queue.enqueue(**row)
    c.c.history.bind_task(task['id'],row['binding'],row['envelope'],thread['root_reference_id'],thread)
    c.sources.admit(sid,task);c.coordinator.admitted([task])
    return task


def test_explicit_library_creates_one_save_without_rewriting_original_clarification(flow):
    c=flow;old=c.c.agent.get_reply(c.root['agent_task_id']);before=deepcopy(old)
    assert c.bridge._followup_root_anchor(c.root['id']) is None
    sid,candidate,result,reads=prepare(c)
    child=admit(c,sid,result)
    assert child['kind']=='save' and child['envelope']['comment_text']==candidate['content']
    assert child['envelope']['evidence']['real_mention'] is False
    assert len(reads)==2 and len(c.planner.calls)==2
    assert c.runtime.execute_once()['state']=='ready_to_publish'
    assert c.runtime.publish_once()['state']=='b_view_visible'
    assert c.backend.posts==1 and len(c.save_calls)==1
    assert c.c.agent.get_reply(c.root['agent_task_id'])==before
    assert c.runtime.execute_once() is None and c.runtime.publish_once() is None
    reopened=ClarificationSessionStore(c.store.path)
    assert reopened.for_root(c.root['id'])['child_task_id']==child['id']
    assert c.coordinator.prepare({'source_id':sid,'candidate':candidate},checkpoint=lambda:True,read=lambda *_:pytest.fail('spent source')) is None
    assert c.bridge._followup_root_anchor(child['id']) is not None


def test_missing_library_creates_new_formal_menu_then_unique_save_child(flow):
    c=flow
    sid,_,result,_=prepare(c,text='请保存这条视频，不需要提问。',
        decision={'action':'save','target_state':'unspecified','grant_id':None})
    menu=admit(c,sid,result)
    assert menu['kind']=='clarify' and menu['id']!=c.root['id']
    assert c.runtime.execute_once()['state']=='ready_to_publish'
    assert c.runtime.publish_once()['state']=='waiting_library'
    choice=c.c.store.get_for_root_task(menu['id'])
    assert choice and choice['state']=='waiting_library'
    assert choice['root_envelope']==c.root['envelope']
    assert c.c.queue.get_task(c.root['id'])['state']=='b_view_visible' and c.backend.posts==0
    c.planner.decision={'action':'save','target_state':'one','grant_id':next(
        option['authorization']['grant_id'] for option in choice['options'] if option['knowledge_base_id']==8)}
    text='请放入生活库。';adapter=SemanticAdapter(c.c,text=text);adapter.native='7999999000000000093'
    c.c.clock[0]=time.time()
    selection={'sender_profile_url':A,'receiver_profile_url':B,'video_id':choice['root_envelope']['video_id'],
        'native_comment_id':adapter.native,'comment_text':text,'choice_id':choice['choice_id'],
        'root_reference_id':choice['root_envelope']['reference_id']}
    rows=c.c.controller.collect_pending(adapter,'synthetic-v1-owner',choice_id=choice['choice_id'],
        source_checkpoint=lambda _:True,selection_candidate=selection)
    assert len(rows)==1 and rows[0]['kind']=='save'
    child=c.c.queue.enqueue(**rows[0]);c.c.controller.admitted([child]);c.save_target[0]=8
    c.sources.add('native:'+'c'*64,{'source_kind':'native_reply_notice_candidate',
        'native_comment_id':adapter.native,'content':text},task=child,state='admitted')
    assert c.lane.authority_current(child) is True
    assert c.interpreter.native_authority_current(child) is True
    bad=deepcopy(child);bad['envelope']['evidence']['root_reference_id']='different-synthetic-root'
    assert c.lane.authority_current(bad) is False
    attached=c.lane.clarification_coordinator;c.lane.clarification_coordinator=None
    assert c.lane.authority_current(child) is False
    c.lane.clarification_coordinator=attached
    assert c.runtime.execute_once()['state']=='ready_to_publish'
    assert c.runtime.publish_once()['state']=='b_view_visible'
    assert c.backend.posts==1 and len(c.save_calls)==1
    linked=c.c.store.get(choice['choice_id'])
    assert linked['child_task_id']==child['id'] and linked['root_task_id']==menu['id']
    assert c.store.for_root(c.root['id'])['child_task_id']==menu['id']
    assert c.c.controller.collect_pending(adapter,'synthetic-v1-owner',choice_id=choice['choice_id'],
        source_checkpoint=lambda _:True,selection_candidate=selection)==[]
    assert c.backend.posts==1
    assert {c.root['id'],menu['id'],child['id']}<=set(c.source_checks)
    assert {c.root['id'],menu['id'],child['id']}<=set(c.semantic_checks)


@pytest.mark.parametrize('decision',[
    {'action':'cancel','target_state':'unspecified','grant_id':None},
    {'action':'clarify','target_state':'ambiguous','grant_id':None},
])
def test_negation_or_ambiguity_is_a_visible_nonexecuting_clarification(flow,decision):
    c=flow;sid,_,result,_=prepare(c,text='先不要保存，目标还没确定。',decision=decision)
    task=admit(c,sid,result)
    assert task['kind']=='clarify'
    assert c.runtime.execute_once()['state']=='ready_to_publish'
    assert c.runtime.publish_once()['state']=='b_view_visible'
    assert c.backend.posts==0 and c.save_calls==[]


@pytest.mark.parametrize('problem',['wrong_root','wrong_video','wrong_sender','old_ui','revoked','body_changed_after_model','unknown_model'])
def test_bad_scope_or_unknown_results_never_import_or_erase_consumption(flow,problem):
    c=flow
    def alter(detail,round_):
        if problem=='wrong_root':detail['evidence']['root_task_id']='9'*32
        elif problem=='wrong_video':detail['video_id']=detail['evidence']['video_id']='7999999000000000099'
        elif problem=='wrong_sender':detail['sender_profile_url']=detail['evidence']['sender_profile_url']='https://www.douyin.com/user/synthetic-foreign'
        elif problem=='old_ui':detail['evidence']['observed_at_utc']=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        elif problem=='body_changed_after_model' and round_==2:
            detail['command_text']=detail['evidence']['followup_text']='不同的新补充'
    if problem=='revoked':c.planner.hook=lambda:c.c.registry.revoke_grant(c.c.prior['grant_id'])
    if problem=='unknown_model':c.planner.error=TimeoutError('synthetic unknown provider result')
    try:
        _,_,result,_=prepare(c,detail_hook=alter)
    except Exception:
        result=([],None,None,'rejected')
    assert result[0]==[] and c.backend.posts==0 and c.save_calls==[]
    assert c.c.queue.list_tasks()['total']==1
    if problem in {'body_changed_after_model','unknown_model'}:
        assert c.store.for_root(c.root['id'])['state']=='responding'
        assert len(c.planner.calls)==2


def test_crash_after_source_admitted_repairs_only_the_existing_queue_session_link(flow):
    c=flow;sid,_,result,_=prepare(c)
    row=result[0][0];task=c.c.queue.enqueue(**row)
    c.c.history.bind_task(task['id'],row['binding'],row['envelope'],result[1]['root_reference_id'],result[1])
    c.sources.admit(sid,task)  # Crash before coordinator/Session handoff.
    assert c.store.for_root(c.root['id'])['state']=='planned'
    source_before=c.sources.path.read_bytes();queue_before=c.c.queue.path.read_bytes()
    agent_before=c.c.agent.path.read_bytes();calls=len(c.planner.calls)
    assert c.coordinator.reconcile_existing()['links_repaired']==1
    assert c.store.for_root(c.root['id'])['child_task_id']==task['id']
    assert c.coordinator.reconcile_existing()['links_repaired']==0
    assert len(c.planner.calls)==calls and c.backend.posts==0 and c.platform.submits==1
    assert c.sources.path.read_bytes()==source_before and c.c.queue.path.read_bytes()==queue_before
    assert c.c.agent.path.read_bytes()==agent_before
    assert c.lane.authority_current(task) is True


def test_crash_after_agent_exact_B_view_projects_publication_without_new_send(flow):
    c=flow
    with c.store._db(write=True) as db:
        db.execute("UPDATE clarification_sessions SET state='awaiting_publication',published_reply=NULL,revision=1 WHERE session_id=?",(c.session['session_id'],))
    snapshots={path:path.read_bytes() for path in (c.sources.path,c.c.queue.path,c.c.agent.path,c.meaning.path,c.budget.path)}
    calls=len(c.planner.calls)
    assert c.coordinator.reconcile_existing()['publications_observed']==1
    assert c.store.for_root(c.root['id'])['state']=='awaiting_reply'
    assert c.coordinator.reconcile_existing()['publications_observed']==0
    assert c.platform.submits==1 and c.backend.posts==0 and len(c.planner.calls)==calls
    assert all(path.read_bytes()==raw for path,raw in snapshots.items())
