"""Only fake lanes/events, no page, credentials or real task data."""
from types import SimpleNamespace
import threading
import pytest

from app.assistant.native_recovery_owner import NativeRecoveryOwner

SOURCE='native:'+'1'*64
RUN='a'*32

@pytest.fixture
def case():
    events=[]
    state={'run_id':RUN,'state':'paused','desired_state':'paused','process_alive':True,'worker_state':'idle'}
    def heartbeat(*args):state['state']='paused'
    def pause():state['desired_state']='paused'
    control=SimpleNamespace(snapshot=lambda:dict(state),pause=pause,heartbeat=heartbeat,
        set_receiver_health=lambda *a,**k:events.append('health'))
    flags=[threading.Event() for _ in range(3)]
    result={'source_id':SOURCE,'status':'legacy_prequeue_review_resumed','dispatchable':False}
    def review(source):
        assert flags[0].is_set() and flags[1].is_set();events.append(('review',source));return dict(result)
    lane=SimpleNamespace(review_and_resume_legacy_prequeue=review)
    counts={};pending=[False]
    owner=NativeRecoveryOwner(lane,control,SimpleNamespace(counts=lambda:dict(counts)),RUN,
        threading.RLock(),flags[0],flags[1],flags[2],lambda:pending[0])
    return SimpleNamespace(owner=owner,state=state,flags=flags,events=events,lane=lane,result=result,
        counts=counts,pending=pending)

@pytest.mark.parametrize('mutation',[{'desired_state':'running'},{'run_id':'b'*32},
    {'process_alive':False},{'worker_state':'processing'}])
def test_changed_runtime_does_not_invoke_review(case,mutation):
    case.state.update(mutation)
    assert case.owner('review_legacy_prequeue',SOURCE)['ok'] is False
    assert not case.events

def test_pending_human_or_actual_worker_prevents_review(case):
    case.pending[0]=True
    assert case.owner('review_legacy_prequeue',SOURCE)['ok'] is False
    case.pending[0]=False;case.flags[2].set()
    assert case.owner('review_legacy_prequeue',SOURCE)['ok'] is False
    assert not case.events

def test_legacy_review_is_one_read_review_leaves_pause_no_dispatch(case):
    value=case.owner('review_legacy_prequeue',SOURCE)
    assert value['ok'] is True and value['dispatchable'] is False
    assert case.events==[('review',SOURCE)]
    assert not case.flags[0].is_set() and not case.flags[1].is_set()
    assert case.state['desired_state']=='paused'

def test_wrong_source_or_missing_receipt_never_claim_success(case):
    case.result['source_id']='native:'+'2'*64
    assert case.owner('review_legacy_prequeue',SOURCE)['ok'] is False
    case.lane.source_store=SimpleNamespace(prequeue_recovery_status=lambda s:None)
    assert case.owner('resume_interrupted_prequeue',SOURCE)['ok'] is False

def test_failure_releases_local_review_mode_without_echoing_arbitrary_text(case):
    def fail(source):raise ValueError('synthetic private provider detail')
    case.lane.review_and_resume_legacy_prequeue=fail
    result=case.owner('review_legacy_prequeue',SOURCE)
    assert result=={'ok':False,'code':'native_recovery_not_safe'}
    assert not case.flags[0].is_set() and not case.flags[1].is_set()
    assert case.state['desired_state']=='paused'


def test_visibility_hint_request_only_arms_read_only_review(case):
    hint='native-hint:'+'2'*64
    def request(source):
        assert source==hint and case.flags[0].is_set() and case.flags[1].is_set()
        case.events.append('hint_request')
        return {'source_id':hint,'status':'visibility_ui_review_requested','dispatchable':False}
    case.lane.request_visibility_review=request
    result=case.owner('request_visibility_ui_review',hint)
    assert result['ok'] is True and result['queued_for_fresh_ui'] is True
    assert result['dispatchable'] is False and case.events==['hint_request']
    assert case.state['desired_state']=='paused'
