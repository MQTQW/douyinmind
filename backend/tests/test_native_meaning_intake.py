"""Read-only API meaning capture; no real account/provider/browser."""
from copy import deepcopy
import pytest
from tests.test_native_visibility_hint_store import case
from tests.test_native_visibility_review_hints import receiver
from tests.test_assistant_native_receiver import at
from app.assistant.native_receiver import NativeReceiverError


def test_registered_unknown_status_reaches_separate_meaning_sink_without_becoming_candidate():
    calls=[];value,store,transport,events=receiver()
    value.semantic_message_sink=lambda messages,**kw:calls.append((deepcopy(messages),kw))
    transport.rows.append(at(nid='1002',cid='2002',created='102',text='这段挺有用，帮我收着'))
    rows=value.poll(1)
    assert [row['native_comment_id'] for row in rows]==['2002']
    assert len(calls)==1 and {row['native_comment_id'] for row in calls[0][0]}=={'2001','2002'}
    unknown=next(row for row in calls[0][0] if row['native_comment_id']=='2001')
    assert unknown['dispatchable'] is False and 'native_visibility' not in unknown
    assert store.projections[0]['notices'][0]['skip_reason']=='native_visibility_unknown'


def test_meaning_capture_failure_does_not_discard_legal_page_or_prompt_phone_verification():
    value,store,transport,events=receiver()
    def unavailable(*a,**kw):raise RuntimeError('private implementation detail')
    value.semantic_message_sink=unavailable
    assert value.poll(1)==[] and store.pauses==[]
    assert value.last_semantic_message_sink_error_code=='semantic_message_sink_unavailable'


def test_actual_wrong_B_identity_blocks_meaning_sink_before_commit():
    calls=[];value,store,transport,events=receiver();transport.switch=True
    value.semantic_message_sink=lambda *a,**kw:calls.append(True)
    with pytest.raises(NativeReceiverError,match='session_identity_mismatch'):value.poll(1)
    assert calls==[] and store.projections==[]


@pytest.mark.parametrize('status',[None,True,'new_status',42,{},[]])
def test_unknown_visibility_type_does_not_decide_whether_registered_original_is_understood(status):
    calls=[];value,store,transport,events=receiver()
    transport.rows[0]['at']['comment_status']=status
    value.semantic_message_sink=lambda messages,**kw:calls.extend(messages)
    assert value.poll(1)==[]
    assert len(calls)==1 and calls[0]['content']==transport.rows[0]['at']['content']
    assert store.projections[0]['candidates']==[]


def test_pinned_identity_allows_only_readonly_meaning_of_unknown_visibility(case):
    source,ledger,clock,hid,hint=case
    before=source.safe_status();old=ledger.get_hint(hid)
    assert source.native_meaning_checkpoint(hint) is True
    assert source.find_source_by_native_comment(hint['recipient_uid'],hint['video_id'],hint['native_comment_id']) is None
    assert source.safe_status()==before and ledger.get_hint(hid)==old


@pytest.mark.parametrize('change',['uid','sec','receiver','stale','paused','historical'])
def test_meaning_never_uses_display_name_or_visibility_to_bypass_identity(case,change):
    source,ledger,clock,hid,original=case;hint=deepcopy(original)
    if change=='uid':hint['sender_uid']='8801'
    if change=='sec':hint['sender_sec_uid']='different_A'
    if change=='receiver':hint['recipient_uid']='9002'
    if change=='stale':clock[0]+=121
    if change=='paused':source.pause('synthetic_pause')
    if change=='historical':hint['created_time']='99'
    assert source.native_meaning_checkpoint(hint) is False
