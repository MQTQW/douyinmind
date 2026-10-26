"""Unknown row visibility is quarantined, never treated as visible."""
from copy import deepcopy
import json

import pytest

from app.assistant.native_events import NativeEventError
from app.assistant.native_source_store import NativeSourceStore,NativeStoreError
from tests.test_assistant_native_receiver import B,PROFILE_B,at,reply,page,project


def unknown_row(channel, field='visible', value=None):
    raw=at(created='90') if channel==9 else reply(created='90')
    if channel==9:raw['at']['comment_status' if field=='visible' else 'comment_unvisible']=value
    elif field=='visible':raw['comment']['comment']['status']=value
    else:raw['comment']['comment_unvisible']=value
    return raw


@pytest.mark.parametrize('channel',[9,7])
@pytest.mark.parametrize('field',['visible','hidden'])
@pytest.mark.parametrize('value',[None,True,'0',2,[]])
def test_unknown_visibility_keeps_only_minimal_notice_and_later_valid_candidate(channel,field,value):
    bad=unknown_row(channel,field,value)
    good=at(nid='1007',cid='2007',created='102') if channel==9 else reply(nid='1007',cid='2007',created='102')
    result=project(page([bad,good]),channel)
    assert result['notices'][0]=={'native_notice_id':bad['nid_str'],'notice_type':45 if channel==9 else 31,
        'skip_reason':'native_visibility_unknown'}
    assert len(result['candidates'])==1 and result['candidates'][0]['native_comment_id']=='2007'
    assert result['candidates'][0]['dispatchable'] is False and result['skipped']==1


def test_unknown_row_does_not_parse_or_persist_body_video_schema_or_timestamp():
    bad=unknown_row(9)
    bad['create_time']=object();bad['at']['content']=object();bad['at']['schema_url']=object();bad['aweme_id']=object()
    result=project(page([bad]),9)
    assert result['candidates']==[]
    assert json.dumps(result['notices'])=='[{"native_notice_id": "1001", "notice_type": 45, "skip_reason": "native_visibility_unknown"}]'


@pytest.mark.parametrize('problem',['duplicate','wrong_recipient'])
def test_page_identity_failures_still_reject_the_entire_mixed_page(problem):
    bad=unknown_row(9);good=at(nid='1007',cid='2007')
    if problem=='duplicate':good['nid_str']=bad['nid_str']
    else:good['user_id']='9002'
    with pytest.raises(NativeEventError,match='duplicate_native_notice_id' if problem=='duplicate' else 'notice_recipient_mismatch'):
        project(page([bad,good]),9)


def test_store_persists_skip_and_history_fence_without_unknown_source(tmp_path):
    clock=[100.0];store=NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:clock[0])
    bad=unknown_row(9)
    projection=project(page([bad,at(nid='1007',cid='2007',created='999')]),9)
    assert store.record_page(store.prepare_channel(9,'601'),projection,next_cursor=('1','2'),has_more=False)==[]
    store.record_page(store.prepare_channel(7,'2'),project(page(),7),next_cursor=('0','0'),has_more=False)
    assert store.arm_if_ready(True)
    with store._connection() as db:
        assert [tuple(r) for r in db.execute('SELECT notice_id,channel,reason FROM notice_skips')]==[('1001',9,'native_visibility_unknown')]
        assert db.execute("SELECT source_id FROM notice_refs WHERE notice_id='1001'").fetchone()[0] is None
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==1
    clock[0]=102;store.set_verified()
    corrected=at(created='90')
    store.record_page(store.prepare_channel(9,'601'),project(page([corrected]),9),next_cursor=('0','0'),has_more=False,allow_live=True)
    assert store.claim_ui_candidate('later-owner') is None
    assert store.safe_status()['counts']=={'historical':2}


@pytest.mark.parametrize('problem',['unknown_reason','skip_also_candidate'])
def test_store_rejects_unsupported_skip_or_candidate_conflict_atomically(tmp_path,problem):
    store=NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:100.0)
    projection=project(page([at()]),9);context=store.prepare_channel(9,'601')
    projection['notices'][0]['skip_reason']='synthetic_unknown_reason' if problem=='unknown_reason' else 'native_visibility_unknown'
    with pytest.raises(NativeStoreError):store.record_page(context,projection,next_cursor=('1','2'),has_more=False)
    with store._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM notice_refs').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0
        assert tuple(db.execute('SELECT min_cursor,max_cursor FROM channels WHERE channel=9').fetchone())==('0','0')
