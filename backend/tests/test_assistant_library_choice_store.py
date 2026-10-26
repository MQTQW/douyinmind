"""Durable library-choice CAS/recovery, temporary files and synthetic evidence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.assistant.library_choice import LibraryChoiceStore, LibraryChoiceError, DATABASE_NAME
from tests.library_choice_fixture import choice_fixture, visible, selection, reserve


def test_all_local_original_root_and_positive_selected_binding_never_use_sentinel_library(tmp_path):
    case=choice_fixture(tmp_path,all_local_root=True);visible(case);row=reserve(case)
    assert row['root_binding']['query_scope']=='all_local' and row['root_binding']['knowledge_base_id'] is None
    assert row['authorization_snapshot']['query_scope']=='all_local'
    assert row['selected_kb_id']==7 and row['selected_authorization']['knowledge_base_id']==7
    assert 'query_scope' not in row['selected_authorization']


def test_unknown_ask_cannot_claim_or_poison_a_platform_reply_reference(tmp_path):
    case=choice_fixture(tmp_path)
    with pytest.raises(LibraryChoiceError,match='unknown_ask_cannot_claim_reference'):
        case.store.record_ask_outcome(case.created['choice_id'],ask_ref=case.ask_ref,outcome='delivery_unknown',ask_reply_ref='not-read-back')
    row=case.store.get(case.created['choice_id'])
    assert row['state']=='awaiting_ask' and row['ask_reply_ref'] is None
    assert visible(case)['state']=='waiting_library'


@pytest.mark.parametrize('body',['1和2','不要保存到编程库','我想问编程库','如果选1','9'])
def test_choice_actual_body_must_be_one_frozen_option_not_caller_preference(tmp_path,body):
    case=choice_fixture(tmp_path);visible(case)
    with pytest.raises(LibraryChoiceError,match='text_not_single_option'):
        reserve(case,envelope=selection(case,text=body))
    assert case.store.get(case.created['choice_id'])['state']=='waiting_library'


def test_catalog_ordinal_is_preserved_when_options_are_filtered(tmp_path):
    case=choice_fixture(tmp_path,option_ordinals={7:3,8:5});visible(case)
    with pytest.raises(LibraryChoiceError,match='text_not_single_option'):
        reserve(case,envelope=selection(case,text='2',target=8),target=8)
    row=reserve(case,envelope=selection(case,text='5',target=8),target=8)
    assert row['selected_kb_id']==8 and row['options'][1]['ordinal']==5


def test_digit_library_name_conflict_is_ambiguous_but_explicit_name_is_allowed(tmp_path):
    case=choice_fixture(tmp_path,option_names={7:'编程库',8:'1'});visible(case)
    with pytest.raises(LibraryChoiceError,match='text_not_single_option'):
        reserve(case,envelope=selection(case,text='1',target=8),target=8)
    assert reserve(case,envelope=selection(case,text='存到1',target=8),target=8)['selected_kb_id']==8


def test_public_reply_label_is_derived_but_original_selection_body_is_not_rewritten(tmp_path):
    case=choice_fixture(tmp_path);visible(case)
    envelope=selection(case,text='回复@6：1')
    envelope['evidence'].update(receiver_reply_label='@6',selection_value='1')
    row=reserve(case,envelope=envelope)
    assert row['selection_envelope']['comment_text']=='回复@6：1'
    assert row['selection_envelope']['evidence']['real_mention'] is False


def test_convenient_selection_value_cannot_replace_actual_public_body(tmp_path):
    case=choice_fixture(tmp_path);visible(case)
    envelope=selection(case,text='回复@6：不要保存')
    envelope['evidence'].update(receiver_reply_label='@6',selection_value='1')
    with pytest.raises((ValueError,LibraryChoiceError)):reserve(case,envelope=envelope)
    assert case.store.get(case.created['choice_id'])['state']=='waiting_library'


@pytest.mark.parametrize('state',['awaiting_ask','waiting_library','ask_unknown','ask_held'])
def test_root_recovery_lookup_and_authorization_hold_preserve_unsent_menu(tmp_path,state):
    case=choice_fixture(tmp_path)
    if state=='waiting_library':visible(case)
    elif state=='ask_unknown':case.store.record_ask_outcome(case.created['choice_id'],ask_ref=case.ask_ref,outcome='delivery_unknown')
    elif state=='ask_held':case.store.record_ask_outcome(case.created['choice_id'],ask_ref=case.ask_ref,outcome='failed_no_submit')
    before=case.store.get_for_root_task('root-task-1')
    assert before['state']==state and before['choice_id']==case.created['choice_id']
    held=case.store.hold_waiting(before['choice_id'],'binding_changed')
    assert held['state']=='needs_review' and held['revision']==before['revision']+1
    for key in ('ask_ref','ask_text','options','ask_reply_ref','ask_proof','root_binding','root_envelope','authorization_snapshot'):
        assert held[key]==before[key]
    assert case.store.list_waiting(case.root_binding.receiver_profile_url)==[]
    assert case.store.hold_waiting(before['choice_id'])['revision']==held['revision']
    assert case.store.get_for_root_task('missing') is None
    with sqlite3.connect(case.store.path.as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute("SELECT metadata FROM choice_events WHERE choice_id=? AND event='waiting_held'",(held['choice_id'],)).fetchall()
    assert len(rows)==1 and json.loads(rows[0][0])['reason_code']=='binding_changed'


def test_selected_and_handoff_never_roll_back_when_root_is_held_late(tmp_path):
    case=choice_fixture(tmp_path);visible(case);selected=reserve(case)
    for state in ('selected','handoff_claimed','handed_off'):
        current=case.store.get(selected['choice_id'])
        assert current['state']==state and case.store.hold_waiting(current['choice_id'])==current
        assert case.store.get_for_root_task('root-task-1')['state']==state
        if state=='selected':claim=case.store.claim_handoff(current['choice_id'],run_id='fixture-run')
        elif state=='handoff_claimed':case.store.finish_handoff(current['choice_id'],claim['handoff_token'],'actual-child-task')


def test_selected_reference_lookup_is_scoped_by_actual_actor_receiver_instance_and_source(tmp_path):
    case=choice_fixture(tmp_path);visible(case);row=reserve(case)
    query=dict(sender=case.root_binding.sender_profile_url,receiver=case.root_binding.receiver_profile_url,
               source_mode='isolated',reference_id=row['selection_ref'],instance_id=case.root_binding.instance_id)
    assert case.store.get_for_selection_reference(**query)['choice_id']==row['choice_id']
    for key,value in (('sender','https://www.douyin.com/user/other'),('receiver','https://www.douyin.com/user/other'),
                      ('source_mode','live_ui'),('reference_id','other'),('instance_id','other')):
        assert case.store.get_for_selection_reference(**{**query,key:value}) is None


def test_original_task_id_cannot_be_reused_for_another_root(tmp_path):
    first=choice_fixture(tmp_path);second=choice_fixture(tmp_path,store=first.store,root_number=2)
    with pytest.raises(LibraryChoiceError,match='root_task_conflict'):
        first.store.create(root_task_id='root-task-1',root_binding=second.root_binding.to_dict(),
            root_envelope=second.root_envelope,ask_ref='third-intent',ask_text=second.ask_text,options=second.options,
            authorization=second.created['authorization_snapshot'])
    assert first.store.get_for_root_task('root-task-1')['choice_id']==first.created['choice_id']


@pytest.mark.parametrize('scope',['verified_ask_parent','unique_pending_choice_in_verified_root'])
@pytest.mark.parametrize('mention',[False,True])
def test_both_honest_parent_quality_modes_and_raw_mention_are_preserved(tmp_path,scope,mention):
    case=choice_fixture(tmp_path);visible(case)
    envelope=selection(case,parent_scope=scope,mention=mention)
    selected=reserve(case,envelope=envelope)
    assert selected['state']=='selected' and selected['selection_envelope']==envelope
    assert selected['selection_envelope']['comment_text']=='1'
    assert selected['selection_envelope']['evidence']['real_mention'] is mention
    assert selected['selection_envelope']['native_comment_id'] is None
    assert selected['selected_kb_id']==7 and selected['selected_authorization']==case.options[0]['authorization']
    assert 'selection_token' not in case.store.get(selected['choice_id'])


def test_unknown_ask_never_lists_or_selects_until_same_ask_is_read_back(tmp_path):
    case=choice_fixture(tmp_path)
    unknown=case.store.record_ask_outcome(case.created['choice_id'],ask_ref=case.ask_ref,outcome='delivery_unknown')
    assert unknown['state']=='ask_unknown'
    assert case.store.list_waiting(case.root_binding.receiver_profile_url)==[]
    with pytest.raises((LibraryChoiceError,ValueError)):reserve(case)
    waiting=visible(case)
    assert waiting['state']=='waiting_library' and len(case.store.list_waiting(case.root_binding.receiver_profile_url))==1
    selected=reserve(case)
    late=case.store.record_ask_outcome(selected['choice_id'],ask_ref=case.ask_ref,outcome='delivery_unknown')
    assert late['state']=='selected' and late['revision']==selected['revision']


def test_root_replay_never_replaces_ask_or_options_or_authorization(tmp_path):
    case=choice_fixture(tmp_path);visible(case)
    original=case.store.get(case.created['choice_id'])
    changed=deepcopy(case.options);changed[0]['name']='已经改名的库';changed[0]['authorization']['grant_revision']=2
    repeated=case.store.create(root_task_id='new-local-attempt-id',root_binding=case.root_binding.to_dict(),
        root_envelope=case.root_envelope,ask_ref='another-local-ask-intent',ask_text='不要再次发表这个新问句',
        options=changed,authorization=original['authorization_snapshot'])
    assert repeated['choice_id']==original['choice_id'] and repeated['created'] is False
    assert repeated['ask_text']==original['ask_text'] and repeated['options']==original['options']
    assert repeated['authorization_snapshot']==original['authorization_snapshot']
    assert repeated['state']=='waiting_library'


@pytest.mark.parametrize('field,value',[
    ('root_reference_id','different-root'),('root_comment_text','不同的原保存指令'),
    ('ask_ref','other-ask'),('ask_reply_ref','other-ask-reply'),('ask_text','不同的问库正文'),
    ('instance_id','other-instance'),('choice_id','f'*64),('sender_profile_url','https://www.douyin.com/user/foreign'),
    ('video_id','7999999000000009999'),('root_chain_verified',False),('unique_pending_choice',False),
    ('ask_visible_unique',False),('receiver_reply_target_verified',False),('unique_count',2),
])
def test_wrong_or_missing_frozen_parent_or_actor_never_reserves(tmp_path,field,value):
    case=choice_fixture(tmp_path);visible(case);envelope=selection(case)
    envelope['evidence'][field]=value
    with pytest.raises((LibraryChoiceError,ValueError)):reserve(case,envelope=envelope)
    assert case.store.get(case.created['choice_id'])['state']=='waiting_library'
    with sqlite3.connect(case.store.path.as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('SELECT count(*) FROM choice_selection_refs').fetchone()[0]==0


def test_new_permission_version_or_nonoption_library_is_not_silently_substituted(tmp_path):
    case=choice_fixture(tmp_path);visible(case);envelope=selection(case)
    altered=deepcopy(case.options[0]['authorization']);altered['grant_version']+=1
    with pytest.raises(LibraryChoiceError,match='authorization_changed'):
        case.store.reserve_selection(case.created['choice_id'],expected_revision=2,selection_ref=envelope['reference_id'],
            selection_envelope=envelope,selected_kb_id=7,selected_authorization=altered)
    with pytest.raises(LibraryChoiceError):
        case.store.reserve_selection(case.created['choice_id'],expected_revision=2,selection_ref=envelope['reference_id'],
            selection_envelope=envelope,selected_kb_id=999,selected_authorization=case.options[0]['authorization'])
    assert case.store.get(case.created['choice_id'])['state']=='waiting_library'
    assert case.store.get(case.created['choice_id'])['child_task_id'] is None


def test_concurrent_different_replies_choose_exactly_one_frozen_option(tmp_path):
    case=choice_fixture(tmp_path);waiting=visible(case)
    inputs=[(selection(case,text='1',target=7),7),(selection(case,text='2',target=8),8)]
    def choose(value):
        envelope,target=value
        local=LibraryChoiceStore(case.store.path)
        try:
            return local.reserve_selection(case.created['choice_id'],expected_revision=waiting['revision'],
                selection_ref=envelope['reference_id'],selection_envelope=envelope,selected_kb_id=target,
                selected_authorization=next(option['authorization'] for option in case.options if option['knowledge_base_id']==target))
        except LibraryChoiceError:return None
    with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(choose,inputs))
    assert sum(result is not None for result in results)==1
    final=case.store.get(case.created['choice_id'])
    assert final['state']=='selected' and final['selected_kb_id'] in (7,8)
    with sqlite3.connect(case.store.path.as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('SELECT count(*) FROM choice_selection_refs').fetchone()[0]==1


def test_same_native_selection_cannot_be_consumed_by_two_choices(tmp_path):
    first=choice_fixture(tmp_path);visible(first)
    second=choice_fixture(tmp_path,store=first.store,root_number=2);visible(second)
    one=selection(first,native='7999999000000000777')
    two=selection(second,native='7999999000000000777')
    assert one['reference_id']==two['reference_id']
    reserve(first,envelope=one)
    with pytest.raises(LibraryChoiceError,match='already_used'):reserve(second,envelope=two)
    assert second.store.get(second.created['choice_id'])['state']=='waiting_library'


def test_selected_intent_and_child_key_survive_reopen_and_fresh_readback_replay(tmp_path):
    case=choice_fixture(tmp_path);visible(case);first=reserve(case)
    case.store=LibraryChoiceStore(case.store.path)
    replay=reserve(case,envelope=selection(case))
    assert replay['child_key']==first['child_key'] and replay['revision']==first['revision']
    assert replay['selection_envelope']==first['selection_envelope']
    assert len(case.store.list_pending_handoffs())==1


def test_handoff_crash_gap_reclaim_only_links_same_child_and_stale_claim_cannot_change_it(tmp_path):
    clock=[1792640000.0]
    case=choice_fixture(tmp_path,clock=lambda:clock[0]);visible(case);selected=reserve(case)
    first=case.store.claim_handoff(selected['choice_id'],run_id='owner-one',lease_seconds=30)
    assert case.store.claim_handoff(selected['choice_id'],run_id='owner-two') is None
    # The product queue may already contain this child. A reopened caller must
    # look it up by the real selection reference rather than repeat execution.
    clock[0]+=31;case.store=LibraryChoiceStore(case.store.path,clock=lambda:clock[0])
    second=case.store.claim_handoff(selected['choice_id'],run_id='owner-two')
    assert second['child_key']==first['child_key'] and second['handoff_token']!=first['handoff_token']
    with pytest.raises(LibraryChoiceError,match='handoff_stale'):
        case.store.finish_handoff(selected['choice_id'],first['handoff_token'],'actual-existing-child')
    done=case.store.finish_handoff(selected['choice_id'],second['handoff_token'],'actual-existing-child')
    assert done['state']=='handed_off'
    assert case.store.claim_handoff(selected['choice_id'],run_id='owner-three') is None
    assert case.store.finish_handoff(selected['choice_id'],'stale','actual-existing-child')['child_task_id']=='actual-existing-child'
    with pytest.raises(LibraryChoiceError,match='child_conflict'):
        case.store.finish_handoff(selected['choice_id'],second['handoff_token'],'another-child')


def test_waiting_recheck_rotation_survives_restart_without_marking_choice_selected(tmp_path):
    clock=[1792640000.0]
    first=choice_fixture(tmp_path,clock=lambda:clock[0]);visible(first)
    clock[0]+=1
    second=choice_fixture(tmp_path,store=first.store,root_number=2,clock=lambda:clock[0]);visible(second)
    receiver=first.root_binding.receiver_profile_url
    assert first.store.list_waiting(receiver,limit=1)[0]['choice_id']==first.created['choice_id']
    initial=first.store.get(first.created['choice_id'])
    first.store.defer_waiting_recheck(first.created['choice_id'],seconds=120)
    assert first.store.list_waiting(receiver,limit=1)[0]['choice_id']==second.created['choice_id']
    reopened=LibraryChoiceStore(first.store.path,clock=lambda:clock[0])
    assert reopened.list_waiting(receiver,limit=1)[0]['choice_id']==second.created['choice_id']
    assert reopened.get(first.created['choice_id'])==initial
    reopened.defer_waiting_recheck(second.created['choice_id'],seconds=45)
    assert reopened.list_waiting(receiver)==[]
    clock[0]+=121
    assert {row['choice_id'] for row in reopened.list_waiting(receiver)}=={first.created['choice_id'],second.created['choice_id']}
    assert all(row['state']=='waiting_library' for row in reopened.list_waiting(receiver))


def test_missing_or_changed_initialized_choice_storage_is_not_recreated(tmp_path):
    path=tmp_path/'not-created'/DATABASE_NAME
    with pytest.raises(LibraryChoiceError):LibraryChoiceStore(path)
    assert not path.parent.exists()
    case=choice_fixture(tmp_path)
    original=case.store.path.read_bytes();marker=case.store.marker_path.read_bytes()
    case.store.path.unlink()
    with pytest.raises(LibraryChoiceError):LibraryChoiceStore.prepare(case.store.path)
    assert not case.store.path.exists() and case.store.marker_path.read_bytes()==marker
    case.store.path.write_bytes(original);case.store.marker_path.unlink()
    before=hashlib.sha256(case.store.path.read_bytes()).hexdigest()
    with pytest.raises(LibraryChoiceError):LibraryChoiceStore.prepare(case.store.path)
    assert hashlib.sha256(case.store.path.read_bytes()).hexdigest()==before
    assert not case.store.marker_path.exists()
