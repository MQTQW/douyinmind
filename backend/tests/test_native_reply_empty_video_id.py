"""Synthetic native notices only; no network, account, SQLite or browser."""
from copy import deepcopy
import pytest
from app.assistant.native_events import (NativeEventError, canonical_profile,
    notice_identifier_diagnostic, project_page)

B='990000000000000001';A='880000000000000001'
VIDEO='770000000000000001';OTHER_VIDEO='770000000000000002'
CID='660000000000000001';NID='550000000000000001'
SEC_B='synthetic_receiver';SEC_A='synthetic_sender'
MISSING=object()


def reply(*,inner='',wrapper=VIDEO,outer=MISSING,parent='440000000000000001'):
    value={'nid_str':NID,'type':31,'user_id':B,'create_time':'1700000000',
        'comment':{'comment_type':2,'comment_unvisible':0,'parent_id':parent,
            'aweme':{'aweme_id':wrapper},'comment':{'cid':CID,'aweme_id':inner,'status':1,
                'text':'这个合成视频讲了什么事情','user':{'uid':A,'sec_uid':SEC_A}}}}
    if inner is MISSING:del value['comment']['comment']['aweme_id']
    if wrapper is MISSING:del value['comment']['aweme']['aweme_id']
    if outer is not MISSING:value['aweme_id']=outer
    return value


def project(rows,*,channel=7):
    return project_page({'status_code':0,'notice_list_v2':rows},channel,
        receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda p:p==canonical_profile(SEC_A))


def test_exact_empty_inner_uses_valid_same_wrapper_and_matches_existing_full_projection():
    empty=project([reply()]);full=project([reply(inner=VIDEO)])
    assert empty==full
    item=empty['candidates'][0]
    assert item['video_id']==VIDEO and item['native_comment_id']==CID and item['native_notice_id']==NID
    assert item['source_kind']=='native_reply_notice_candidate' and item['comment_type']==2
    assert item['dispatchable'] is False and item['relation_status']=='unknown'
    assert item['root_comment_id'] is None and item['parent_comment_id'] is None


@pytest.mark.parametrize('inner,wrapper',[(VIDEO,VIDEO),(int(VIDEO),VIDEO),(VIDEO,int(VIDEO)),('',int(VIDEO))])
def test_existing_valid_numeric_shapes_remain_exact(inner,wrapper):
    assert project([reply(inner=inner,wrapper=wrapper)])['candidates'][0]['video_id']==VIDEO


class EmptyStringSubclass(str):pass


@pytest.mark.parametrize('inner',[MISSING,None,False,True,0,0.0,[],{},' ','\t','\n','0','001','+1','1.0','bad',EmptyStringSubclass('')])
def test_nonexact_empty_inner_still_fails_without_fallback(inner):
    with pytest.raises(NativeEventError) as caught:project([reply(inner=inner)])
    assert caught.value.code=='invalid_notice_identifier'
    fact=notice_identifier_diagnostic(caught.value)
    assert fact['field_role']=='reply_video_id'


@pytest.mark.parametrize('wrapper',[MISSING,None,False,True,0,0.0,[],{},'',' ','\t','0','001','+1','bad',EmptyStringSubclass('')])
def test_empty_inner_never_replaces_invalid_wrapper(wrapper):
    with pytest.raises(NativeEventError) as caught:project([reply(wrapper=wrapper)])
    assert caught.value.code=='invalid_notice_identifier'
    assert notice_identifier_diagnostic(caught.value)['field_role']=='reply_wrapper_video_id'


def test_missing_or_nonobject_wrapper_does_not_fallback():
    for value in (MISSING,None,False,[],VIDEO):
        row=reply()
        if value is MISSING:del row['comment']['aweme']
        else:row['comment']['aweme']=value
        with pytest.raises(NativeEventError,match='invalid_notice_shape'):project([row])


@pytest.mark.parametrize('outer',[VIDEO,int(VIDEO)])
def test_present_outer_must_match_valid_wrapper(outer):
    assert project([reply(outer=outer)])['candidates'][0]['video_id']==VIDEO


@pytest.mark.parametrize('outer',[None,False,True,0,'',' ','001','bad',OTHER_VIDEO])
def test_present_bad_or_conflicting_outer_is_not_ignored(outer):
    with pytest.raises(NativeEventError):project([reply(outer=outer)])


def test_nonempty_inner_conflict_is_still_rejected():
    with pytest.raises(NativeEventError,match='notice_video_mismatch'):
        project([reply(inner=OTHER_VIDEO)])


@pytest.mark.parametrize('parent',[None,0,'0','440000000000000001'])
def test_parent_hint_never_becomes_root_or_execution_right(parent):
    item=project([reply(parent=parent)])['candidates'][0]
    assert item['parent_id_hint']==(None if parent is None else str(parent))
    assert item['root_comment_id'] is None and item['parent_comment_id'] is None and item['dispatchable'] is False


@pytest.mark.parametrize('field,value',[('cid',''),('cid',False),('text',''),('text','bad\x00text')])
def test_other_comment_fields_are_not_relaxed(field,value):
    row=reply();row['comment']['comment'][field]=value
    with pytest.raises(NativeEventError):project([row])


def test_receiver_and_author_scope_still_precede_any_candidate():
    row=reply();row['user_id']='990000000000000002'
    with pytest.raises(NativeEventError,match='notice_recipient_mismatch'):project([row])
    row=reply();row['comment']['comment']['user']['sec_uid']='unregistered_synthetic'
    row['comment']['comment']['text']=None;row['comment']['aweme']=None
    result=project([row])
    assert result['candidates']==[] and result['skipped']==1


@pytest.mark.parametrize('status,hidden',[(0,0),(1,1),(2,0),(True,0),(1,False)])
def test_visibility_rules_are_unchanged(status,hidden):
    row=reply();row['comment']['comment']['status']=status;row['comment']['comment_unvisible']=hidden
    result=project([row]);assert result['candidates']==[] and result['skipped']==1


@pytest.mark.parametrize('subtype',[0,1,11,12])
def test_nonreply_comment_subtypes_do_not_become_reply_candidates(subtype):
    row=reply();row['comment']['comment_type']=subtype
    assert project([row])['candidates']==[]


def test_channel9_at_video_rules_are_not_relaxed():
    row={'nid_str':NID,'type':45,'user_id':B,'aweme_id':'','create_time':'1700000000',
        'at':{'sub_type':2,'aweme':{'aweme_id':VIDEO},'user_info':{'uid':A,'sec_uid':SEC_A},
              'content':'合成问题','comment_status':1,'comment_unvisible':0,
              'schema_url':'aweme://aweme/detail/'+VIDEO+'?cid='+CID}}
    with pytest.raises(NativeEventError) as caught:project([row],channel=9)
    assert notice_identifier_diagnostic(caught.value)['field_role']=='at_outer_video_id'


def test_bad_sibling_still_rejects_entire_page_instead_of_returning_partial_candidates():
    valid=reply();bad=deepcopy(valid);bad['nid_str']='550000000000000002'
    bad['comment']['comment']['aweme_id']=None
    with pytest.raises(NativeEventError,match='invalid_notice_identifier'):project([valid,bad])
