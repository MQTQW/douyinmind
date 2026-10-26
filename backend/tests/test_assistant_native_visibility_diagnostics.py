"""Unknown visibility stays quarantined; diagnostics contain no account data."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.assistant.native_events import NativeEventError, project_page
from app.assistant.native_receiver import NativeReceiver
from tests.test_assistant_native_receiver import at, reply, page, B, SEC_B, PROFILE_B, PROFILE_A


PROBE = {'video_id': '3001', 'current_text': 'synthetic current request',
         'previous_text': 'synthetic previous request',
         'current_comment_id': None, 'previous_comment_id': None}
KEYS = {'channel', 'notice_type', 'at_sub_type', 'notice_id_sha256',
        'visible_category', 'hidden_category', 'video_id_valid', 'video_matches_probe',
        'comment_id_valid', 'current_comment_id_matches', 'previous_comment_id_matches',
        'content_is_string', 'current_text_matches', 'previous_text_matches'}


def unknown(*, nid='1001', text=PROBE['current_text']):
    raw = at(nid=nid, text=text)
    raw['at'].pop('comment_status')
    return raw


def project(raw, *, channel=9, allowed=True, probe=PROBE, observer=None):
    return project_page(page(raw), channel, receiver_uid=B, receiver_sec_uid=SEC_B,
        profile_allowed=lambda value: allowed is True and value == PROFILE_A,
        diagnostic_probe=probe, diagnostic_observer=observer)


def test_exact_safe_schema_and_nullable_id_match_do_not_make_candidate():
    diagnostics=[]; raw=unknown()
    result=project([raw],observer=diagnostics.append)
    assert result['candidates']==[]
    assert result['notices']==[{'native_notice_id':'1001','notice_type':45,'skip_reason':'native_visibility_unknown'}]
    assert len(diagnostics)==1 and set(diagnostics[0])==KEYS
    entry=diagnostics[0]
    assert entry['notice_id_sha256']==hashlib.sha256(b'1001').hexdigest()
    assert (entry['channel'],entry['notice_type'],entry['at_sub_type'])==(9,45,2)
    assert entry['visible_category']=='missing' and entry['hidden_category']=='int0'
    assert entry['current_comment_id_matches'] is entry['previous_comment_id_matches'] is None
    assert entry['video_id_valid'] is entry['video_matches_probe'] is entry['comment_id_valid'] is True
    assert entry['current_text_matches'] is True and entry['previous_text_matches'] is False
    serialized=json.dumps(diagnostics)
    for forbidden in ('3001','2001','synthetic current request','aweme://','http','fixture_A','fixture_B','sender_uid','recipient_uid'):
        assert forbidden not in serialized


@pytest.mark.parametrize(('value','category'),[
    (None,'null'),(True,'bool'),(False,'bool'),(0,'int0'),(1,'int1'),(2,'other_int'),
    ('1','string'),(1.0,'other'),({},'other'),([], 'other'),
])
def test_visibility_value_categories_never_copy_original_values(value, category):
    raw=unknown();raw['at']['comment_status']=value;raw['at']['comment_unvisible']=None
    entries=[];result=project([raw],observer=entries.append)
    assert result['candidates']==[]
    assert entries[0]['visible_category']==category and entries[0]['hidden_category']=='null'


def test_current_previous_text_and_optional_native_id_match_only_as_booleans():
    entries=[]
    raw=unknown(text=' synthetic\nprevious\trequest ')
    probe={**PROBE,'current_comment_id':'2999','previous_comment_id':'2001'}
    project([raw],probe=probe,observer=entries.append)
    entry=entries[0]
    assert entry['current_text_matches'] is False and entry['previous_text_matches'] is True
    assert entry['current_comment_id_matches'] is False and entry['previous_comment_id_matches'] is True


@pytest.mark.parametrize('change',['wrong_video','video_conflict','missing_video','bad_schema','duplicate_cid','wrong_text_type'])
def test_malformed_optional_content_only_changes_safe_diagnostic_not_quarantine(change):
    raw=unknown()
    if change=='wrong_video':
        raw['aweme_id']='3002';raw['at']['aweme']['aweme_id']='3002';raw['at']['schema_url']='aweme://aweme/detail/3002?cid=2001'
    if change=='video_conflict':raw['at']['aweme']['aweme_id']='3002'
    if change=='missing_video':raw.pop('aweme_id')
    if change=='bad_schema':raw['at']['schema_url']='secret-url-not-saved'
    if change=='duplicate_cid':raw['at']['schema_url']+='&cid=2002'
    if change=='wrong_text_type':raw['at']['content']={'secret':'not-saved'}
    entries=[];plain=project([deepcopy(raw)]);observed=project([raw],observer=entries.append)
    assert plain==observed and observed['candidates']==[] and len(entries)==1
    assert entries[0]['video_matches_probe'] is (change not in {'wrong_video','video_conflict','missing_video'})
    assert 'not-saved' not in json.dumps(entries)


def test_unregistered_author_data_is_not_observed_even_if_malformed():
    raw=unknown();raw['at']['user_info']['sec_uid']='unregistered-person'
    raw['at']['content']={'private':'must-not-be-read'};raw['at']['aweme']=None
    entries=[]
    assert project([raw],observer=entries.append)['candidates']==[]
    assert entries==[]


def test_wrong_receiver_fails_before_observation():
    raw=unknown();raw['user_id']='9002';entries=[]
    with pytest.raises(NativeEventError,match='notice_recipient_mismatch'):
        project([raw],observer=entries.append)
    assert entries==[]


@pytest.mark.parametrize('case',['reply_channel','other_at_subtype','other_notice_type','known_visible','known_unavailable'])
def test_only_exact_registered_comment_at_unknown_visibility_is_observed(case):
    raw=unknown();channel=9
    if case=='reply_channel':raw=reply();raw['comment']['comment_unvisible']=None;channel=7
    if case=='other_at_subtype':raw['at']['sub_type']=1
    if case=='other_notice_type':raw['type']=46
    if case=='known_visible':raw['at']['comment_status']=1
    if case=='known_unavailable':raw['at']['comment_status']=0
    entries=[];project([raw],channel=channel,observer=entries.append)
    assert entries==[]


def test_observer_failure_cannot_change_projection_or_valid_other_candidate():
    rows=[unknown(),at(nid='1002',cid='2002',created='102')]
    def fail(_safe):raise RuntimeError('observer-secret')
    assert project(deepcopy(rows),observer=fail)==project(deepcopy(rows))
    assert len(project(rows,observer=fail)['candidates'])==1


@pytest.mark.parametrize('probe',[None,{}, {'video_id':None,'current_text':'x','previous_text':None},
                                  {**PROBE,'secret':'must-not-be-read'}])
def test_disabled_or_invalid_probe_silently_leaves_business_projection_unchanged(probe):
    entries=[];raw=unknown()
    assert project([raw],probe=probe,observer=entries.append)==project([raw])
    assert entries==[]


class MemoryStore:
    receiver_uid=B
    receiver_profile_url=PROFILE_B
    def __init__(self):self.projections=[]
    def pending_count(self):return 0
    def prepare_channel(self,channel,group):return {'cursor':('0','0')}
    def set_verified(self):pass
    def record_page(self,context,projection,**kwargs):self.projections.append(projection);return projection['candidates']
    def arm_if_ready(self,_):pass


class Transport:
    def __init__(self):self.rows=[]
    def verify_identity(self):return B
    def get_groups(self):return {9:'601',7:'2'}
    def read_page(self,group,cursor):return page(self.rows if group=='601' else [])


def test_receiver_per_poll_memory_list_is_bounded_reset_and_never_passed_to_store():
    store=MemoryStore();transport=Transport()
    receiver=NativeReceiver(transport,store,receiver_uid=B,receiver_sec_uid=SEC_B,
        profile_allowed=lambda value:value==PROFILE_A,visibility_diagnostic_probe=PROBE)
    transport.rows=[unknown(nid=str(1000+i)) for i in range(25)]
    assert receiver.poll(2)==[] and len(receiver.last_visibility_diagnostics)==16
    assert all(set(entry)==KEYS for entry in receiver.last_visibility_diagnostics)
    assert all(set(projection)=={'notices','candidates','fingerprint','skipped'} for projection in store.projections)
    transport.rows=[]
    assert receiver.poll(2)==[] and receiver.last_visibility_diagnostics==[]
