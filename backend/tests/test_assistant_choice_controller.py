"""Controller integration with real temporary ledgers and synthetic UI facts."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
import threading
import time

import pytest

from app.assistant.choice_controller import ChoiceController
from app.assistant.commands import CommandResolver
from app.assistant.conversation import ConversationStore
from app.assistant.library_choice import LibraryChoiceStore,DATABASE_NAME
from app.assistant.queue import AssistantQueue
from app.assistant.registry import Registry
from app.mention_agent.choice_evidence import choice_ask_reference
from app.mention_agent.contracts import Binding
from app.mention_agent.store import AgentStore
from app.mention_receiver.browser import ReceiverDetailError,ReceiverInterventionRequired
from tests.library_choice_fixture import selection
from tests.test_mention_agent_store import envelope


A='https://www.douyin.com/user/controller-A'
B='https://www.douyin.com/user/controller-B'


@pytest.fixture
def case(tmp_path):
    clock=[time.time()];catalog=[{'id':7,'name':'编程库'},{'id':8,'name':'生活库'}]
    registry=Registry(tmp_path/'registry.sqlite3',catalog_reader=lambda:deepcopy(catalog),clock=lambda:clock[0])
    prior=registry.import_legacy(Binding('controller-legacy','controller-instance',A,B,7,True),verified_by='synthetic-owner',allow_import=True)
    registry.set_local_owner_access(prior['user_id'],prior['device_id'],query_all_local=True,catalog_read_allowed=True,
        public_reply_allowed=True,grant_current_libraries=True,verified_by='synthetic-owner')
    queue=AssistantQueue(tmp_path/'queue.sqlite3');agent=AgentStore(tmp_path/'agent.sqlite3')
    history=ConversationStore(tmp_path/'conversation.sqlite3');history.prepare_scope_v2()
    store=LibraryChoiceStore.prepare(tmp_path/DATABASE_NAME,clock=lambda:clock[0]);stopped=[False]
    controller=ChoiceController(registry,queue,agent,store,conversation=history,should_stop=lambda:stopped[0],
        admission_lock=threading.RLock(),clock=lambda:clock[0])
    return SimpleNamespace(clock=clock,catalog=catalog,registry=registry,prior=prior,queue=queue,agent=agent,
        history=history,store=store,controller=controller,stopped=stopped)


def root(case,n=1):
    env=envelope(sender_profile_url=A,receiver_profile_url=B,comment_text='@助手 请保存这个视频。',
        reference_id='controller-root-'+str(n),command_id='controller-root-command-'+str(n))
    resolution=CommandResolver(case.registry).resolve(env)
    task=case.queue.enqueue(resolution.binding,env,kind='clarify',user_id=resolution.user_id,
        device_id=resolution.device_id,authorization=resolution.authorization)
    case.history.bind_task(task['id'],resolution.binding,env,env['reference_id'],
        {'quality':'current_comment','root_reference_id':env['reference_id']})
    session=case.controller.prepare_ask(task,resolution)
    assert session is not None
    return task,session,resolution


def draft(case,task,session):
    case.agent.put_binding(task['binding']);old=case.agent.enqueue(task['binding_id'],task['envelope'])
    claim=case.agent.claim(old['task_id'])
    if claim:case.agent.complete(old['task_id'],claim['claim_token'],
        {'kind':'clarification','text':session['ask_text'],'source_ids':[],'sources':[]})
    return old['task_id']


def publish_fact(case,task,session,*,outcome='b_view_visible',age=0,gate=True,body=True):
    identity=draft(case,task,session);env=task['envelope']
    verification={key:env[key] for key in ('sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id','native_comment_id')}
    verification.update(real_mention=True,unique_count=1,current_receiver_verified=True,
        observed_at_utc=datetime.now(timezone.utc).isoformat())
    reserved=case.agent.reserve_send(identity,verification);assert reserved is not None
    evidence={'submit_gate_passed':gate,'exact_body_in_original_thread':body,
        'reply_sha256':sha256(session['ask_text'].encode()).hexdigest(),
        'observed_at_utc':datetime.fromtimestamp(case.clock[0]-age,timezone.utc).isoformat()}
    case.agent.finish_send(identity,reserved['send_token'],outcome,evidence)
    case.controller.after_publish(task,{'state':outcome})
    return identity


def ask_proof(case,session):
    binding=session['root_binding'];env=session['root_envelope']
    ref=choice_ask_reference(root_reference_id=env['reference_id'],ask_ref=session['ask_ref'],ask_text=session['ask_text'],
        receiver_profile_url=binding['receiver_profile_url'],video_id=env['video_id'])
    return {'source_kind':'library_choice_ask','sender_profile_url':A,'receiver_profile_url':B,
        'ask_author_profile_url':B,'receiver_verified':True,'video_id':env['video_id'],
        'root_reference_id':env['reference_id'],'root_comment_text':env['comment_text'],'ask_ref':session['ask_ref'],
        'ask_reply_ref':ref,'ask_text':session['ask_text'],'unique_count':1,'exact_body_in_original_thread':True,
        'reference_quality':'verified_ui','native_comment_id':None,
        'observed_at_utc':datetime.fromtimestamp(case.clock[0],timezone.utc).isoformat()}


def choice_detail(case,session,*,text='2',target=8,native=None):
    binding=case.registry.authorize_snapshot(next(item['authorization'] for item in session['options'] if item['knowledge_base_id']==target),action='import')
    proxy=SimpleNamespace(root_binding=Binding.from_dict(session['root_binding']),root_envelope=session['root_envelope'],
        selected_bindings={target:binding},created=session,ask_ref=session['ask_ref'],ask_reply_ref=ask_proof(case,session)['ask_reply_ref'],
        ask_text=session['ask_text'],clock=lambda:case.clock[0])
    env=selection(proxy,text=text,target=target,native=native)
    return {'ok':True,'unique_count':1,'sender_profile_url':A,'receiver_profile_url':B,'video_id':env['video_id'],
        'source_url':'https://www.douyin.com/video/'+env['video_id'],'command_text':text,'native_comment_id':native,
        'native_comment_id_conflict':False,'evidence':env['evidence']}


class Adapter:
    expected_profile_url=B
    def __init__(self,case,*,text='2',target=8,details=True):
        self.case=case;self.text=text;self.target=target;self.details=details;self.last_choice_ask_proof=None
        self.reads=[];self.current_reads=[];self.error=None;self.challenge=False;self.on_read=None
        self.keys=[];self.page=SimpleNamespace(keyboard=SimpleNamespace(press=self.keys.append))
    def check_intervention(self):
        if self.challenge:raise ReceiverInterventionRequired('synthetic challenge')
    def read_choice_replies(self,session,allowed,*,keep_open=False):
        assert keep_open is True
        self.reads.append(session['choice_id'])
        if self.error:raise self.error
        self.last_choice_ask_proof=ask_proof(self.case,session)
        value=[choice_detail(self.case,session,text=self.text,target=self.target)] if self.details else []
        if self.on_read:self.on_read()
        return value
    def observe_current_choice_replies(self,session,allowed):
        self.current_reads.append(session['choice_id'])
        if self.error:raise self.error
        return [choice_detail(self.case,session,text=self.text,target=self.target)]


def admit(case,events):
    tasks=[case.queue.enqueue(**event) for event in events];case.controller.admitted(tasks);return tasks


def test_prepare_freezes_real_menu_without_model_or_import_and_replay_keeps_one_ask(case):
    task,session,resolution=root(case)
    assert '2个知识库' in session['ask_text'] and '编程库' in session['ask_text'] and '生活库' in session['ask_text']
    assert session['root_binding']['knowledge_base_id'] is None and session['options'][1]['ordinal']==2
    assert case.agent.list_tasks()==[] and case.queue.list_tasks()['total']==1
    assert case.controller.prepare_ask(task,resolution)['choice_id']==session['choice_id']
    assert case.store.get_for_root_task(task['id'])['state']=='awaiting_ask'


@pytest.mark.parametrize('age,gate,body',[(500,True,True),(0,False,True),(0,True,False)])
def test_incomplete_or_stale_durable_publish_fact_never_claims_new_ask_observation(case,age,gate,body):
    task,session,_=root(case);publish_fact(case,task,session,age=age,gate=gate,body=body)
    assert case.store.get(session['choice_id'])['state']=='awaiting_ask'
    assert case.store.get(session['choice_id'])['ask_reply_ref'] is None


def test_actual_durable_ask_readback_keeps_original_timestamp_and_never_resends(case):
    task,session,_=root(case);draft(case,task,session)
    assert case.controller.before_publish(task)
    publish_fact(case,task,session)
    waiting=case.store.get(session['choice_id']);assert waiting['state']=='waiting_library'
    assert waiting['ask_proof']['observed_at_utc']==case.agent.get_reply(case.agent.list_tasks()[0]['task_id'])['evidence']['observed_at_utc']
    assert not case.controller.before_publish(task)
    decorated=case.controller.decorate_result(task,{'state':'b_view_visible','details':{},'send_token_present':True})
    assert decorated['state']=='waiting_library' and decorated['details']['library_choice']['option_count']==2


def test_unknown_ask_is_read_only_reconciled_before_actual_plain_selection_child(case):
    task,session,_=root(case);publish_fact(case,task,session,outcome='delivery_unknown')
    assert case.store.get(session['choice_id'])['state']=='ask_unknown' and not case.controller.before_publish(task)
    adapter=Adapter(case);events=case.controller.collect_pending(adapter,'fixture-run')
    assert len(adapter.reads)==1 and len(events)==1
    assert set(events[0])=={'binding','envelope','kind','video_title','user_id','device_id','authorization'}
    assert events[0]['envelope']['comment_text']=='2' and events[0]['envelope']['evidence']['real_mention'] is False
    child=admit(case,events)[0];session=case.store.get(session['choice_id'])
    assert session['state']=='handed_off' and session['child_task_id']==child['id']
    resolved=case.controller.resolution_for_selection(child)
    assert resolved.action=='save' and resolved.binding.knowledge_base_id==8
    assert set(resolved.choice_intent)=={'choice_id','root_comment_text','selection_reference_id','selected_library_name'}
    assert case.history.task_scope(child['id'])['thread_id']==task['envelope']['reference_id']
    assert len(case.agent.list_tasks())==1 and case.agent.get_task_ingestion(case.agent.list_tasks()[0]['task_id']) is None
    root_result=case.controller.decorate_result(task,{'state':'b_view_visible','details':{'reply_text':session['ask_text']}})
    assert root_result['state']=='b_view_visible' and root_result['details']['reply_text']==session['ask_text']


def test_ready_unsubmitted_ask_never_opens_browser_or_becomes_unknown(case):
    task,session,_=root(case);draft(case,task,session)
    adapter=Adapter(case);assert case.controller.collect_pending(adapter,'fixture-run')==[]
    assert adapter.reads==[] and case.store.get(session['choice_id'])['state']=='awaiting_ask'


def test_native_wakeup_only_reads_selected_session_not_an_older_pending_root(case):
    old_task,old,_=root(case,1);publish_fact(case,old_task,old)
    new_task,new,_=root(case,2);publish_fact(case,new_task,new)
    case.store.defer_waiting_recheck(new['choice_id'],seconds=600)
    adapter=Adapter(case)
    events=case.controller.collect_pending(adapter,'native-wakeup',choice_id=new['choice_id'])
    assert adapter.reads==[new['choice_id']] and len(events)==1
    assert events[0]['envelope']['evidence']['choice_id']==new['choice_id']
    assert case.store.get(old['choice_id'])['state']=='waiting_library'
    assert case.store.get(new['choice_id'])['state']=='handoff_claimed'


def test_native_wakeup_unknown_id_never_falls_back_to_old_pending_thread(case):
    task,session,_=root(case);publish_fact(case,task,session)
    adapter=Adapter(case)
    assert case.controller.collect_pending(adapter,'native-wakeup',choice_id='a'*64)==[]
    assert adapter.reads==[] and adapter.keys==[]
    assert case.store.get(session['choice_id'])['state']=='waiting_library'


@pytest.mark.parametrize('choice_id',[True,{},'', 'arbitrary-user-input', 'A'*64])
def test_native_wakeup_rejects_invalid_session_id_without_browser(case,choice_id):
    adapter=Adapter(case)
    with pytest.raises(ValueError):
        case.controller.collect_pending(adapter,'native-wakeup',choice_id=choice_id)
    assert adapter.reads==[] and adapter.keys==[]


def test_native_wakeup_does_not_recover_another_sessions_handoff(case):
    old_task,old,_=root(case,1);publish_fact(case,old_task,old)
    old_events=case.controller.collect_pending(Adapter(case),'before')
    assert len(old_events)==1
    case.controller.clear_buffer();case.clock[0]+=31
    new_task,new,_=root(case,2);publish_fact(case,new_task,new)
    adapter=Adapter(case)
    events=case.controller.collect_pending(adapter,'native-wakeup',choice_id=new['choice_id'])
    assert adapter.reads==[new['choice_id']] and len(events)==1
    assert events[0]['envelope']['evidence']['choice_id']==new['choice_id']
    assert case.store.get(old['choice_id'])['state']=='handoff_claimed'


def test_menu_change_holds_original_intent_instead_of_upgrading_frozen_options(case):
    task,session,_=root(case);draft(case,task,session)
    case.catalog.append({'id':9,'name':'后来新建库'})
    assert not case.controller.before_publish(task)
    held=case.store.get(session['choice_id']);assert held['state']=='needs_review' and held['options']==session['options']
    assert case.controller.decorate_result(task,{'state':'answer_ready','details':{}})['state']=='needs_review'


def test_authorization_changed_during_dom_read_never_selects_or_enqueues_child(case):
    task,session,_=root(case);publish_fact(case,task,session)
    adapter=Adapter(case)
    adapter.on_read=lambda:case.registry.set_local_owner_access(case.prior['user_id'],case.prior['device_id'],
        query_all_local=False,catalog_read_allowed=False,public_reply_allowed=False,verified_by='synthetic-owner')
    assert case.controller.collect_pending(adapter,'fixture-run')==[]
    assert case.store.get(session['choice_id'])['state']=='needs_review'
    assert case.queue.list_tasks()['total']==1


def test_original_cancellation_blocks_selected_child_even_when_library_grant_still_exists(case):
    task,session,_=root(case);publish_fact(case,task,session)
    child=admit(case,case.controller.collect_pending(Adapter(case),'fixture-run'))[0]
    with case.queue._tx() as db:db.execute("UPDATE assistant_tasks SET state='cancelled' WHERE id=?",(task['id'],))
    assert case.controller.resolution_for_selection(child) is None
    assert not case.controller.before_publish(child)
    assert case.store.get(session['choice_id'])['state']=='handed_off'


def test_enqueued_child_crash_before_admitted_recovers_actual_source_without_new_task(case):
    task,session,_=root(case);publish_fact(case,task,session)
    event=case.controller.collect_pending(Adapter(case),'run-before')[0]
    child=case.queue.enqueue(**event);assert case.history.task_scope(child['id']) is None
    case.controller.clear_buffer();case.clock[0]+=31
    recovered=ChoiceController(case.registry,case.queue,case.agent,case.store,conversation=case.history,
        clock=lambda:case.clock[0])
    adapter=Adapter(case);assert recovered.collect_pending(adapter,'run-after')==[]
    assert adapter.reads==[] and case.queue.list_tasks()['total']==2
    assert case.store.get(session['choice_id'])['child_task_id']==child['id']
    assert case.history.task_scope(child['id']) is not None


def test_not_enqueued_handoff_crash_reuses_same_public_selection_after_lease(case):
    task,session,_=root(case);publish_fact(case,task,session)
    old=case.controller.collect_pending(Adapter(case),'run-before')[0]
    case.controller.clear_buffer();case.clock[0]+=31
    events=case.controller.collect_pending(Adapter(case),'run-after')
    assert len(events)==1 and events[0]['envelope']==old['envelope']
    assert case.queue.list_tasks()['total']==1
    assert admit(case,events)[0]['envelope']['comment_text']=='2'


def test_one_bad_root_defers_and_next_root_is_available_after_restart(case):
    first,one,_=root(case);publish_fact(case,first,one)
    second,two,_=root(case,2);publish_fact(case,second,two)
    adapter=Adapter(case);adapter.error=ReceiverDetailError('synthetic vanished root',{})
    assert case.controller.collect_pending(adapter,'fixture-run')==[] and len(adapter.reads)==1
    first_read=adapter.reads[0]
    adapter.error=None;adapter.details=False
    assert case.controller.collect_pending(adapter,'fixture-run')==[]
    assert adapter.reads[1]!=first_read


def test_human_verification_or_stop_never_defers_or_holds_pending_root(case):
    task,session,_=root(case);publish_fact(case,task,session)
    before=case.store.get(session['choice_id']);adapter=Adapter(case);adapter.challenge=True
    with pytest.raises(ReceiverInterventionRequired):case.controller.collect_pending(adapter,'fixture-run')
    assert case.store.get(session['choice_id'])==before
    case.stopped[0]=True
    assert case.controller.collect_pending(adapter,'fixture-run')==[]
    assert case.store.get(session['choice_id'])==before


def test_known_selection_requires_current_exact_root_proof_and_never_body_cover_guess(case):
    task,session,_=root(case);publish_fact(case,task,session)
    adapter=Adapter(case);admit(case,case.controller.collect_pending(adapter,'fixture-run'))
    current=case.store.get(session['choice_id']);detail=choice_detail(case,current)
    notice={'source_profile_url':A,'exact_text':'2','semantic_fingerprint':'some-cover'}
    assert not case.controller.known_selection_notice(notice,None,adapter)
    assert case.controller.known_selection_notice(notice,detail,adapter)
    assert len(adapter.current_reads)==1
    assert not case.controller.known_selection_notice({**notice,'source_profile_url':'https://www.douyin.com/user/foreign'},detail,adapter)
    adapter.observe_current_choice_replies=lambda *_:[]
    assert not case.controller.known_selection_notice(notice,detail,adapter)


def test_selection_task_never_resolves_by_ordinary_default_query_parser(case):
    task,session,_=root(case);publish_fact(case,task,session)
    child=admit(case,case.controller.collect_pending(Adapter(case),'fixture-run'))[0]
    altered=deepcopy(child);altered['authorization']['grant_version']+=1
    assert case.controller.resolution_for_selection(altered) is None
    altered=deepcopy(child);altered['envelope']['evidence']['ask_reply_ref']='another-ask'
    assert case.controller.resolution_for_selection(altered) is None
    assert case.controller.resolution_for_selection(child).binding.knowledge_base_id==8


def test_missing_durable_choice_never_allows_an_untracked_eligible_menu_publish(case):
    env=envelope(sender_profile_url=A,receiver_profile_url=B,comment_text='@助手 请保存这个视频。')
    res=CommandResolver(case.registry).resolve(env)
    task=case.queue.enqueue(res.binding,env,kind='clarify',user_id=res.user_id,device_id=res.device_id,authorization=res.authorization)
    assert case.store.get_for_root_task(task['id']) is None
    assert not case.controller.before_publish(task)


def test_unknown_reconciliation_replays_do_not_advance_choice_or_reopen_send(case):
    task,session,_=root(case);publish_fact(case,task,session,outcome='delivery_unknown')
    original=case.store.get(session['choice_id'])
    for _ in range(3):case.controller.decorate_result(task,{'state':'delivery_unknown','details':{}})
    assert case.store.get(session['choice_id'])==original
    assert not case.controller.before_publish(task)


def test_stop_does_not_turn_waiting_permission_into_permanent_review(case):
    task,session,_=root(case);publish_fact(case,task,session)
    original=case.store.get(session['choice_id']);case.stopped[0]=True
    assert not case.controller.before_publish(task)
    assert case.controller.decorate_result(task,{'state':'b_view_visible','details':{}})['state']=='waiting_library'
    assert case.store.get(session['choice_id'])==original


def test_another_actual_root_with_identical_choice_text_is_not_swallowed(case):
    task,session,_=root(case);publish_fact(case,task,session)
    adapter=Adapter(case);admit(case,case.controller.collect_pending(adapter,'fixture-run'))
    task2,session2,_=root(case,2);publish_fact(case,task2,session2)
    current2=case.store.get(session2['choice_id']);foreign_detail=choice_detail(case,current2)
    adapter.observe_current_choice_replies=lambda *_:[foreign_detail]
    notice={'source_profile_url':A,'exact_text':'2','semantic_fingerprint':'same-video-cover'}
    assert not case.controller.known_selection_notice(notice,foreign_detail,adapter)


def test_selected_grant_version_change_is_not_upgraded_or_published_as_waiting(case):
    task,session,_=root(case);publish_fact(case,task,session)
    event=case.controller.collect_pending(Adapter(case),'fixture-run')[0]
    child=case.queue.enqueue(**event)
    grant=event['authorization']
    case.registry.grant_library(grant['user_id'],grant['device_id'],8,can_query=True,can_import=True,public_reply_allowed=False)
    assert case.controller.resolution_for_selection(child) is None
    value=case.controller.decorate_result(task,{'state':'b_view_visible','details':{}})
    assert value['state']=='needs_review' and value['safe_to_publish'] is False
    assert case.store.get(session['choice_id'])['state']=='handoff_claimed'


def test_known_selected_source_remains_deduplicated_after_permission_revoke(case):
    task,session,_=root(case);publish_fact(case,task,session)
    adapter=Adapter(case);child=admit(case,case.controller.collect_pending(adapter,'fixture-run'))[0]
    saved=case.store.get(session['choice_id']);detail=choice_detail(case,saved)
    adapter.observe_current_choice_replies=lambda *_:[deepcopy(detail)]
    grant=child['authorization']
    case.registry.grant_library(grant['user_id'],grant['device_id'],8,can_query=False,can_import=False,public_reply_allowed=False)
    notice={'source_profile_url':A,'exact_text':'2','semantic_fingerprint':'public-cover'}
    assert case.controller.known_selection_notice(notice,detail,adapter)
    assert case.controller.resolution_for_selection(child) is None


def test_child_cancellation_cannot_continue_from_a_stale_claimed_object(case):
    task,session,_=root(case);publish_fact(case,task,session)
    child=admit(case,case.controller.collect_pending(Adapter(case),'fixture-run'))[0]
    assert case.controller.resolution_for_selection(child) is not None
    with case.queue._tx() as db:db.execute("UPDATE assistant_tasks SET state='cancelled' WHERE id=?",(child['id'],))
    assert case.controller.resolution_for_selection(child) is None
    assert not case.controller.before_publish(child)


@pytest.mark.parametrize('interruption',['stop','challenge'])
def test_stop_or_challenge_appearing_after_read_preserves_modal_and_recheck_state(case,interruption):
    task,session,_=root(case);publish_fact(case,task,session);before=case.store.get(session['choice_id'])
    adapter=Adapter(case)
    def change():
        if interruption=='stop':case.stopped[0]=True
        else:adapter.challenge=True
    adapter.on_read=change
    if interruption=='stop':assert case.controller.collect_pending(adapter,'fixture-run')==[]
    else:
        with pytest.raises(ReceiverInterventionRequired):case.controller.collect_pending(adapter,'fixture-run')
    assert adapter.keys==[] and case.store.get(session['choice_id'])==before
    assert case.store.list_waiting(B)[0]['next_check_at']==0


def test_authority_gate_remains_usable_after_reservation_and_closes_on_catalog_change(case):
    task,session,_=root(case);identity=draft(case,task,session)
    env=task['envelope'];proof={key:env[key] for key in ('sender_profile_url','receiver_profile_url','video_id','comment_text','reference_id')}
    proof.update(real_mention=True,unique_count=1,current_receiver_verified=True,observed_at_utc=datetime.now(timezone.utc).isoformat())
    assert case.controller.before_publish(task) and case.controller.authority_current(task)
    assert case.agent.reserve_send(identity,proof) is not None
    assert not case.controller.before_publish(task) and case.controller.authority_current(task)
    case.catalog[0]['name']='重新命名后的编程库'
    assert not case.controller.authority_current(task)


def test_existing_child_fact_is_recovered_after_revoke_without_authorizing_new_work(case):
    task,session,_=root(case);publish_fact(case,task,session)
    event=case.controller.collect_pending(Adapter(case),'before-crash')[0]
    child=case.queue.enqueue(**event);case.controller.clear_buffer()
    auth=child['authorization']
    case.registry.grant_library(auth['user_id'],auth['device_id'],8,can_query=False,can_import=False,public_reply_allowed=False)
    case.clock[0]+=31
    adapter=Adapter(case)
    assert case.controller.collect_pending(adapter,'after-crash')==[] and adapter.reads==[]
    assert case.store.get(session['choice_id'])['child_task_id']==child['id']
    assert case.history.task_scope(child['id']) is not None and case.queue.list_tasks()['total']==2
    assert not case.controller.authority_current(child)
    assert case.controller.resolution_for_selection(child) is None


def test_body_only_detail_from_another_frame_cannot_borrow_known_root_parent(case):
    task,session,_=root(case);publish_fact(case,task,session)
    adapter=Adapter(case);admit(case,case.controller.collect_pending(adapter,'fixture-run'))
    known=case.store.get(session['choice_id']);old_frame_detail=choice_detail(case,known)
    # A normal detail from Root Y carries no exact parent. After a frame swap,
    # the current observer can truthfully see the old selection under Root X.
    old_frame_detail['evidence']={'sender_verified':True,'video_verified':True,'real_mention':True,'unique_count':1}
    notice={'source_profile_url':A,'exact_text':'2','semantic_fingerprint':'same-video-cover'}
    assert not case.controller.known_selection_notice(notice,old_frame_detail,adapter)
    assert case.controller.selection_notice_requires_recheck(notice,old_frame_detail)
    assert not case.controller.selection_notice_requires_recheck({**notice,'exact_text':'另一个问题'},old_frame_detail)
