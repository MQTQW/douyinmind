"""Known hint conflicts must revoke later effects, not merely future intake."""
from copy import deepcopy
import hashlib

import pytest

from tests.test_assistant_native_visibility_lane import case,request
from tests.test_assistant_native_lane import native_system


def admitted(case):
    request(case); rows=case.lane.collect()
    assert len(rows)==1
    task=case.queue.enqueue(**rows[0]); case.lane.admitted([task])
    assert case.lane.authority_current(task) is True
    return task


def test_later_hint_payload_conflict_blocks_already_admitted_source(case):
    task=admitted(case)
    edited=deepcopy(case.hint);edited['content']='edited original public instruction'
    case.ledger.record_hints([edited],page_fingerprint='b'*64)
    assert case.ledger.get_hint(case.hid)['state']=='held'
    assert case.lane.authority_current(task) is False


@pytest.mark.parametrize('problem', ['missing','pending','committing','hold_code',
    'wrong_hint_id','wrong_source_id','wrong_admitted_source_id','changed_body',
    'changed_sender','changed_receiver','changed_video','changed_cid'])
def test_current_hint_receipt_is_required_for_each_effect_gate(case,monkeypatch,problem):
    task=admitted(case)
    row=case.ledger.get_hint(case.hid)
    if problem=='missing':row=None
    elif problem in {'pending','committing'}:row['state']=problem
    elif problem=='hold_code':row['hold_code']='native_visibility_hint_payload_conflict'
    elif problem=='wrong_hint_id':row['hint_id']='native-hint:'+'0'*64
    elif problem=='wrong_source_id':row['source_id']='native:'+'0'*64
    elif problem=='wrong_admitted_source_id':row['admitted_source_id']='native:'+'0'*64
    elif problem=='changed_body':row['hint']['content']='different public body'
    elif problem=='changed_sender':row['hint']['sender_uid']='9000000000000000001'
    elif problem=='changed_receiver':row['hint']['recipient_uid']='9000000000000000002'
    elif problem=='changed_video':row['hint']['video_id']='7999999000000000099'
    elif problem=='changed_cid':row['hint']['native_comment_id']='9000000000000000003'
    monkeypatch.setattr(case.ledger,'get_hint',lambda hid:deepcopy(row))
    assert case.lane.authority_current(task) is False


def test_missing_configured_hint_ledger_fails_closed(case):
    task=admitted(case);case.lane.visibility_hint_store=None
    assert case.lane.authority_current(task) is False


def test_valid_continuous_check_is_readonly_and_does_not_refresh_intake(case):
    task=admitted(case)
    before={path:hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (case.ledger.path,case.source.path,case.queue.path)}
    network_calls=case.receiver.calls
    case.clock[0]+=1000
    assert case.lane.authority_current(task) is True
    assert case.lane.authority_current(task) is True
    assert case.receiver.calls==network_calls
    assert before=={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in before}


def test_ordinary_source_has_no_hint_dependency_or_extra_reads(tmp_path):
    lane,source,_,_,queue,*_=native_system(tmp_path)
    [row]=lane.collect();task=queue.enqueue(**row);lane.admitted([task])
    class UnavailableHintLedger:
        def get_hint(self,*args):pytest.fail('ordinary sources must not read a hint ledger')
    lane.visibility_hint_store=UnavailableHintLedger()
    assert lane.authority_current(task) is True
