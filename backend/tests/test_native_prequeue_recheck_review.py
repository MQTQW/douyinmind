"""Independent explicit prequeue-recheck attacks; fake/temp native ledgers only."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app.assistant.native_events import canonical_profile, project_page
from app.assistant.native_source_store import NativeSourceStore, NativeStoreError


B='9001'
SEC_B='independent_recheck_B'
A='8001'
SEC_A='independent_recheck_A'
PROFILE_B=canonical_profile(SEC_B)
OLD_CODE='native_reply_not_matched'
REASON='explicit_operator_choice_ui_recheck'


def project(rows,channel):
    return project_page({'status_code':0,'notice_list_v2':rows},channel,receiver_uid=B,
        receiver_sec_uid=SEC_B,profile_allowed=lambda _:True)


def raw_reply(nid='1001',cid='2001'):
    return {'nid_str':nid,'type':31,'user_id':B,'create_time':'102',
        'comment':{'comment_type':2,'comment_unvisible':0,'parent_id':'1900',
            'aweme':{'aweme_id':'3001'},'comment':{'cid':cid,'aweme_id':'3001','status':1,
                'text':'收藏到合成库吧','user':{'uid':A,'sec_uid':SEC_A}}}}


@pytest.fixture
def case(tmp_path):
    now=[100.0]
    path=tmp_path/'sources.sqlite3'
    store=NativeSourceStore(path,B,PROFILE_B,create=True,clock=lambda:now[0])
    for channel,group in ((9,'601'),(7,'2')):
        context=store.prepare_channel(channel,group)
        store.record_page(context,project([],channel),next_cursor=('0','0'),has_more=False)
    store.arm_if_ready(True)
    now[0]=102.0
    store.set_verified()  # Invented fixture state, not a production freshness call.
    context=store.prepare_channel(7,'2')
    item=store.record_page(context,project([raw_reply()],7),next_cursor=('0','0'),
        has_more=False,allow_live=True)[0]
    claim=store.claim_ui_candidate('independent-owner',lease_seconds=300)
    store.hold(claim['source_id'],claim['claim_token'],OLD_CODE)
    return {'store':store,'path':path,'now':now,'source_id':item['source_id'],'old_token':claim['claim_token']}


def request(store,source_id,**changes):
    return store.request_ui_recheck(source_id,expected_old_code=changes.get('expected_old_code',OLD_CODE),
        operator_reason=changes.get('operator_reason',REASON))


def snapshot(store):
    with store._connection() as db:
        return {name:[tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY 1')]
            for name in ('sources','meta','notice_refs','observations','identities','channels')}


def source_row(store,source_id):
    with store._connection() as db:
        return dict(db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone())


def assert_denied_without_semantic_change(case,**changes):
    before=snapshot(case['store'])
    with pytest.raises(NativeStoreError) as error:
        request(case['store'],case['source_id'],**changes)
    assert 'fixture-secret' not in str(error.value)
    assert snapshot(case['store'])==before


def test_allowed_recheck_keeps_source_history_and_invalidates_old_claim(case):
    store=case['store'];source_id=case['source_id']
    before=source_row(store,source_id)
    request(store,source_id)
    after=source_row(store,source_id)
    assert after['state']=='fresh' and after['attempts']==1
    permitted={'state','hold_code','claim_owner','claim_token','lease_until'}
    assert {k:v for k,v in after.items() if k not in permitted}=={k:v for k,v in before.items() if k not in permitted}
    assert after['claim_token'] is None and after['lease_until'] is None
    with pytest.raises(NativeStoreError):
        store.hold(source_id,case['old_token'],OLD_CODE)
    with store._connection() as db:
        audit=db.execute('SELECT value FROM meta WHERE key=?',('ui_recheck_v1:'+source_id,)).fetchall()
    assert len(audit)==1
    assert json.loads(audit[0][0])


def test_lost_recheck_ack_and_reopen_do_not_allow_a_second_request(case):
    request(case['store'],case['source_id'])  # Durable success; caller loses return.
    reopened=NativeSourceStore(case['path'],B,PROFILE_B,clock=lambda:case['now'][0])
    before=snapshot(reopened)
    with pytest.raises(NativeStoreError):request(reopened,case['source_id'])
    assert snapshot(reopened)==before
    claim=reopened.claim_ui_candidate('second-owner',lease_seconds=300)
    assert claim['source_id']==case['source_id']
    assert claim['claim_token']!=case['old_token']
    assert source_row(reopened,case['source_id'])['attempts']==2
    reopened.hold(claim['source_id'],claim['claim_token'],OLD_CODE)
    assert_denied_without_semantic_change({**case,'store':reopened})


def test_two_independent_stores_cannot_both_authorize_recheck(case):
    second=NativeSourceStore(case['path'],B,PROFILE_B,clock=lambda:case['now'][0])
    barrier=threading.Barrier(2)
    def act(store):
        barrier.wait(timeout=5)
        try:request(store,case['source_id']);return 'accepted'
        except NativeStoreError:return 'denied'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(act,store) for store in (case['store'],second)]
        outcomes=[future.result(timeout=20) for future in futures]
    assert sorted(outcomes)==['accepted','denied']
    assert source_row(second,case['source_id'])['attempts']==1
    with second._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM meta WHERE key=?',('ui_recheck_v1:'+case['source_id'],)).fetchone()[0]==1


@pytest.mark.parametrize('column,value',[
    ('has_at',1),('has_reply',0),('historical',1),('attempts',0),('attempts',2),
    ('state','claimed'),('state','planned'),('state','admitted'),('state','held_conflict'),
    ('hold_code','native_unknown_lease'),('hold_code','binding_changed'),
    ('plan','{}'),('task_id','spent-task'),('reference_id','old-reference'),
])
def test_spent_mixed_historical_or_unknown_sources_are_never_reopened(case,column,value):
    with case['store']._connection(write=True) as db:
        db.execute('UPDATE sources SET '+column+'=? WHERE source_id=?',(value,case['source_id']))
    assert_denied_without_semantic_change(case)


@pytest.mark.parametrize('verified,paused',[
    ('0',''),('NaN',''),('Infinity',''),('fixture-secret',''),('108',''),('102','verification_required')
])
def test_bad_or_paused_native_verification_never_reopens_source(case,verified,paused):
    with case['store']._connection(write=True) as db:
        db.execute("UPDATE meta SET value=? WHERE key='verified_at'",(verified,))
        db.execute("UPDATE meta SET value=? WHERE key='pause_code'",(paused,))
    assert_denied_without_semantic_change(case)


def test_intake_freshness_still_expires_at_120_seconds(case):
    case['now'][0]=223.0
    assert_denied_without_semantic_change(case)


@pytest.mark.parametrize('mutation',["UPDATE identities SET state='conflict'","UPDATE identities SET revision=revision+1"])
def test_changed_native_identity_pin_is_not_rebound_by_operator(case,mutation):
    with case['store']._connection(write=True) as db:db.execute(mutation)
    assert_denied_without_semantic_change(case)


def test_same_text_different_native_cid_remains_ambiguous(case):
    context=case['store'].prepare_channel(7,'2')
    case['store'].record_page(context,project([raw_reply('1002','2002')],7),
        next_cursor=('0','0'),has_more=False,allow_live=True)
    assert_denied_without_semantic_change(case)


@pytest.mark.parametrize('changes',[
    {'expected_old_code':'native_unknown_lease'}, {'operator_reason':'fixture-secret'},
    {'operator_reason':True}, {'operator_reason':REASON+'\n'},
])
def test_only_exact_explicit_reason_and_expected_old_code_are_accepted(case,changes):
    assert_denied_without_semantic_change(case,**changes)
