"""Inert review facts are distinct from candidates, with no network or effects."""
from copy import deepcopy
import json

import pytest

from app.assistant.native_events import NativeEventError, project_page, project_ui_review_hints
from app.assistant.native_receiver import NativeReceiver, NativeReceiverError
from app.assistant.native_source_store import NativeStoreError
from tests.test_assistant_native_receiver import at, page, B, SEC_B, PROFILE_B, PROFILE_A


KEYS={'namespace','source_kind','native_notice_id','native_comment_id','notice_type',
      'recipient_uid','sender_uid','sender_sec_uid','sender_profile_url','receiver_profile_url',
      'video_id','content','created_time','parent_id_hint','relation_status','root_comment_id',
      'parent_comment_id','dispatchable','at_sub_type','native_visibility','visible_category','hidden_category'}


def unknown(nid='1001',cid='2001',**kwargs):
    raw=at(nid=nid,cid=cid,created='102',**kwargs)
    raw['at']['comment_status']=2
    return raw


def hints(rows,channel=9,allowed=True):
    return project_ui_review_hints(page(rows),channel,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:allowed is True and profile==PROFILE_A)


def normal(rows):
    return project_page(page(rows),9,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:profile==PROFILE_A)


def test_complete_hint_preserves_unknown_and_has_exact_twenty_two_keys_not_a_candidate():
    raw=unknown();before=normal([deepcopy(raw)])
    [hint]=hints([raw])
    assert set(hint)==KEYS and len(KEYS)==22
    assert hint['source_kind']=='native_visibility_review_hint'
    assert hint['native_visibility']=='unknown' and hint['visible_category']=='other_int' and hint['hidden_category']=='int0'
    assert hint['dispatchable'] is False and hint['relation_status']=='unknown'
    assert hint['root_comment_id'] is hint['parent_comment_id'] is hint['parent_id_hint'] is None
    assert hint['created_time']=='102'
    assert normal([raw])==before and before['candidates']==[]
    assert before['notices'][0]['skip_reason']=='native_visibility_unknown'


@pytest.mark.parametrize('value',[None,True,False,'2',2.0,{},[]])
def test_only_unknown_integer_status_can_produce_review_hint(value):
    raw=unknown();raw['at']['comment_status']=value
    assert hints([raw])==[]


@pytest.mark.parametrize(('status','hidden'),[(0,0),(1,0),(2,1),(2,None),(2,False),(2,'0')])
def test_known_visible_unavailable_or_unknown_hidden_is_not_this_review_path(status,hidden):
    raw=unknown();raw['at']['comment_status']=status;raw['at']['comment_unvisible']=hidden
    assert hints([raw])==[]


@pytest.mark.parametrize('change',['missing_video','mixed_video','duplicate_cid','schema_video','float_cid',
                                  'missing_text','empty_text','long_text','control_text','missing_time','float_time','huge_time'])
def test_incomplete_unknown_fact_remains_skipped_without_hint(change):
    raw=unknown()
    if change=='missing_video':raw.pop('aweme_id')
    if change=='mixed_video':raw['at']['aweme']['aweme_id']='3002'
    if change=='duplicate_cid':raw['at']['schema_url']+='&cid=2999'
    if change=='schema_video':raw['at']['schema_url']='aweme://aweme/detail/3002?cid=2001'
    if change=='float_cid':raw['at']['schema_url']='aweme://aweme/detail/3001?cid=2.1'
    if change=='missing_text':raw['at'].pop('content')
    if change=='empty_text':raw['at']['content']=' '
    if change=='long_text':raw['at']['content']='x'*4001
    if change=='control_text':raw['at']['content']='x\x00y'
    if change=='missing_time':raw.pop('create_time')
    if change=='float_time':raw['create_time']=102.0
    if change=='huge_time':raw['create_time']='1000000000000'
    before=normal([deepcopy(raw)])
    assert hints([raw])==[] and normal([raw])==before


def test_unregistered_body_and_media_are_not_read_or_exported():
    raw=unknown(sec='unregistered-synthetic-A');raw['at']['content']={'do_not_read':'private'};raw['at']['aweme']=None
    assert hints([raw])==[]


@pytest.mark.parametrize('change',['wrong_recipient','duplicate_notice','bool_type','bool_subtype'])
def test_invalid_page_identity_or_type_fails_closed(change):
    raw=unknown();rows=[raw]
    if change=='wrong_recipient':raw['user_id']='9002'
    if change=='duplicate_notice':rows.append(deepcopy(raw))
    if change=='bool_type':raw['type']=True
    if change=='bool_subtype':raw['at']['sub_type']=True
    with pytest.raises(NativeEventError):hints(rows)


