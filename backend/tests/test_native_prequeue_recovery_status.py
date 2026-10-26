"""Read-only receipt discovery; no runtime accounts, services or credentials."""
import hashlib
import json

from tests.test_native_prequeue_intervention_recovery import reading,record


def digest(store):return hashlib.sha256(store.path.read_bytes()).hexdigest()


def test_read_status_preserves_every_source_and_receipt_timestamp(reading):
    store,clock,claim=reading;receipt=record(store,claim);before=digest(store)
    first=store.prequeue_recovery_status(claim['source_id']);clock[0]+=10
    assert first==store.prequeue_recovery_status(claim['source_id'])
    assert before==digest(store)
    assert first['receipt']['receipt_id']==receipt['receipt_id']
    assert first['receipt']['recorded_at']==102 and first['dispatchable'] is False
    assert claim['claim_token'] not in json.dumps(first)
    assert not first['resume_requested'] and not first['legacy_review_used']
    assert all(key not in first for key in ('candidate','sender_uid','claim_token','plan'))


def test_unknown_source_returns_none_without_creating_any_record(reading):
    store,clock,claim=reading;before=digest(store)
    assert store.prequeue_recovery_status('native:'+'0'*64) is None
    assert digest(store)==before


def test_unknown_private_fields_are_not_projected_and_malformed_receipt_is_unavailable(reading):
    store,clock,claim=reading;record(store,claim)
    key='prequeue_intervention_v1:'+claim['source_id']+':1'
    with store._connection(write=True) as db:
        value=json.loads(db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()[0])
        value['cookie']='synthetic_private_value'
        db.execute('UPDATE meta SET value=? WHERE key=?',(json.dumps(value),key))
    before=digest(store)
    assert 'synthetic_private_value' not in json.dumps(store.prequeue_recovery_status(claim['source_id']))
    assert before==digest(store)
    with store._connection(write=True) as db:db.execute('UPDATE meta SET value=? WHERE key=?',('[]',key))
    before=digest(store)
    assert store.prequeue_recovery_status(claim['source_id'])['receipt'] is None
    assert before==digest(store)
