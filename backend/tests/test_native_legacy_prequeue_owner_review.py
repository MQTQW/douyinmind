"""Legacy expiry is not evidence: owner fresh review, temp ledgers/fake UI."""
from copy import deepcopy
import json

import pytest

from app.assistant.native_events import project_page
from app.assistant.native_source_store import NativeSourceStore,NativeStoreError
from app.assistant.native_lane import NativeLaneError
from app.mention_agent.choice_evidence import make_choice_envelope
from tests.test_assistant_choice_controller import case,choice_detail
from tests.test_assistant_native_lane import choice_native_system


@pytest.fixture
def legacy(case,tmp_path):
    lane,fake,root,session,adapter=choice_native_system(case)
    c=deepcopy(fake.candidate);clock=case.clock
    store=NativeSourceStore(tmp_path/'native.sqlite3',c['recipient_uid'],c['receiver_profile_url'],
        create=True,clock=lambda:clock[0])
    def project(rows,channel):
        return project_page({'status_code':0,'notice_list_v2':rows},channel,receiver_uid=c['recipient_uid'],
            receiver_sec_uid=c['receiver_profile_url'].rsplit('/',1)[-1],profile_allowed=lambda p:p==c['sender_profile_url'])
    old={'nid_str':'8900000000000000101','type':45,'user_id':c['recipient_uid'],'aweme_id':c['video_id'],
        'create_time':str(int(clock[0])-20),'at':{'sub_type':2,'aweme':{'aweme_id':c['video_id']},
        'user_info':{'uid':c['sender_uid'],'sec_uid':c['sender_sec_uid']},'content':root['envelope']['comment_text'],
        'comment_status':1,'comment_unvisible':0,'schema_url':'aweme://aweme/detail/'+c['video_id']+'?cid='+c['parent_id_hint']}}
    store.record_page(store.prepare_channel(9,'601'),project([old],9),next_cursor=('0','0'),has_more=False)
    store.record_page(store.prepare_channel(7,'2'),project([],7),next_cursor=('0','0'),has_more=False)
    store.arm_if_ready(True);clock[0]+=2;store.set_verified()
    fresh={'nid_str':'8900000000000000102','type':31,'user_id':c['recipient_uid'],'create_time':str(int(clock[0])),
        'comment':{'comment_type':2,'comment_unvisible':0,'parent_id':c['parent_id_hint'],
        'aweme':{'aweme_id':c['video_id']},'comment':{'cid':c['native_comment_id'],'aweme_id':c['video_id'],
        'status':1,'text':c['content'],'user':{'uid':c['sender_uid'],'sec_uid':c['sender_sec_uid']}}}}
    rows=store.record_page(store.prepare_channel(7,'2'),project([fresh],7),next_cursor=('0','0'),has_more=False,allow_live=True)
    sid=rows[0]['source_id']
    first=store.claim_ui_candidate('old-first');store.hold(sid,first['claim_token'],'native_reply_not_matched')
    store.request_ui_recheck(sid,expected_old_code='native_reply_not_matched',operator_reason='explicit_operator_choice_ui_recheck')
    second=store.claim_ui_candidate('old-second');store.hold(sid,second['claim_token'],'native_reply_not_matched')
    store.request_ui_recheck_after_read_fix(sid,expected_old_code='native_reply_not_matched',operator_reason='explicit_operator_verified_read_fix')
    third=store.claim_ui_candidate('expired-owner',lease_seconds=300)
    clock[0]+=301
    lane.source_store=store;calls=[]
    def verify():
        calls.append('fixed_identity_get');store.set_verified();return True
    lane.native_receiver.reverify_identity_checkpoint=verify
    return case,lane,store,sid,root,session,adapter,calls


def saved(store,sid):
    with store._connection() as db:
        row=dict(db.execute('SELECT * FROM sources WHERE source_id=?',(sid,)).fetchone())
        audits={r[0]:r[1] for r in db.execute("SELECT key,value FROM meta WHERE key LIKE 'ui_%'")}
        legacy_count=db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'legacy_prequeue_review_v1:%'").fetchone()[0]
    return row,audits,legacy_count


