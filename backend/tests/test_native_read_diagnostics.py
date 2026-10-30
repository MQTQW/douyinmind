"""Fresh report-only READ diagnostics, real temporary source claims and fake UI."""
from copy import deepcopy
from types import SimpleNamespace
import json
import threading

import pytest

from app.assistant.native_lane import NativeLane, NativeLaneError
from app.assistant.native_source_store import NativeStoreError
from app.mention_receiver.browser import ReceiverPageError, ReceiverDetailError
from tests.test_native_source_followup_review import case as reviewed_case, reply_case, request, row


ZERO = {'configured':0,'expanded':0,'restored':0,'restore_failed':0}


def normal_lane(case):
    store, clock, source, first = case
    request(case)  # Synthetic normal one-shot operator request, never a production source.
    candidate = store.get_source(source)['candidate']
    effects = {'read':0,'native_get':0,'model':0,'enqueue':0,'send':0}
    def forbidden(*args, **kwargs):
        pytest.fail('diagnostic attempted an effect or extra read/model')
    registry = SimpleNamespace(find_user=lambda *_:{'active':True,'verification_status':'verified'},
        intake_binding=lambda *_:object())
    queue = SimpleNamespace(get_control=lambda:{'desired_state':'running'},
        list_tasks=lambda **_:{'total':0,'items':[]},enqueue=forbidden)
    adapter = SimpleNamespace(expected_profile_url=candidate['receiver_profile_url'],check_intervention=lambda:None,
        prepare_reply=forbidden,submit_reply=forbidden)
    base = SimpleNamespace(adapter=adapter,registry=registry,queue=queue,conversation=SimpleNamespace(),
        run_id='synthetic-normal-read',should_stop=lambda:False,pending={},choice_controller=None,
        last_detail_failure_count=0,semantic_resolution_provider=forbidden,clear_buffer=lambda:None)
    lane = NativeLane(base,SimpleNamespace(poll_once=forbidden,reverify_identity_checkpoint=forbidden),store)
    def prepare(claim, *, checkpoint, read):
        assert checkpoint()
        read({},claim['candidate'])
        return [],None,None,None
    lane.followup_coordinator=SimpleNamespace(prepare=prepare)
    return lane,store,source,adapter,effects


def test_plain_page_failure_keeps_attempt_two_claim_without_effects_and_drops_old_match(reviewed_case):
    lane,store,source,adapter,effects = normal_lane(reviewed_case)
    old={'stage':'old','reason_code':'matched','counts':{'root_count':1},'raw':'PRIVATE-OLD'}
    adapter.last_followup_diagnostic=deepcopy(old)
    adapter.last_followup_diagnostics=deepcopy(old)
    adapter.last_original_lookup={'final_verified':1,'raw':'PRIVATE-OLD'}
    adapter.last_choice_readback_viewport_diagnostics={'configured':1,'expanded':1,'restored':1,'restore_failed':1}
    expected={}
    failure=ReceiverPageError('PRIVATE-COMMENT https://private.invalid token=DO-NOT-REPORT')
    def read(*args,**kwargs):
        effects['read']+=1
        assert adapter.last_followup_diagnostic['reason_code']=='checking'
        assert adapter.last_followup_diagnostic['counts']=={} and adapter.last_original_lookup=={}
        assert adapter.last_choice_readback_viewport_diagnostics==ZERO
        expected.update(row(store,source))
        adapter.last_choice_readback_viewport_diagnostics={'configured':1,'expanded':1,'restored':1,'restore_failed':0,
            'raw':'PRIVATE-NEW','source_id':source}
        raise failure
    adapter.read_followup_reply=read
    with pytest.raises(ReceiverPageError) as caught:
        lane.collect()
    assert caught.value is failure and effects=={'read':1,'native_get':0,'model':0,'enqueue':0,'send':0}
    assert row(store,source)==expected and expected['state']=='claimed' and expected['attempts']==2
    assert expected['plan'] is expected['task_id'] is expected['reference_id'] is None
    assert lane.last_native_read_diagnostic=={'stage':'original_ui_lookup','code':'native_read_page_unready',
        'exception_type':'ReceiverPageError','viewport_counts':{'configured':1,'expanded':1,'restored':1,'restore_failed':0}}
    diagnostic=json.dumps(lane.last_native_read_diagnostic)
    assert all(value not in diagnostic for value in ('PRIVATE','token','https://',source,expected['claim_token'],'matched'))
    with pytest.raises(NativeStoreError,match='already_requested'):
        request(reviewed_case)
    lane.semantic_intake_ready=lambda:False
    assert lane.collect()==[] and lane.last_native_read_diagnostic=={} and row(store,source)==expected


