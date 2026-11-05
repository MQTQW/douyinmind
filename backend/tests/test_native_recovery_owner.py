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


def test_original_source_inspection_reads_once_and_keeps_processing_paused(case):
    def inspect(source):
        assert case.flags[0].is_set() and case.flags[1].is_set()
        case.events.append(('original_inspect',source))
        return {'source_id':source,'status':'original_source_inspected',
            'matched':True,'dispatchable':False}
    case.lane.inspect_original_source=inspect
    result=case.owner('inspect_original_source',SOURCE)
    assert result=={'ok':True,'reviewed':True,'queued_for_fresh_ui':False,
        'historical_preserved':True,'dispatchable':False}
    assert case.events==[('original_inspect',SOURCE)]
    assert case.state['state']==case.state['desired_state']=='paused'
    assert not case.flags[0].is_set() and not case.flags[1].is_set()


@pytest.mark.parametrize('block',['state','desired','run','process','worker_state',
    'worker_inflight','manual','hold','executing','publishing'])
def test_original_source_inspection_obeys_existing_owner_gates(case,block):
    case.lane.inspect_original_source=lambda *_:pytest.fail('blocked owner invoked source READ')
    if block=='state':case.state['state']='running'
    elif block=='desired':case.state['desired_state']='running'
    elif block=='run':case.state['run_id']='b'*32
    elif block=='process':case.state['process_alive']=False
    elif block=='worker_state':case.state['worker_state']='processing'
    elif block=='worker_inflight':case.flags[2].set()
    elif block=='manual':case.pending[0]=True
    elif block=='hold':case.flags[0].set()
    else:case.counts[block]=1
    assert case.owner('inspect_original_source',SOURCE)=={'ok':False,'code':'native_recovery_not_safe'}
    assert case.events==[]


@pytest.mark.parametrize('change',[{'dispatchable':True},{'source_id':'native:'+'2'*64},
    {'status':'unrecognized_read_result'}])
def test_original_source_inspection_rejects_invalid_lane_result(case,change):
    value={'source_id':SOURCE,'status':'original_source_inspected','dispatchable':False}
    value.update(change)
    case.lane.inspect_original_source=lambda source:value
    assert case.owner('inspect_original_source',SOURCE)=={'ok':False,'code':'native_recovery_effects_unknown'}
    assert case.state['desired_state']=='paused'
    assert not case.flags[0].is_set() and not case.flags[1].is_set()


def test_original_source_inspection_failure_keeps_fixed_reason_and_pause(case):
    def failed(source):raise ValueError('DO-NOT-SAVE')
    case.lane.inspect_original_source=failed
    assert case.owner('inspect_original_source',SOURCE)=={'ok':False,'code':'native_recovery_not_safe'}
    assert case.state['desired_state']=='paused'
    assert not case.flags[0].is_set() and not case.flags[1].is_set()


@pytest.mark.parametrize('reason',['matched','intervention_required'])
def test_original_source_inspection_projects_only_bounded_lookup_counts(case,reason):
    case.owner.control.queue=SimpleNamespace(update_control=lambda update:update(case.state))
    case.lane.last_native_recovery_diagnostic={
        'stage':'original_inspect_complete','code':'original_source_matched',
        'lookup_reason_code':reason,'raw':'DO-NOT-SAVE',
        'lookup_counts':{'candidate_count':1,'final_verified':1,'checked_count':True,
            'page_open_count':-1,'history_scrolls':1000001,'raw':'DO-NOT-SAVE'}}
    case.lane.inspect_original_source=lambda source:{'source_id':source,
        'status':'original_source_inspected','dispatchable':False,'matched':True}
    assert case.owner('inspect_original_source',SOURCE)['queued_for_fresh_ui'] is False
    assert case.state['native_recovery']=={'source_id':SOURCE,'diagnostic':{
        'stage':'original_inspect_complete','code':'original_source_matched',
        'lookup_reason_code':reason,'lookup_counts':{'candidate_count':1,'final_verified':1}}}
