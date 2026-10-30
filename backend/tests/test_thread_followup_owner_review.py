"""Independent followup owner/policy review. All data/fees/UI are fake/temp."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
from types import SimpleNamespace

import pytest

from app.assistant.thread_followup import ThreadFollowupCoordinator
from app.assistant.queue import AssistantQueue,QueueError
from app.assistant.native_lane import NativeLane,NativeLaneError
from app.mention_agent.followup_evidence import make_followup_envelope
from app.mention_receiver.browser import ReceiverDetailError,ReceiverInterventionRequired
from tests.test_semantic_command_resolution import case as semantic_case
from tests.test_native_recovery_owner import case as owner_case,SOURCE
from tests.test_assistant_native_lane import native_system
from tests.test_assistant_native_recovery_mailbox import make as mailbox


@pytest.fixture
def case(semantic_case):
    c=semantic_case
    c.model.decision={'action':'query','target_state':'unspecified','grant_id':None}
    binding=c.registry.intake_binding(c.env['sender_profile_url'],c.env['receiver_profile_url'])
    root=deepcopy(c.env);root['reference_id']='reference:published-original'
    root['evidence']={'real_mention':True}
    prior='已按原请求完成保存。';sha=hashlib.sha256(prior.encode()).hexdigest()
    anchor={'root_task_id':'1'*32,'root_agent_task_id':'2'*32,'root_envelope':root,
        'prior_reply_text':prior,'prior_reply_sha256':sha}
    native='7999999000000000019'
    candidate={'source_kind':'native_reply_notice_candidate','sender_profile_url':binding.sender_profile_url,
        'receiver_profile_url':binding.receiver_profile_url,'video_id':c.env['video_id'],
        'content':'这个片段主要说明什么？','native_comment_id':native,'parent_id_hint':'7999999000000000018',
        'namespace':'douyin_web_v1','sender_uid':'8001','recipient_uid':'9001',
        'sender_sec_uid':binding.sender_profile_url.rsplit('/',1)[-1],
        'native_source_key':'native:'+'f'*64,'identity_link_revision':'1',
        'root_comment_id':None,'parent_comment_id':None,'notice_type':31,'comment_type':2}
    proof={'source_kind':'conversation_followup','evidence_version':1,'query_only':True,
        'instance_id':binding.instance_id,'real_mention':False,'sender_verified':True,'receiver_verified':True,
        'video_verified':True,'current_receiver_verified':True,'unique_count':1,'root_chain_verified':True,
        'prior_reply_visible_unique':True,'reply_to_receiver_verified':True,'direct_reply_to_prior':True,
        'parent_scope':'direct_prior_reply','sender_profile_url':binding.sender_profile_url,
        'receiver_profile_url':binding.receiver_profile_url,'video_id':candidate['video_id'],
        'root_task_id':anchor['root_task_id'],'root_agent_task_id':anchor['root_agent_task_id'],
        'root_reference_id':root['reference_id'],'root_comment_text':root['comment_text'],
        'root_native_comment_id':None,'prior_reply_native_comment_id':'7999999000000000018',
        'prior_reply_text':prior,'prior_reply_sha256':sha,'followup_reference_id':'filled-by-builder',
        'followup_text':candidate['content'],'native_comment_id':native,
        'observed_at_utc':datetime.now(timezone.utc).isoformat()}
    detail={'ok':True,'sender_profile_url':binding.sender_profile_url,'receiver_profile_url':binding.receiver_profile_url,
        'video_id':candidate['video_id'],'native_comment_id':native,'command_text':candidate['content'],
        'video_title':'Synthetic public video','evidence':proof}
    anchors=[anchor];current=[True];checks=[True];reads=[]
    bridge=SimpleNamespace(followup_anchors=lambda observed:deepcopy(anchors),
        followup_anchor_current=lambda task,body:current[0] and task==anchor['root_task_id'] and body==sha)
    lane=SimpleNamespace(registry=c.registry,pending={},semantic_resolution_provider=c.router,should_stop=lambda:c.stopped[0])
    coordinator=ThreadFollowupCoordinator(lane,bridge)
    def read(found,observed):reads.append(1);return deepcopy(detail)
    return SimpleNamespace(c=c,binding=binding,anchor=anchor,candidate=candidate,detail=detail,
        anchors=anchors,current=current,checks=checks,reads=reads,lane=lane,coordinator=coordinator,
        read=read,prepare=lambda:coordinator.prepare({'candidate':candidate},checkpoint=lambda:checks[0],read=read))


def test_complete_ordinary_followup_uses_private_query_all_scope_without_AT_fingerprint(case):
    rows,thread,correlation,failure=case.prepare()
    assert failure is None and len(rows)==1 and len(case.c.model.requests)==1
    item=rows[0];env=item['envelope']
    assert item['kind']=='query' and item['binding'].is_all_local and item['binding'].knowledge_base_id is None
    assert env['comment_text']==case.candidate['content'] and env['evidence']['real_mention'] is False
    assert env['evidence']['query_only'] is True and item['notice_fingerprint'] is None
    assert case.lane.pending[env['reference_id']]['fingerprint'] is None
    assert thread=={'quality':'verified_ancestor','root_reference_id':case.anchor['root_envelope']['reference_id']}
    assert correlation['quality']=='native_equal'
    assert case.c.store.get_recorded_decision(env,binding=item['binding']) is not None
    case.lane.pending.clear()
    assert len(case.prepare()[0])==1 and len(case.c.model.requests)==1


@pytest.mark.parametrize('problem',['none','two','crossA','crossB','crossVideo','not_at_root','bad_prior_hash'])
def test_ambiguous_or_cross_scope_anchor_denied_before_reader_and_model(case,problem):
    if problem=='none':case.anchors.clear()
    elif problem=='two':case.anchors.append(deepcopy(case.anchor))
    elif problem=='crossA':case.anchor['root_envelope']['sender_profile_url']='https://www.douyin.com/user/other-A'
    elif problem=='crossB':case.anchor['root_envelope']['receiver_profile_url']='https://www.douyin.com/user/other-B'
    elif problem=='crossVideo':case.anchor['root_envelope']['video_id']='7999999000000000099'
    elif problem=='not_at_root':case.anchor['root_envelope']['evidence']['real_mention']=False
    elif problem=='bad_prior_hash':case.anchor['prior_reply_text']+='changed'
    assert case.prepare()[0]==[]
    assert case.reads==[] and case.c.model.requests==[] and not case.lane.pending


@pytest.mark.parametrize('problem',['old','future','cross_instance','false_at','wrong_video','wrong_cid','bad_anchor_id'])
def test_stale_or_conflicting_actual_UI_proof_never_bills(case,problem):
    p=case.detail['evidence']
    if problem in {'old','future'}:
        p['observed_at_utc']=(datetime.now(timezone.utc)+timedelta(seconds=-121 if problem=='old' else 20)).isoformat()
    elif problem=='cross_instance':p['instance_id']='other-instance'
    elif problem=='false_at':p['real_mention']=True
    elif problem=='wrong_video':case.detail['video_id']='7999999000000000099'
    elif problem=='wrong_cid':case.detail['native_comment_id']='7999999000000000098'
    elif problem=='bad_anchor_id':p['root_task_id']='3'*32
    try:rows=case.prepare()[0]
    except ValueError:rows=[]
    assert rows==[] and case.c.model.requests==[] and not case.lane.pending


def test_source_or_anchor_revocation_denies_before_model(case):
    case.checks[0]=False
    assert case.prepare()[0]==[] and not case.reads and not case.c.model.requests
    case.checks[0]=True;case.current[0]=False
    assert case.prepare()[0]==[] and not case.c.model.requests


def test_missing_private_receipt_cannot_use_old_keyword_resolver(case):
    env=make_followup_envelope(case.detail,case.binding)
    calls=[]
    original=case.c.router.resolver.resolve
    def guarded(*args,**kwargs):
        # A fixed program refusal may reuse the Registry resolver; only the
        # unrestricted old grammar path is forbidden.
        assert kwargs.get('_program_clarification') is not None
        calls.append('fixed_refusal')
        return original(*args,**kwargs)
    case.c.router.resolver.resolve=guarded
    result=case.c.router.recorded_resolution({'id':'a'*32,'envelope':env,'binding':case.binding.to_dict()})
    assert result.action not in {'query','save'} and result.reason_code=='semantic_scope_unavailable'
    assert calls==['fixed_refusal'] and not case.c.model.requests
    case.c.router.native_inbox=SimpleNamespace()
    assert case.c.router.native_authority_current({'envelope':env}) is False


def test_model_save_on_plain_reply_cannot_reopen_import_or_choice(case):
    case.c.model.decision={'action':'save','target_state':'one','grant_id':case.c.grant['grant_id']}
    rows,_,_,_=case.prepare()
    assert len(rows)==1 and rows[0]['kind']=='clarify'
    env=rows[0]['envelope']
    receipt=case.c.store.get_recorded_decision(env,binding=rows[0]['binding'])
    result=case.c.router.resolver.resolve_verified_intent(env,receipt)
    # The verified ordinary-followup tool now has no save action. A provider
    # ignoring that schema is held, rather than receiving any decision receipt.
    assert receipt is None and result.reason_code=='semantic_recorded_decision_required'
    assert result.choice_intent is None
    assert not result.choice_options and len(case.c.model.requests)==1
    from app.assistant.semantic_resolution import semantic_source_key
    recorded=case.c.store.lookup_receipt(semantic_source_key(env))
    assert recorded['state']=='held' and recorded['reason_code']=='semantic_response_invalid'
    assert recorded['usage']['total_tokens']==20 and recorded['dispatchable'] is False


def test_queue_reuses_time_refresh_but_rejects_changed_frozen_anchor_or_quality(case,tmp_path):
    item=case.prepare()[0][0]
    queue=AssistantQueue(tmp_path/'independent-followup-queue.sqlite3')
    first=queue.enqueue(**item)
    changed=deepcopy(item)
    changed['envelope']['evidence']['observed_at_utc']=datetime.now(timezone.utc).isoformat()
    assert queue.enqueue(**changed)['id']==first['id']
    for key in ('root_task_id','root_agent_task_id'):
        changed=deepcopy(item);changed['envelope']['evidence'][key]='4'*32
        with pytest.raises(QueueError):queue.enqueue(**changed)
    changed=deepcopy(item);p=changed['envelope']['evidence']
    p.update(parent_scope='unique_followup_in_verified_root',direct_reply_to_prior=False)
    with pytest.raises(QueueError):queue.enqueue(**changed)
    assert queue.list_tasks()['total']==1


@pytest.mark.parametrize('block',['pause','run','manual','worker','effects','hold'])
def test_followup_operator_recovery_cannot_bypass_existing_owner_gates(owner_case,block):
    case=owner_case;calls=[]
    case.lane.request_followup_review=lambda source:calls.append(source) or {'source_id':source,
        'status':'followup_ui_review_requested','dispatchable':False}
    if block=='pause':case.state['desired_state']='running'
    elif block=='run':case.state['run_id']='b'*32
    elif block=='manual':case.pending[0]=True
    elif block=='worker':case.flags[2].set()
    elif block=='effects':case.counts['publishing']=1
    elif block=='hold':case.flags[0].set()
    assert case.owner('request_followup_ui_review',SOURCE)['ok'] is False and calls==[]


def test_operator_followup_only_registers_and_preserves_pause(owner_case):
    case=owner_case;calls=[]
    case.lane.request_followup_review=lambda source:calls.append(source) or {'source_id':source,
        'status':'followup_ui_review_requested','dispatchable':False}
    result=case.owner('request_followup_ui_review',SOURCE)
    assert result['ok'] is True and result['queued_for_fresh_ui'] is True and result['dispatchable'] is False
    assert calls==[SOURCE] and case.state['desired_state']=='paused'


@pytest.mark.parametrize('failure',['detail','local','challenge'])
def test_native_followup_helper_exception_never_defers_or_dispatches(tmp_path,failure):
    lane,source,receiver,adapter,queue,conversation,registry,migrated=native_system(tmp_path,
        source_kind='native_reply_notice_candidate',notice_type=31,comment_type=2,parent_id_hint='7999999000000000008')
    calls=[]
    def fail(*a,**k):
        if failure=='detail':raise ReceiverDetailError('synthetic UI read failed',{})
        if failure=='challenge':raise ReceiverInterventionRequired('synthetic challenge')
        raise RuntimeError('synthetic helper unavailable')
    lane.followup_coordinator=SimpleNamespace(prepare=fail)
    lane._reply_session=lambda candidate:None
    source.defer=lambda *a,**k:calls.append('defer')
    try:result=lane.collect()
    except (RuntimeError,ReceiverInterventionRequired):result=[]
    assert result==[] and source.state!='pending' and calls==[] and not lane._pending
    assert queue.list_tasks()['total']==0 and receiver.calls==0


def test_existing_choice_session_keeps_old_choice_route(tmp_path):
    lane,source,receiver,adapter,queue,conversation,registry,migrated=native_system(tmp_path,
        source_kind='native_reply_notice_candidate',notice_type=31,comment_type=2,parent_id_hint='7999999000000000008')
    calls=[]
    lane.followup_coordinator=SimpleNamespace(prepare=lambda *a,**k:pytest.fail('pending Choice diverted'))
    lane._reply_session=lambda candidate:{'choice_id':'a'*64,'root_envelope':{'reference_id':'existing-choice-root'}}
    lane.choice_controller=SimpleNamespace(collect_pending=lambda *a,**k:calls.append('choice') or [])
    assert lane.collect()==[] and calls==['choice']
    assert source.state=='held' and source.held==['native_reply_not_matched']


def inspection_lane(case):
    source='native:'+'f'*64
    state={'run_id':'a'*32,'state':'paused','desired_state':'paused'}
    saved={'source_id':source,'state':'held','hold_code':'native_reply_parent_unresolved','historical':False,
        'candidate':deepcopy(case.candidate),'attempts':1,'plan':None,'task_id':None,'reference_id':None}
    events=[]
    def denied(*args,**kwargs):pytest.fail('read-only inspection invoked an effect')
    store=SimpleNamespace(get_source=lambda key:deepcopy(saved) if key==source else None,
        claim_ui_candidate=denied,request_followup_review=denied,record_ui_plan=denied)
    adapter=SimpleNamespace(expected_profile_url=case.binding.receiver_profile_url,
        read_followup_reply=lambda *a,**k:events.append('read') or deepcopy(case.detail))
    base=SimpleNamespace(adapter=adapter,registry=case.c.registry,queue=SimpleNamespace(get_control=lambda:dict(state)),
        conversation=SimpleNamespace(),run_id=state['run_id'],should_stop=lambda:False)
    lane=NativeLane(base,SimpleNamespace(),store)
    lane.followup_coordinator=case.coordinator
    lane._recovery_identity=lambda:events.append('fixedB')
    lane._checkpoint=lambda:True
    lane._visibility_absent_from_effects=lambda _:True
    return lane,store,state,saved,events


def test_explicit_inspection_observes_without_spending_recovery_or_model(case):
    lane,store,state,saved,events=inspection_lane(case)
    before=deepcopy(saved)
    case.c.model.complete=lambda *a,**k:pytest.fail('inspect planned a model')
    result=lane.inspect_followup_source(saved['source_id'])
    assert result=={'source_id':saved['source_id'],'status':'followup_source_inspected','dispatchable':False,'matched':True}
    assert saved==before and events==['fixedB','read'] and case.c.model.requests==[]
    assert not case.lane.pending


@pytest.mark.parametrize('block',['running','run_changed','effect','wrong_namespace','no_anchor'])
def test_explicit_inspection_scope_refusals_never_read_or_pay(case,block):
    lane,store,state,saved,events=inspection_lane(case);source=saved['source_id']
    if block=='running':state['desired_state']='running'
    elif block=='run_changed':state['run_id']='b'*32
    elif block=='effect':lane._visibility_absent_from_effects=lambda _:False
    elif block=='wrong_namespace':source='native-hint:'+'f'*64
    elif block=='no_anchor':case.anchors.clear()
    before=deepcopy(saved)
    if block=='no_anchor':
        assert lane.inspect_followup_source(source)['matched'] is False
    else:
        with pytest.raises(NativeLaneError):lane.inspect_followup_source(source)
    assert 'read' not in events and not case.c.model.requests and saved==before


@pytest.mark.parametrize('operation',['request_followup_ui_review','inspect_thread_followup'])
def test_followup_mailbox_namespace_and_no_inline_execution(tmp_path,operation):
    box,_,_=mailbox(tmp_path);source='native:'+'e'*64
    for wrong in ('native-hint:'+'e'*64,'assistant-task:'+'e'*32):
        with pytest.raises(Exception):box.submit(wrong,operation)
    box.submit(source,operation);calls=[]
    def handler(op,source):
        calls.append((op,source));return {'ok':True,'queued_for_fresh_ui':op=='request_followup_ui_review','dispatchable':False}
    result=box.process_one(handler)
    assert result['no_model'] and result['no_import'] and result['no_send']
    assert result['queued_for_fresh_ui']==(operation=='request_followup_ui_review')
    assert calls==[(operation,source)] and box.process_one(handler) is None


@pytest.mark.parametrize('block',['running','manual','worker','run_changed'])
def test_new_inspect_op_keeps_outer_owner_pause_and_manual_gates(owner_case,block):
    case=owner_case;calls=[]
    case.lane.inspect_followup_source=lambda source:calls.append(1) or {'source_id':source,
        'status':'followup_source_inspected','dispatchable':False,'matched':True}
    if block=='running':case.state['desired_state']='running'
    elif block=='manual':case.pending[0]=True
    elif block=='worker':case.flags[2].set()
    else:case.state['run_id']='b'*32
    assert case.owner('inspect_thread_followup',SOURCE)['ok'] is False and calls==[]


def test_new_inspect_op_success_does_not_queue_a_followup(owner_case):
    case=owner_case
    case.lane.inspect_followup_source=lambda source:{'source_id':source,
        'status':'followup_source_inspected','dispatchable':False,'matched':True}
    result=case.owner('inspect_thread_followup',SOURCE)
    assert result['ok'] is True and result['queued_for_fresh_ui'] is False
    assert case.state['desired_state']=='paused'


def test_inspect_without_current_read_cannot_attach_old_UI_counters(case):
    lane,store,state,saved,events=inspection_lane(case)
    lane.adapter.last_followup_diagnostic={'reason_code':'matched','counts':{'original_root_count':1,'selection_count':1}}
    lane.adapter.last_original_lookup={'candidate_count':1,'final_verified':1}
    case.anchors.clear()
    result=lane.inspect_followup_source(saved['source_id'])
    assert result['matched'] is False and 'read' not in events
    diagnostic=lane.last_native_recovery_diagnostic
    assert diagnostic['code']=='native_reply_parent_unresolved'
    assert not diagnostic.get('followup_counts') and not diagnostic.get('lookup_counts')


def test_inspect_detail_failure_keeps_fixed_counts_but_never_matches_or_pays(case):
    lane,store,state,saved,events=inspection_lane(case)
    before=deepcopy(saved)
    def failed(*args,**kwargs):
        lane.adapter.last_followup_diagnostic={'reason_code':'followup_source_ambiguous',
            'counts':{'body_candidate_count':2,'root_count':1,'followup_candidate_count':2,
                'selection_count':True,'raw':'DO-NOT-SAVE'}}
        lane.adapter.last_original_lookup={'candidate_count':1,'checked_count':True,'raw':'DO-NOT-SAVE'}
        raise ReceiverDetailError('DO-NOT-SAVE',{})
    lane.adapter.read_followup_reply=failed
    result=lane.inspect_followup_source(saved['source_id'])
    assert result['matched'] is False and saved==before and not case.c.model.requests
    diagnostic=lane.last_native_recovery_diagnostic
    assert diagnostic['code']=='followup_source_ambiguous'
    assert diagnostic['followup_counts']=={'body_candidate_count':2,'root_count':1,'followup_candidate_count':2}
    assert diagnostic['lookup_counts']=={'candidate_count':1}
    assert 'DO-NOT-SAVE' not in str(diagnostic)


def test_owner_projects_new_followup_counter_names_without_raw_values(owner_case):
    case=owner_case;writes=[]
    case.owner.control.queue=SimpleNamespace(update_control=lambda fn:writes.append(fn(dict(case.state))))
    case.lane.last_native_recovery_diagnostic={'stage':'followup_inspect_complete','code':'followup_source_ambiguous',
        'followup_counts':{'root_count':1,'prior_reply_count':1,'followup_candidate_count':2,'sender_row_count':1,
            'reply_to_B_count':1,'more_count':0,'body_candidate_count':2,'selection_count':True,'raw':'DO-NOT-SAVE'}}
    case.lane.inspect_followup_source=lambda source:{'source_id':source,'status':'followup_source_inspected',
        'dispatchable':False,'matched':False}
    result=case.owner('inspect_thread_followup',SOURCE)
    assert result['ok'] is True and result['queued_for_fresh_ui'] is False
    assert writes[0]['native_recovery']['diagnostic']['followup_counts']=={'root_count':1,'prior_reply_count':1,
        'followup_candidate_count':2,'sender_row_count':1,'reply_to_B_count':1,'more_count':0,'body_candidate_count':2}
    assert 'DO-NOT-SAVE' not in str(writes)
