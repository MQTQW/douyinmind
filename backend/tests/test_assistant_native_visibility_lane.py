"""Explicit unknown-visibility hints, real temp ledgers + fake UI/identity GET."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from app.assistant.browser_lane import BrowserLane
from app.assistant.native_events import project_page,project_ui_review_hints
from app.assistant.native_lane import NativeLane,NativeLaneError
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_visibility_hint_store import NativeVisibilityHintStore
from app.mention_agent.intake import make_envelope
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_native_lane import NativeCapture
from tests.test_assistant_native_receiver import at,page,B
from tests.test_assistant_product_bridge import system


@pytest.fixture
def case(tmp_path):
    binding,agent,_,registry,migrated,queue,conversation,_=system(tmp_path)
    adapter=NativeCapture(binding);base=BrowserLane(adapter,registry,queue,conversation)
    clock=[100.0]
    source=NativeSourceStore(tmp_path/'sources.sqlite3',B,binding.receiver_profile_url,create=True,clock=lambda:clock[0])
    def raw(nid,cid,content,created='101',unknown=False):
        value=at(nid=nid,cid=cid,sec=binding.sender_profile_url.rsplit('/',1)[-1],text=content,created=created)
        value['aweme_id']=value['at']['aweme']['aweme_id']='7999999000000000011'
        value['at']['schema_url']='aweme://aweme/detail/7999999000000000011?cid='+cid
        if unknown:value['at']['comment_status']=42
        return value
    def project(value,channel):
        return project_page(value,channel,receiver_uid=B,receiver_sec_uid=binding.receiver_profile_url.rsplit('/',1)[-1],
            profile_allowed=lambda profile:profile==binding.sender_profile_url)
    source.record_page(source.prepare_channel(9,'601'),project(page([raw('1000','20000','old instruction','90')]),9),next_cursor=('0','0'),has_more=False)
    source.record_page(source.prepare_channel(7,'2'),project(page(),7),next_cursor=('0','0'),has_more=False)
    assert source.arm_if_ready(True)
    clock[0]=102.0;source.set_verified()
    unknown=raw('1009','20009','@助手 根据编程知识库解释Git',unknown=True)
    projected=project(page([unknown]),9)
    source.record_page(source.prepare_channel(9,'601'),projected,next_cursor=('0','0'),has_more=False,allow_live=True)
    [hint]=project_ui_review_hints(page([unknown]),9,receiver_uid=B,
        receiver_sec_uid=binding.receiver_profile_url.rsplit('/',1)[-1],profile_allowed=lambda p:p==binding.sender_profile_url)
    ledger=NativeVisibilityHintStore(tmp_path/'hints.sqlite3',B,binding.receiver_profile_url,source,create=True,clock=lambda:clock[0])
    hid=ledger.record_hints([hint],page_fingerprint=projected['fingerprint'])[0]
    receiver=SimpleNamespace(calls=0,hook=lambda:None)
    def verify():
        receiver.calls+=1;receiver.hook();source.set_verified();return True
    receiver.reverify_identity_checkpoint=verify
    lane=NativeLane(base,receiver,source);lane.run_id='hint-owner';lane.visibility_hint_store=ledger
    lane.choice_controller=SimpleNamespace(agent=agent,admission_lock=threading.RLock(),
        known_selection_notice=lambda *a:False,selection_notice_requires_recheck=lambda *a:False,admitted=lambda tasks:None)
    queue.update_control(lambda old:{**old,'run_id':lane.run_id,'desired_state':'paused','state':'paused'})
    return SimpleNamespace(lane=lane,source=source,ledger=ledger,hid=hid,hint=hint,clock=clock,
        receiver=receiver,adapter=adapter,queue=queue,agent=agent,registry=registry,migrated=migrated,
        binding=binding,conversation=conversation,raw=raw,project=project)


def request(case):
    return case.lane.request_visibility_review(case.hid)


def no_task(case):
    assert not any(case.queue.counts().values())
    assert case.agent.list_tasks()==[]
    assert case.source.find_source_by_native_comment(B,case.hint['video_id'],case.hint['native_comment_id']) is None


def test_historical_hints_never_auto_activate_or_open_UI(case):
    assert case.lane.collect()==[]
    assert case.ledger.get_hint(case.hid)['state']=='historical'
    assert case.adapter.native_calls==[] and case.receiver.calls==0
    no_task(case)


@pytest.mark.parametrize('problem',['running','wrong_run','wrong_B','revoked','not_enabled'])
def test_explicit_request_requires_this_paused_owner_and_live_scope(case,problem):
    if problem=='running':case.queue.update_control(lambda old:{**old,'state':'running','desired_state':'running'})
    if problem=='wrong_run':case.queue.update_control(lambda old:{**old,'run_id':'other-owner'})
    if problem=='wrong_B':case.adapter.expected_profile_url='https://www.douyin.com/user/other-B'
    if problem=='revoked':case.registry.revoke_user(case.migrated['user_id'])
    if problem=='not_enabled':case.lane.visibility_hint_store=None
    with pytest.raises(Exception):request(case)
    assert case.ledger.get_hint(case.hid)['state']=='historical'
    no_task(case)


def test_explicit_request_is_inert_then_true_UI_builds_a_normal_planned_source(case):
    result=request(case)
    assert result=={'source_id':case.hid,'status':'visibility_ui_review_requested','dispatchable':False}
    assert case.ledger.get_hint(case.hid)['state']=='pending'
    no_task(case)
    rows=case.lane.collect()
    assert len(rows)==1 and case.receiver.calls>=3
    envelope=rows[0]['envelope']
    assert envelope['source_mode']=='live_ui' and envelope['native_comment_id'] is None
    assert envelope['reference_quality']=='verified_ui'
    assert case.ledger.get_hint(case.hid)['hint']==case.hint
    assert case.ledger.get_hint(case.hid)['state']=='admitted'
    source=case.source.get_source(case.ledger.get_hint(case.hid)['source_id'])
    assert source['state']=='planned' and source['candidate']['native_visibility']=='unknown'
    assert source['candidate']['hint_provenance']['original_source_kind']=='native_visibility_review_hint'
    assert case.source.admitted_source_current(source['source_id']) is False
    task=case.queue.enqueue(**rows[0]);case.lane.admitted([task])
    assert case.source.admitted_source_current(source['source_id']) is True
    assert case.conversation.task_scope(task['id']) is not None
    assert case.lane.authority_current(task) is True
    assert case.lane.collect()==[]
    assert len(case.queue.list_tasks()['items'])==1 and case.agent.list_tasks()==[]
    with pytest.raises(Exception):request(case)


@pytest.mark.parametrize('failure',['wrong_text','wrong_video','false_at','fake_notice','captcha','stop'])
def test_failed_or_interrupted_UI_never_creates_plan_task_or_automatic_retry(case,failure):
    request(case)
    if failure=='captcha':
        def before():raise ReceiverInterventionRequired('synthetic challenge')
        case.adapter.before_return=before
    elif failure=='stop':case.adapter.before_return=lambda:setattr(case.lane,'should_stop',lambda:True)
    else:
        def changed(detail):
            if failure=='wrong_text':detail['command_text']='different public instruction'
            if failure=='wrong_video':detail['video_id']='7999999000000000099'
            if failure=='false_at':detail['real_mention']=detail['evidence']['real_mention']=False
            if failure=='fake_notice':detail['native_ui_notice']['semantic_fingerprint']=''
            return detail
        case.adapter.change_detail=changed
    if failure=='captcha':
        with pytest.raises(ReceiverInterventionRequired):case.lane.collect()
    else:assert case.lane.collect()==[]
    row=case.ledger.get_hint(case.hid)
    assert row['state']==('claimed' if failure=='stop' else 'held') and row['attempts']==1
    no_task(case)
    case.lane.should_stop=lambda:False
    assert case.lane.collect()==[]


@pytest.mark.parametrize('where',['Queue','Agent','QueueNative','AgentNative'])
def test_old_same_native_or_UI_semantic_task_blocks_hint_even_if_source_missing(case,where):
    notice={'source_profile_url':case.binding.sender_profile_url,'exact_text':case.hint['content'],'semantic_fingerprint':'old-real-shaped-UI-card'}
    if where.endswith('Native'):notice['exact_text']='different old body with same native comment'
    detail=case.adapter.detail(notice)
    if where.endswith('Native'):
        detail['native_comment_id']=detail['evidence']['native_comment_id']=case.hint['native_comment_id']
    envelope=make_envelope(notice,detail,case.binding)
    if where.startswith('Queue'):case.queue.enqueue(case.binding,envelope,kind='query')
    else:case.agent.enqueue(case.binding.binding_id,envelope)
    request(case)
    assert case.lane.collect()==[]
    assert case.ledger.get_hint(case.hid)['state']=='held'
    assert case.source.find_source_by_native_comment(B,case.hint['video_id'],case.hint['native_comment_id']) is None
    assert case.adapter.native_calls==[]


def test_revocation_during_UI_is_rechecked_before_typed_plan(case):
    request(case)
    case.adapter.before_return=lambda:case.registry.revoke_user(case.migrated['user_id'])
    assert case.lane.collect()==[]
    assert case.ledger.get_hint(case.hid)['state']=='held'
    no_task(case)


def test_hint_is_only_considered_after_no_normal_source_is_available(case):
    request(case)
    live=case.raw('1010','20100','@助手 根据编程知识库回答别的问题')
    case.source.record_page(case.source.prepare_channel(9,'601'),case.project(page([live]),9),next_cursor=('0','0'),has_more=False,allow_live=True)
    rows=case.lane.collect()
    assert len(rows)==1 and rows[0]['envelope']['comment_text']==live['at']['content']
    assert case.ledger.get_hint(case.hid)['state']=='pending' and case.ledger.get_hint(case.hid)['attempts']==0
    assert case.receiver.calls==1  # Request only; normal source kept its normal checks.


def test_process_restart_cannot_reclaim_an_expired_hint_UI_attempt(case):
    request(case)
    claim=case.ledger.claim_review(case.hid,'lost-owner',lease_seconds=1)
    assert claim is not None
    case.clock[0]+=2;case.source.set_verified()
    assert case.lane.collect()==[]
    assert case.ledger.get_hint(case.hid)['state']=='claimed'
    no_task(case)


def test_conflicting_native_pin_after_actual_UI_blocks_admission(case):
    request(case)
    def conflict():
        with case.source._connection(write=True) as db:db.execute("UPDATE identities SET state='conflict'")
    case.adapter.before_return=conflict
    assert case.lane.collect()==[]
    assert case.ledger.get_hint(case.hid)['state']=='held'
    no_task(case)


def test_commit_ack_loss_never_reopens_hint_or_reenqueues_plan(case,monkeypatch):
    request(case)
    original=case.source.accept_verified_hint_ui_plan
    def lost_ack(permit):
        original(permit)
        raise NativeLaneError('native_visibility_commit_unknown')
    monkeypatch.setattr(case.source,'accept_verified_hint_ui_plan',lost_ack)
    assert case.lane.collect()==[]
    source=case.source.get_source(case.ledger.get_hint(case.hid)['source_id'])
    assert source['state']=='planned' and source['plan'] is not None
    assert case.lane.collect()==[]
    assert case.source.get_source(source['source_id'])['state']=='held'
    assert not any(case.queue.counts().values()) and case.agent.list_tasks()==[]
    with pytest.raises(Exception):request(case)


def test_lifecycle_pause_inside_hint_read_propagates_without_new_plan(case):
    request(case)
    class Lifecycle:
        def cycle(self,**kwargs):
            from contextlib import contextmanager
            @contextmanager
            def blocked():
                raise NativeLaneError('session_refresh_unavailable')
                yield
            return blocked()
        def safe_status(self):return {'state':'paused'}
    case.lane.session_lifecycle=Lifecycle()
    with pytest.raises(ReceiverInterventionRequired):case.lane.collect()
    assert case.ledger.get_hint(case.hid)['state']=='held'
    no_task(case)