def test_large_identifiers_remain_strings_and_schema_extras_are_not_retained():
    raw=unknown(nid='9999999999999999999',cid='8888888888888888888')
    raw['at']['schema_url']+='&opaque=not-retained'
    [item]=hints([raw])
    assert item['native_notice_id']=='9999999999999999999' and item['native_comment_id']=='8888888888888888888'
    assert 'not-retained' not in json.dumps(item) and 'schema_url' not in item


def test_channel_seven_and_noncomment_at_never_become_hints():
    assert hints([unknown()],channel=7)==[]
    raw=unknown();raw['at']['sub_type']=1
    assert hints([raw])==[]
    raw=unknown();raw['type']=46
    assert hints([raw])==[]


def test_hint_batch_cap_is_sixteen_and_does_not_skip_later_recipient_validation():
    rows=[unknown(nid=str(1000+i),cid=str(2000+i)) for i in range(25)]
    assert len(hints(rows))==16
    rows[-1]['user_id']='9002'
    with pytest.raises(NativeEventError,match='notice_recipient_mismatch'):hints(rows)


class Store:
    receiver_uid=B
    receiver_profile_url=PROFILE_B
    def __init__(self,events):self.events=events;self.projections=[];self.pauses=[];self.fail_record=False
    def pending_count(self):return 0
    def prepare_channel(self,channel,group):self.events.append('prepare');return {'cursor':('0','0')}
    def set_verified(self):self.events.append('verified')
    def record_page(self,context,projection,**kwargs):
        self.events.append('record')
        if self.fail_record:raise NativeStoreError('synthetic_record_failure')
        self.projections.append(deepcopy(projection));return projection['candidates']
    def arm_if_ready(self,_):self.events.append('arm')
    def pause(self,code):self.pauses.append(code)


class Transport:
    def __init__(self,events):self.events=events;self.rows=[unknown()];self.identity_calls=0;self.switch=False
    def verify_identity(self):
        self.events.append('identity');self.identity_calls+=1
        return '9002' if self.switch and self.identity_calls==2 else B
    def get_groups(self):self.events.append('groups');return {9:'601',7:'2'}
    def read_page(self,group,cursor):self.events.append('read');return page(self.rows if group=='601' else [])


def receiver(*,sink=None):
    events=[];store=Store(events);transport=Transport(events)
    value=NativeReceiver(transport,store,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda profile:profile==PROFILE_A,visibility_review_hint_sink=sink)
    return value,store,transport,events


def test_default_without_sink_does_not_even_call_hint_parser(monkeypatch):
    def forbidden(*a,**kw):pytest.fail('default receipt must not parse review hints')
    monkeypatch.setattr('app.assistant.native_receiver.project_ui_review_hints',forbidden)
    value,store,transport,events=receiver()
    assert value.poll(1)==[] and len(store.projections)==1
    assert value.last_visibility_review_sink_error_code is None


def test_sink_receives_only_complete_hint_after_real_identity_and_normal_page_commit():
    captured=[];value,store,transport,events=receiver()
    def sink(items,*,page_fingerprint):
        events.append('sink');captured.append((deepcopy(items),page_fingerprint))
        assert len(store.projections)==1 and transport.identity_calls==2
    value.visibility_review_hint_sink=sink
    assert value.poll(1)==[]
    assert events==['identity','groups','prepare','read','identity','verified','record','arm','sink','verified']
    assert set(captured[0][0][0])==KEYS
    assert captured[0][1]==store.projections[0]['fingerprint']
    assert store.projections[0]['candidates']==[]


def test_sink_failure_keeps_page_skip_and_valid_candidate_and_never_pauses_or_exposes_raw_error():
    calls=[]
    def unavailable(items,*,page_fingerprint):calls.append(True);raise RuntimeError('private-ledger-path-or-raw-response')
    value,store,transport,events=receiver(sink=unavailable)
    transport.rows.append(at(nid='1002',cid='2002',created='102'))
    result=value.poll(1)
    assert [item['native_comment_id'] for item in result]==['2002']
    assert store.projections[0]['notices'][0]['skip_reason']=='native_visibility_unknown'
    assert store.pauses==[] and len(calls)==1
    assert value.last_visibility_review_sink_error_code=='visibility_review_hint_sink_unavailable'
    transport.rows=[]
    assert value.poll(2)==[] and value.last_visibility_review_sink_error_code is None


def test_post_read_identity_mismatch_never_commits_page_or_calls_sink():
    calls=[];value,store,transport,events=receiver(sink=lambda *a,**kw:calls.append(True));transport.switch=True
    with pytest.raises(NativeReceiverError,match='session_identity_mismatch'):value.poll(1)
    assert calls==[] and store.projections==[]


def test_failed_page_commit_never_calls_sink_or_relabels_skip():
    calls=[];value,store,transport,events=receiver(sink=lambda *a,**kw:calls.append(True));store.fail_record=True
    with pytest.raises(NativeReceiverError,match='synthetic_record_failure'):value.poll(1)
    assert calls==[] and store.projections==[]