def test_owner_review_preserves_old_ask_and_audits_and_creates_no_task_or_selection(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    before,old_audits,_=saved(store,sid)
    result=lane.review_and_resume_legacy_prequeue(sid)
    after,audits,count=saved(store,sid)
    assert result['dispatchable'] is False and result['attempts_preserved']==3
    assert after['state']=='fresh' and after['attempts']==3 and count==1 and audits==old_audits
    for key in ('candidate','immutable_hash','first_seen','identity_revision'):assert after[key]==before[key]
    current=case.store.get(session['choice_id'])
    assert current['state']=='waiting_library' and current['selection_ref'] is None and current['child_task_id'] is None
    assert case.queue.list_tasks()['total']==1 and len(case.agent.list_tasks())==1
    assert calls==['fixed_identity_get','fixed_identity_get'] and len(adapter.reads)==1
    with pytest.raises(NativeStoreError,match='already_used'):lane.review_and_resume_legacy_prequeue(sid)


def test_wrong_fresh_body_does_not_resume_or_reserve(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    adapter.text='1';adapter.target=7
    with pytest.raises(NativeLaneError,match='ui_unverified'):lane.review_and_resume_legacy_prequeue(sid)
    row,audits,count=saved(store,sid)
    assert row['state']=='claimed' and row['attempts']==3 and count==0
    assert case.store.get(session['choice_id'])['selection_ref'] is None


@pytest.mark.parametrize('when',['before','during_read'])
def test_possible_agent_effect_before_or_during_fresh_read_blocks_resume(legacy,when):
    case,lane,store,sid,root,session,adapter,calls=legacy
    def add_orphan_agent():
        option=next(o for o in session['options'] if o['knowledge_base_id']==8)
        binding=case.registry.authorize_snapshot(option['authorization'],action='import')
        detail=choice_detail(case,session)
        env=make_choice_envelope({'source_profile_url':detail['sender_profile_url'],'exact_text':detail['command_text'],
            'semantic_fingerprint':'synthetic-existing-effect'},detail,binding,source_mode='live_ui')
        case.agent.put_binding(binding.to_dict());case.agent.enqueue(binding.binding_id,env)
    if when=='before':add_orphan_agent()
    else:adapter.on_read=add_orphan_agent
    with pytest.raises(NativeLaneError,match='effects_present'):lane.review_and_resume_legacy_prequeue(sid)
    assert saved(store,sid)[0]['state']=='claimed' and saved(store,sid)[2]==0


def test_prior_selected_intent_is_not_eligible_for_legacy_recovery(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    assert case.controller.collect_pending(adapter,'prior-selected',choice_id=session['choice_id'])
    with pytest.raises(NativeLaneError):lane.review_and_resume_legacy_prequeue(sid)
    assert saved(store,sid)[2]==0 and case.store.get(session['choice_id'])['child_task_id'] is None


def test_revoke_during_read_is_not_an_authorization_snapshot_upgrade(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    auth=session['authorization_snapshot']
    adapter.on_read=lambda:case.registry.revoke_user(auth['user_id'])
    with pytest.raises(NativeLaneError):lane.review_and_resume_legacy_prequeue(sid)
    assert saved(store,sid)[0]['state']=='claimed' and saved(store,sid)[2]==0


def test_store_never_accepts_caller_true_instead_of_owner_cross_ledger_review(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    with pytest.raises(NativeStoreError,match='review_required'):store._commit_legacy_prequeue_resume(True)
    assert saved(store,sid)[2]==0


def test_second_identity_delay_cannot_restamp_old_fresh_UI(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    def delayed():
        calls.append('fixed_identity_get')
        if len(calls)==2:case.clock[0]+=121
        store.set_verified();return True
    lane.native_receiver.reverify_identity_checkpoint=delayed
    with pytest.raises(NativeLaneError):lane.review_and_resume_legacy_prequeue(sid)
    assert saved(store,sid)[2]==0 and case.store.get(session['choice_id'])['selection_ref'] is None


def test_orphan_ingestion_job_for_allowed_video_scope_is_unknown_not_zero_effect(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    binding=next(o['authorization']['binding_id'] for o in session['options'] if o['knowledge_base_id']==8)
    with case.agent._tx() as db:
        db.execute('CREATE TABLE agent_ingestion_jobs(job_id TEXT PRIMARY KEY,binding_id TEXT,video_id TEXT)')
        db.execute('CREATE TABLE agent_task_ingestions(task_id TEXT,job_id TEXT)')
        db.execute('INSERT INTO agent_ingestion_jobs VALUES(?,?,?)',('orphan-job',binding,session['root_envelope']['video_id']))
    with pytest.raises(NativeLaneError,match='effects_unknown'):lane.review_and_resume_legacy_prequeue(sid)
    assert saved(store,sid)[2]==0


def test_registry_change_at_final_audit_cannot_be_silently_upgraded(legacy):
    case,lane,store,sid,root,session,adapter,calls=legacy
    original=lane._legacy_no_effects_audit
    def revoke_at_final(candidate,current,envelope=None):
        result=original(candidate,current,envelope)
        if envelope is not None:case.registry.revoke_user(session['authorization_snapshot']['user_id'])
        return result
    lane._legacy_no_effects_audit=revoke_at_final
    with pytest.raises(NativeLaneError):lane.review_and_resume_legacy_prequeue(sid)
    assert saved(store,sid)[2]==0 and saved(store,sid)[0]['state']=='claimed'
