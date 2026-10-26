"""Independent adversarial review. Fake pages/stores only; no account/network."""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from app.mention_receiver.browser import NormalPageReceiver, ReceiverIdentity, ReceiverDetailError, ReceiverInterventionRequired
from app.assistant.choice_controller import ChoiceController

A='https://www.douyin.com/user/mixed-review-synthetic-A'
B='https://www.douyin.com/user/mixed-review-synthetic-B'
C='https://www.douyin.com/user/mixed-review-synthetic-C'
VIDEO='7100000000000000001'
CID='7200000000000000002'
OTHER_CID='7300000000000000003'
TEXT='@合成助手 请查看合成资料。'


def target():
    return {'sender_profile_url':A,'receiver_profile_url':B,'video_id':VIDEO,
            'comment_text':TEXT,'native_comment_id':CID,'reference_id':'synthetic-native-reference'}


def notice(key):
    return {'source_profile_url':A,'exact_text':TEXT,'semantic_fingerprint':key,'pre_index':0}


def detail(native):
    return {'ok':True,'unique_count':1,'real_mention':True,'sender_verified':True,'video_verified':True,
            'sender_profile_url':A,'receiver_profile_url':B,'command_text':TEXT,'video_id':VIDEO,
            'native_comment_id':native,'native_comment_id_conflict':False,
            'evidence':{'native_comment_id':native,'real_mention':True}}


def receiver(cards, values):
    navigations=[]; reads=[]
    page=SimpleNamespace(goto=lambda url,**kwargs:navigations.append(url),wait_for_timeout=lambda ms:None)
    value=NormalPageReceiver(page,'123456789',B)
    value.identity=lambda:ReceiverIdentity('123456789','synthetic B')
    value.check_intervention=lambda:None
    value.notifications=lambda:deepcopy(cards)
    def read(card,*,keep_open=False):
        assert keep_open is True
        reads.append(card['semantic_fingerprint'])
        result=values(card,len(reads)) if callable(values) else values[card['semantic_fingerprint']]
        return deepcopy(result)
    value.detail=read
    return value,navigations,reads


def test_semantic_correlation_does_not_promote_missing_dom_cid_or_mutate_source():
    source=target(); before=deepcopy(source)
    value,_,_=receiver([notice('one')],{'one':detail(None)})
    observed=value.open_original_from_notifications(source,native_candidate_correlation=True)
    assert source==before
    assert observed['native_comment_id'] is None and observed['evidence']['native_comment_id'] is None
    assert observed['native_correlation']=={'source_native_comment_id':CID,
                                          'observed_native_comment_id':None,'quality':'semantic_only'}


def test_default_publisher_lookup_does_not_gain_native_semantic_exception():
    value,_,_=receiver([notice('one')],{'one':detail(None)})
    with pytest.raises(ReceiverDetailError): value.open_original_from_notifications(target())
    assert value.last_original_lookup['final_verified']==0


def test_two_missing_id_same_video_comments_remain_ambiguous():
    value,_,_=receiver([notice('one'),notice('two')],{'one':detail(None),'two':detail(None)})
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications(target(),native_candidate_correlation=True)
    assert value.last_original_lookup['final_verified']==0


def test_one_missing_id_cannot_become_unique_by_ignoring_other_same_video_comment():
    value,_,_=receiver([notice('other-known'),notice('unknown')],
                       {'other-known':detail(OTHER_CID),'unknown':detail(None)})
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications(target(),native_candidate_correlation=True)
    assert value.last_original_lookup['final_verified']==0


def test_known_native_id_equality_can_still_disambiguate_two_known_ids():
    value,_,_=receiver([notice('other'),notice('right')],{'other':detail(OTHER_CID),'right':detail(CID)})
    observed=value.open_original_from_notifications(target(),native_candidate_correlation=True)
    assert observed['native_comment_id']==CID
    assert observed['native_correlation']['quality']=='native_equal'


@pytest.mark.parametrize('first,last',[(None,CID),(CID,None)])
def test_quality_change_at_final_reopen_is_a_hold(first,last):
    value,_,_=receiver([notice('one')],lambda card,index:detail(first if index==1 else last))
    with pytest.raises(ReceiverDetailError):
        value.open_original_from_notifications(target(),native_candidate_correlation=True)
    assert value.last_original_lookup['reason_code']=='original_target_changed'


def session(key,state='waiting_library',receiver_profile=B):
    return {'choice_id':key,'state':state,'root_binding':{'receiver_profile_url':receiver_profile},
            'created_at':1,'next_check_at':10**15,
            'options':[{'knowledge_base_id':1,'name':'合成库','ordinal':1}]}


class Store:
    def __init__(self,selected,pending):
        self.selected=selected; self.pending=pending; self.deferred=[]; self.gets=[]
    def list_pending_handoffs(self): return deepcopy(self.pending)
    def get(self,key):
        self.gets.append(key)
        return deepcopy(self.selected) if self.selected and self.selected['choice_id']==key else None
    def list_asks_to_observe(self,*args): raise AssertionError('targeted wakeup must not sweep ask sessions')
    def list_waiting(self,*args): raise AssertionError('targeted wakeup must not sweep waiting sessions')
    def defer_waiting_recheck(self,key,*,seconds): self.deferred.append((key,seconds))