def test_followup_none_resolution_has_its_own_fixed_report_and_keeps_claim(reviewed_case):
    lane,store,source,adapter,effects = normal_lane(reviewed_case)
    expected={}
    def read(*args,**kwargs):
        effects['read']+=1;expected.update(row(store,source))
        adapter.last_choice_readback_viewport_diagnostics={'configured':1,'expanded':1,'restored':1,'restore_failed':0}
        return {'ok':True,'not_persisted':'PRIVATE-COMMENT'}
    adapter.read_followup_reply=read
    assert lane.collect()==[]
    assert row(store,source)==expected and expected['state']=='claimed' and expected['attempts']==2
    assert effects=={'read':1,'native_get':0,'model':0,'enqueue':0,'send':0}
    assert lane.last_native_read_diagnostic=={'stage':'followup_prepare','code':'native_followup_no_resolution',
        'exception_type':None,'viewport_counts':{'configured':1,'expanded':1,'restored':1,'restore_failed':0}}


def bare_lane():
    lane=NativeLane.__new__(NativeLane)
    lane._owner_thread=threading.get_ident()
    lane.adapter=SimpleNamespace()
    lane.last_native_read_diagnostic={'stage':'old','code':'matched','viewport_counts':{'configured':1}}
    return lane


@pytest.mark.parametrize('error,code,kind',[
    (ReceiverPageError('PRIVATE'),'native_read_page_unready','ReceiverPageError'),
    (ReceiverDetailError('PRIVATE',{'raw':'PRIVATE'}),'native_read_detail_unverified','ReceiverDetailError'),
    (NativeLaneError('PRIVATE'),'native_read_page_unready','NativeLaneError'),
    (TimeoutError('PRIVATE'),'native_read_unconfirmed','TimeoutError'),
    (RuntimeError('PRIVATE'),'native_read_unconfirmed','local_error'),
])
def test_fixed_exception_categories_never_copy_messages_or_bad_viewport_values(error,code,kind):
    lane=bare_lane()
    def read():
        lane.adapter.last_choice_readback_viewport_diagnostics={'configured':True,'expanded':2,'restored':1.0,
            'restore_failed':-1,'raw':'PRIVATE'}
        raise error
    with pytest.raises(type(error)) as caught:
        lane._read_with_interruption_receipt({},'choice_ui_read',read)
    assert caught.value is error
    assert lane.last_native_read_diagnostic=={'stage':'choice_ui_read','code':code,'exception_type':kind,'viewport_counts':ZERO}


def test_read_return_none_is_not_retried_or_converted_to_a_failure_transition():
    lane=bare_lane();calls=[]
    assert lane._read_with_interruption_receipt({},'original_ui_lookup',lambda:calls.append('read')) is None
    assert calls==['read']
    assert lane.last_native_read_diagnostic=={'stage':'original_ui_lookup','code':'native_read_no_result',
        'exception_type':None,'viewport_counts':ZERO}


@pytest.mark.parametrize('index',[0,2,10,True,-1,11,'PRIVATE',{}])
def test_lookup_stage_is_fresh_bounded_and_never_an_old_matched_result(index):
    lane=bare_lane()
    lane.adapter.last_original_lookup={'lookup_stage_index':10,'raw':'PRIVATE-OLD'}
    failure=ReceiverPageError('PRIVATE')
    def read():
        assert lane.adapter.last_original_lookup=={}
        assert 'lookup_stage_index' not in lane.last_native_read_diagnostic
        lane.adapter.last_original_lookup={'lookup_stage_index':index,'raw':'PRIVATE-NEW'}
        raise failure
    with pytest.raises(ReceiverPageError) as caught:
        lane._read_with_interruption_receipt({},'original_ui_lookup',read)
    assert caught.value is failure
    if type(index) is int and 0<=index<=10:
        assert lane.last_native_read_diagnostic['lookup_stage_index']==index
    else:
        assert 'lookup_stage_index' not in lane.last_native_read_diagnostic
    assert 'PRIVATE' not in json.dumps(lane.last_native_read_diagnostic)
