"""Explicit read-only inspection: real temp ledgers, fake UI/identity only."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.assistant.native_lane import NativeLaneError
from app.assistant.native_recovery_mailbox import NativeRecoveryMailboxError
from app.mention_receiver.browser import ReceiverDetailError, ReceiverInterventionRequired
from tests.test_assistant_native_visibility_lane import case as native_case, request, no_task
from tests.test_assistant_native_recovery_mailbox import make
from tests.test_native_recovery_owner import case as owner_case


@pytest.fixture
def held(native_case):
    case=native_case
    request(case)
    claim=case.ledger.claim_review(case.hid,'synthetic-inspection-fixture',lease_seconds=120)
    case.ledger.hold(case.hid,claim['claim_token'],'native_visibility_ui_unverified')
    return case


def state_hash(case):
    return hashlib.sha256(case.ledger.path.read_bytes()).hexdigest(),deepcopy(case.ledger.get_hint(case.hid))


def prohibit_effects(case,monkeypatch):
    def denied(*args,**kwargs):pytest.fail('inspection invoked an effect')
    for obj,names in ((case.ledger,('claim_review','request_review','hold')),
                      (case.source,('claim_ui_candidate','record_ui_plan','accept_verified_hint_ui_plan')),
                      (case.queue,('enqueue',)),(case.lane.browser_lane,('prepare_native_detail',))):
        for name in names:monkeypatch.setattr(obj,name,denied)
    case.lane.browser_lane.semantic_resolution_provider=denied


def test_held_inspection_reads_once_without_mutating_hint_or_admitting(held,monkeypatch):
    before=state_hash(held);prohibit_effects(held,monkeypatch)
    result=held.lane.inspect_visibility_hint(held.hid)
    assert result=={'source_id':held.hid,'status':'visibility_hint_inspected','dispatchable':False}
    assert len(held.adapter.native_calls)==1 and held.receiver.calls==3  # prior explicit request + two checks
    assert state_hash(held)==before
    assert held.lane.last_native_recovery_diagnostic['stage']=='inspect_complete'
    assert held.queue.get_control()['desired_state']=='paused'
    no_task(held)


@pytest.mark.parametrize('source',['native:'+'1'*64,'native-hint:'+'1'*63,'native-hint:'+'A'*64])
def test_cross_namespace_or_bad_identifier_never_opens_UI(held,source):
    before=state_hash(held)
    with pytest.raises(NativeLaneError):held.lane.inspect_visibility_hint(source)
    assert not held.adapter.native_calls and state_hash(held)==before


@pytest.mark.parametrize('problem',['running','different_run','revoked','missing_skip','different_B','existing_source'])
def test_scope_or_effect_conflict_blocks_before_UI(held,problem):
    if problem=='running':held.queue.update_control(lambda old:{**old,'state':'running','desired_state':'running'})
    elif problem=='different_run':held.lane.run_id='different-run'
    elif problem=='revoked':held.registry.revoke_user(held.migrated['user_id'])
    elif problem=='different_B':held.adapter.expected_profile_url='https://www.douyin.com/user/other-B'
    elif problem=='missing_skip':
        with held.source._connection(write=True) as db:db.execute('DELETE FROM notice_skips')
    elif problem=='existing_source':
        held.source.visibility_hint_checkpoint=lambda _:(_ for _ in ()).throw(NativeLaneError('native_visibility_hint_source_exists'))
    before=state_hash(held)
    with pytest.raises(NativeLaneError):held.lane.inspect_visibility_hint(held.hid)
    assert not held.adapter.native_calls and state_hash(held)==before


def test_nonheld_historical_hint_is_not_promoted(native_case):
    with pytest.raises(NativeLaneError):native_case.lane.inspect_visibility_hint(native_case.hid)
    assert native_case.ledger.get_hint(native_case.hid)['state']=='historical'
    assert not native_case.adapter.native_calls


@pytest.mark.parametrize('kind',['lookup_failure','challenge','unknown'])
def test_fixed_failure_diagnostics_only_and_no_hint_change(held,kind):
    before=state_hash(held)
    def read(target,**kwargs):
        held.adapter.last_original_lookup={'candidate_limit':3,'candidate_count':0,'lookup_stage_index':3,
            'page_open_count':1,'reason_code':'original_lookup_unavailable','secret':'DO-NOT-SAVE',
            'unreadable_count':True,'checked_count':'DO-NOT-SAVE','unsafe':999}
        if kind=='challenge':raise ReceiverInterventionRequired('DO-NOT-SAVE')
        if kind=='lookup_failure':raise ReceiverDetailError('DO-NOT-SAVE')
        raise RuntimeError('DO-NOT-SAVE')
    held.adapter.open_original_from_notifications=read
    with pytest.raises(ReceiverInterventionRequired if kind=='challenge' else NativeLaneError):
        held.lane.inspect_visibility_hint(held.hid)
    diagnostic=held.lane.last_native_recovery_diagnostic
    assert diagnostic['stage']=='inspect_original_lookup'
    assert diagnostic['lookup_counts']=={'candidate_limit':3,'candidate_count':0,'lookup_stage_index':3,'page_open_count':1}
    assert diagnostic['lookup_reason_code']=='original_lookup_unavailable'
    assert 'DO-NOT-SAVE' not in json.dumps(diagnostic)
    assert state_hash(held)==before
    no_task(held)


def test_revocation_after_read_cannot_report_inspected(held):
    before=state_hash(held)
    held.adapter.before_return=lambda:held.registry.revoke_user(held.migrated['user_id'])
    with pytest.raises(NativeLaneError):held.lane.inspect_visibility_hint(held.hid)
    assert len(held.adapter.native_calls)==1 and state_hash(held)==before
    assert held.lane.last_native_recovery_diagnostic['stage']=='inspect_after_read'
    no_task(held)


def test_inspection_mailbox_uses_hint_namespace_and_never_queues_execution(tmp_path):
    box,_,_=make(tmp_path);hint='native-hint:'+'c'*64
    with pytest.raises(NativeRecoveryMailboxError):box.submit('native:'+'c'*64,'inspect_visibility_hint')
    submitted=box.submit(hint,'inspect_visibility_hint');seen=[]
    def inspect(op,source):
        seen.append((op,source));return {'ok':True,'reviewed':True,'queued_for_fresh_ui':False,'dispatchable':False}
    result=box.process_one(inspect)
    assert result['code']=='native_recovery_inspected' and result['queued_for_fresh_ui'] is False
    assert result['no_model'] and result['no_send'] and result['no_import']
    assert seen==[('inspect_visibility_hint',hint)]
    assert box.process_one(lambda *_:pytest.fail('replayed')) is None


def test_owner_keeps_inspection_paused_and_projects_only_safe_lookup_counts(owner_case):
    case=owner_case;hint='native-hint:'+'c'*64;writes=[]
    case.control=case.owner.control
    case.control.queue=SimpleNamespace(update_control=lambda fn:writes.append(fn(dict(case.state))))
    case.lane.last_native_recovery_diagnostic={'stage':'inspect_original_lookup','code':'native_recovery_unconfirmed',
        'exception_type':'ReceiverDetailError','lookup_reason_code':'original_lookup_unavailable',
        'lookup_counts':{'candidate_count':0,'page_open_count':1,'private':'DO-NOT-SAVE','checked_count':True}}
    case.lane.inspect_visibility_hint=lambda source:{'source_id':source,'status':'visibility_hint_inspected','dispatchable':False}
    result=case.owner('inspect_visibility_hint',hint)
    assert result['ok'] is True and result['queued_for_fresh_ui'] is False
    assert case.state['desired_state']=='paused' and not any(flag.is_set() for flag in case.flags)
    assert writes[0]['native_recovery']['diagnostic']['lookup_counts']=={'candidate_count':0,'page_open_count':1}
    assert 'DO-NOT-SAVE' not in json.dumps(writes)


def test_explicit_CLI_only_submits_inspection_mailbox_intent(tmp_path,monkeypatch):
    from app.assistant import __main__ as main
    from app.assistant import native_recovery_mailbox as mailbox
    calls=[];run='a'*32;hint='native-hint:'+'c'*64
    monkeypatch.setattr(main,'STATE',tmp_path)
    monkeypatch.setattr(main,'AssistantQueue',lambda *a,**k:object())
    monkeypatch.setattr(main,'AssistantControl',lambda *a,**k:SimpleNamespace(snapshot=lambda:{'run_id':run}))
    monkeypatch.setattr(main,'start_guard',lambda:None)
    class Box:
        def __init__(self,path,run_id,control):assert path==tmp_path and run_id==run
        def submit(self,source,op):calls.append((source,op));return {'status':'queued'}
    monkeypatch.setattr(mailbox,'NativeRecoveryMailbox',Box)
    assert main.main(['--state-dir',str(tmp_path),'--inspect-native-visibility-hint',hint])==0
    assert calls==[(hint,'inspect_visibility_hint')]