class Adapter:
    expected_profile_url=B
    last_choice_ask_proof=None
    def __init__(self):
        self.reads=[]; self.keys=[]; self.checks=0
        self.page=SimpleNamespace(keyboard=SimpleNamespace(press=self.keys.append))
    def check_intervention(self): self.checks+=1
    def read_choice_replies(self,selected,allowed,*,keep_open=False):
        assert keep_open is True
        self.reads.append(selected['choice_id']); return []


def controller(selected,pending,*,stop=lambda:False):
    store=Store(selected,pending)
    value=ChoiceController(object(),object(),object(),store,conversation=object(),
                           should_stop=stop,admission_lock=threading.RLock())
    fresh=[]; staged=[]
    value._fresh_root=lambda row:fresh.append(row['choice_id']) or ('synthetic-fresh-root','synthetic-resolution')
    value._stage=lambda row,run:staged.append(row['choice_id']) or ['only-target-handoff']
    # These are target-dispatch unit tests, not selected-source authorization
    # tests. Current handoff admission delegates to the fresh-UI recheck gate;
    # the previous minimal session did not implement that full ledger contract.
    # Real selection/UI/revocation behavior is covered with temporary ledgers
    # in test_assistant_native_selected_recovery.py.
    value.rechecked=[]
    def recheck(adapter,row,run_id,source_checkpoint):
        value.rechecked.append(row['choice_id'])
        return value._stage(row,run_id)
    value._recheck_selected_handoff=recheck
    return value,store,fresh,staged


def test_scoped_wakeup_reads_only_target_not_old_handoff_or_old_waiting():
    wanted=session('b'*64); old=session('a'*64,'selected')
    value,store,fresh,staged=controller(wanted,[old])
    adapter=Adapter()
    assert value.collect_pending(adapter,'synthetic-run',choice_id=wanted['choice_id'])==[]
    assert staged==[] and adapter.reads==[wanted['choice_id']]
    assert fresh==[wanted['choice_id'],wanted['choice_id']]
    assert store.deferred==[(wanted['choice_id'],45)]


def test_targeted_handoff_stages_only_same_choice_id():
    wanted=session('b'*64,'selected'); old=session('a'*64,'selected')
    value,store,fresh,staged=controller(wanted,[old,wanted])
    adapter=Adapter()
    assert value.collect_pending(adapter,'synthetic-run',choice_id=wanted['choice_id'])==['only-target-handoff']
    assert staged==[wanted['choice_id']] and adapter.reads==[] and fresh==[]
    assert value.rechecked==[wanted['choice_id']]


def test_targeted_handoff_is_not_starved_by_global_pending_prefix_limit():
    # A real list_pending_handoffs() is globally LIMIT100/ordered oldest first.
    # The selected target can exist but be beyond that returned prefix.
    wanted=session('b'*64,'selected'); old=session('a'*64,'selected')
    value,store,fresh,staged=controller(wanted,[old])
    adapter=Adapter()
    assert value.collect_pending(adapter,'synthetic-run',choice_id=wanted['choice_id'])==['only-target-handoff']
    assert staged==[wanted['choice_id']] and adapter.reads==[]
    assert value.rechecked==[wanted['choice_id']]


def test_targeted_handoff_recheck_refusal_never_falls_back_to_staging():
    wanted=session('b'*64,'selected'); old=session('a'*64,'selected')
    value,store,fresh,staged=controller(wanted,[old,wanted])
    seen=[]
    value._recheck_selected_handoff=lambda adapter,row,run,checkpoint:seen.append(row['choice_id']) or []
    assert value.collect_pending(Adapter(),'synthetic-run',choice_id=wanted['choice_id'])==[]
    assert seen==[wanted['choice_id']] and staged==[]


def test_missing_or_foreign_receiver_target_never_falls_back_to_old_sessions():
    for selected in (None,session('b'*64,receiver_profile=C)):
        value,store,fresh,staged=controller(selected,[session('a'*64,'selected')])
        adapter=Adapter()
        assert value.collect_pending(adapter,'synthetic-run',choice_id='b'*64)==[]
        assert adapter.reads==[] and adapter.keys==[] and staged==[] and fresh==[]


def test_stopped_targeted_wakeup_does_not_navigate_or_inspect_store():
    value,store,fresh,staged=controller(session('b'*64),[],stop=lambda:True)
    adapter=Adapter()
    assert value.collect_pending(adapter,'synthetic-run',choice_id='b'*64)==[]
    assert adapter.checks==0 and store.gets==[] and staged==[]


def test_human_verification_propagates_before_scan_without_cleanup_or_window_actions():
    value,store,fresh,staged=controller(session('b'*64),[])
    adapter=Adapter()
    def challenge(): raise ReceiverInterventionRequired('synthetic challenge')
    adapter.check_intervention=challenge
    with pytest.raises(ReceiverInterventionRequired):
        value.collect_pending(adapter,'synthetic-run',choice_id='b'*64)
    assert adapter.reads==[] and adapter.keys==[] and store.gets==[] and staged==[]
