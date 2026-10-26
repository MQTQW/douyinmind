"""Trusted read interruption recovery; synthetic facts and temp owned files."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from app.assistant.native_source_store import NativeSourceStore, NativeStoreError
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_native_receiver import B, PROFILE_B, page, project, reply
from tests.test_assistant_native_lane import actual_source_system


@pytest.fixture
def reading(tmp_path):
    clock=[100.0]
    store=NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:clock[0])
    for channel,group in ((9,'601'),(7,'2')):
        store.record_page(store.prepare_channel(channel,group),project(page(),channel),next_cursor=('0','0'),has_more=False)
    assert store.arm_if_ready(True)
    clock[0]=102;store.set_verified()
    rows=store.record_page(store.prepare_channel(7,'2'),project(page([reply(created='101')]),7),
        next_cursor=('0','0'),has_more=False,allow_live=True)
    claim=store.claim_ui_candidate('runtime-owner',lease_seconds=300)
    assert claim['source_id']==rows[0]['source_id']
    return store,clock,claim


def source_row(store,sid):
    with store._connection() as db:return dict(db.execute('SELECT * FROM sources WHERE source_id=?',(sid,)).fetchone())


def record(store,claim):
    return store.record_prequeue_intervention(claim['source_id'],claim['claim_token'],
        run_id='runtime-owner',stage='choice_ui_read')


def resume(store,claim,receipt):
    return store.request_prequeue_resume(claim['source_id'],receipt_id=receipt['receipt_id'],
        expected_attempts=1,operator_reason='explicit_operator_after_prequeue_intervention')


def test_receipt_and_explicit_resume_preserve_source_and_prior_audits(reading):
    store,clock,claim=reading;sid=claim['source_id'];before=source_row(store,sid)
    with store._connection(write=True) as db:db.execute('INSERT INTO meta VALUES(?,?)',('ui_recheck_v1:'+sid,'{"unchanged_prior":true}'))
    receipt=record(store,claim);held=source_row(store,sid)
    assert receipt['dispatchable'] is False and held['state']=='held'
    assert held['hold_code']=='native_prequeue_intervention' and held['claim_token'] is None
    assert store.check_candidate(sid,claim['claim_token'])['valid'] is False
    result=resume(store,claim,receipt)
    assert result['dispatchable'] is False and source_row(store,sid)['state']=='fresh'
    for key in ('candidate','immutable_hash','first_seen','last_seen','identity_revision','attempts'):
        assert source_row(store,sid)[key]==before[key]
    with store._connection() as db:
        assert db.execute('SELECT value FROM meta WHERE key=?',('ui_recheck_v1:'+sid,)).fetchone()[0]=='{"unchanged_prior":true}'
        audit=''.join(r[0] for r in db.execute("SELECT value FROM meta WHERE key LIKE 'prequeue_%'"))
        assert claim['claim_token'] not in audit
    with pytest.raises(NativeStoreError,match='already_requested'):resume(store,claim,receipt)
    assert store.claim_ui_candidate('next-owner')['source_id']==sid
    assert source_row(store,sid)['attempts']==2


def test_expiry_without_receipt_is_not_recoverable(reading):
    store,clock,claim=reading;clock[0]+=301
    assert store.claim_ui_candidate('later-owner') is None
    before=source_row(store,claim['source_id'])
    with pytest.raises(NativeStoreError,match='receipt_required'):
        resume(store,claim,{'receipt_id':'a'*64})
    assert source_row(store,claim['source_id'])==before


@pytest.mark.parametrize('field,value',[('historical',1),('plan','{}'),('task_id','spent'),('reference_id','maybe-queued'),('state','planned')])
def test_read_receipt_cannot_relabel_possible_effect_or_history(reading,field,value):
    store,clock,claim=reading
    with store._connection(write=True) as db:db.execute('UPDATE sources SET '+field+'=? WHERE source_id=?',(value,claim['source_id']))
    before=source_row(store,claim['source_id'])
    with pytest.raises(NativeStoreError):record(store,claim)
    assert source_row(store,claim['source_id'])==before


@pytest.mark.parametrize('problem',['stale','pause','pin','nan','future'])
def test_resume_requires_fresh_finite_pinned_identity(reading,problem):
    store,clock,claim=reading;receipt=record(store,claim)
    if problem=='stale':clock[0]+=121
    elif problem=='pause':store.pause('verification_required')
    else:
        with store._connection(write=True) as db:
            if problem=='pin':db.execute('UPDATE identities SET revision=revision+1')
            else:db.execute("UPDATE meta SET value=? WHERE key='verified_at'",('NaN' if problem=='nan' else str(clock[0]+6),))
    before=source_row(store,claim['source_id'])
    with pytest.raises(NativeStoreError,match='identity_unverified'):resume(store,claim,receipt)
    assert source_row(store,claim['source_id'])==before


def test_two_store_instances_only_one_explicit_resume(reading):
    store,clock,claim=reading;receipt=record(store,claim)
    second=NativeSourceStore(store.path,B,PROFILE_B,clock=lambda:clock[0])
    def attempt(instance):
        try:return resume(instance,claim,receipt)['status']
        except NativeStoreError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(attempt,[store,second]))
    assert results.count('prequeue_resume_requested')==1
    assert results.count('native_prequeue_resume_already_requested')==1


def test_audit_and_transition_roll_back_together(reading):
    store,clock,claim=reading;receipt=record(store,claim)
    with store._connection(write=True) as db:
        db.execute("CREATE TRIGGER reject_resume BEFORE UPDATE ON sources WHEN NEW.state='fresh' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    before=source_row(store,claim['source_id'])
    with pytest.raises(Exception):resume(store,claim,receipt)
    assert source_row(store,claim['source_id'])==before
    with store._connection() as db:assert db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'prequeue_resume_v1:%'").fetchone()[0]==0


def test_actual_read_boundary_records_but_front_checkpoint_does_not(tmp_path):
    lane,store,adapter,queue,history,publish,clock=actual_source_system(tmp_path)
    def challenge():raise ReceiverInterventionRequired('synthetic human gate')
    adapter.before_return=challenge
    with pytest.raises(ReceiverInterventionRequired) as error:lane.collect()
    receipt=error.value.native_prequeue_receipt
    row=source_row(store,receipt['source_id'])
    assert row['state']=='held' and row['hold_code']=='native_prequeue_intervention'
    assert row['plan'] is None and row['task_id'] is None and sum(queue.counts().values())==0
    adapter.before_return=lambda:None;adapter.check_intervention=challenge
    with pytest.raises(ReceiverInterventionRequired):lane.collect()
    with store._connection() as db:assert db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'prequeue_intervention_v1:%'").fetchone()[0]==1
