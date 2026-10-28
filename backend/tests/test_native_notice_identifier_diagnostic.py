"""Invented notices and TEMP source ledger; diagnostics never reveal values."""
from copy import deepcopy
import json
import socket
import subprocess

import pytest

from app.assistant import native_events as events
from app.assistant import native_receiver as receiver_module
from app.assistant.native_receiver import NativeReceiver,NativeReceiverError
from app.assistant.native_source_store import NativeSourceStore,NativeStoreError

CODE='invalid_notice_identifier'
KEY='notice_identifier_diagnostic_v1'
B,A='9001','8001'
PROFILE_B='https://www.douyin.com/user/synthetic_B'
PROFILE_A='https://www.douyin.com/user/synthetic_A'


@pytest.fixture(autouse=True)
def deny_actual_io(monkeypatch):
    def deny(*args,**kwargs):raise AssertionError('real network/process forbidden')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket,'getaddrinfo',deny)
    monkeypatch.setattr(subprocess,'run',deny);monkeypatch.setattr(subprocess,'Popen',deny)


def raw_at():
    return {'nid_str':'1001','user_id':B,'type':45,'aweme_id':'3001','create_time':'102',
        'at':{'sub_type':2,'user_info':{'uid':A,'sec_uid':'synthetic_A'},'aweme':{'aweme_id':'3001'},
        'content':'synthetic text only','comment_status':1,'comment_unvisible':0,
        'schema_url':'aweme://aweme/detail/3001?cid=2001'}}


def raw_reply():
    return {'nid_str':'1002','user_id':B,'type':31,'create_time':'102',
        'comment':{'comment_type':2,'comment_unvisible':0,'parent_id':'2001','aweme':{'aweme_id':'3001'},
            'comment':{'aweme_id':'3001','cid':'2002','text':'synthetic reply',
                'status':1,'user':{'uid':A,'sec_uid':'synthetic_A'}}}}


def page(rows=()):
    return {'status_code':0,'notice_list_v2':list(rows),'has_more':False,'min_time':'0','max_time':'0'}


def project(raw,channel=9,**kwargs):
    return events.project_page(page([raw]),channel,receiver_uid=B,receiver_sec_uid='synthetic_B',
        profile_allowed=lambda profile:profile==PROFILE_A,**kwargs)


def mutate(raw,path,value):
    for key in path[:-1]:raw=raw[key]
    raw[path[-1]]=value


@pytest.mark.parametrize('channel,path,value,role,kind,problem',[
    (9,('nid_str',),None,'notice_id','null_or_missing','wrong_type'),
    (9,('user_id',),0,'recipient_uid','int','zero_not_allowed'),
    (9,('at','user_info','uid'),True,'author_uid','bool','wrong_type'),
    (9,('create_time',),1.5,'notice_create_time','float','wrong_type'),
    (9,('aweme_id',),'03001','at_outer_video_id','str','leading_zero'),
    (9,('at','aweme','aweme_id'),-1,'at_inner_video_id','int','negative'),
    (9,('at','schema_url'),'aweme://aweme/detail/3001?cid=','schema_cid','str','empty'),
    (7,('comment','comment','aweme_id'),' 3001','reply_video_id','str','padded'),
    (7,('comment','aweme','aweme_id'),3001.0,'reply_wrapper_video_id','float','wrong_type'),
    (7,('aweme_id',),'３００１','reply_outer_video_id','str','non_ascii'),
    (7,('comment','comment','cid'),10**30,'reply_comment_id','int','out_of_range'),
    (7,('comment','parent_id'),'+2001','reply_parent_hint','str','signed'),
])
def test_actual_projection_failure_has_only_static_role_type_problem(channel,path,value,role,kind,problem):
    raw=raw_at() if channel==9 else raw_reply();mutate(raw,path,value)
    with pytest.raises(events.NativeEventError,match='^'+CODE+'$')as caught:project(raw,channel)
    projector=getattr(events,'notice_identifier_diagnostic',lambda error:None)
    assert projector(caught.value)=={'field_role':role,'value_kind':kind,'problem':problem}
    assert str(caught.value)==CODE


@pytest.mark.parametrize('parent',[0,'0',None])
def test_valid_zero_parent_and_integer_ids_keep_original_meaning(parent):
    raw=raw_reply();raw['nid_str']=1002;raw['user_id']=9001
    raw['comment']['comment']['user']['uid']=8001
    raw['comment']['parent_id']=parent
    result=project(raw,7)['candidates'][0]
    assert result['parent_id_hint']==(None if parent is None else '0')
    assert result['root_comment_id'] is None and result['parent_comment_id'] is None and result['dispatchable'] is False


def source(tmp_path):
    return NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:102.)


def meta(store):
    with store._connection()as db:return dict(db.execute('SELECT key,value FROM meta'))


def other_tables(store):
    with store._connection()as db:
        return {table:[tuple(row)for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
            for table in ('channels','scan_pages','identities','sources','notice_refs','notice_skips','observations')}


def receiver(store,raw,**options):
    class Transport:
        def verify_identity(self):return B
        def get_groups(self):return {9:'601',7:'2'}
        def read_page(self,*args):return page([raw])
    return NativeReceiver(Transport(),store,receiver_uid=B,receiver_sec_uid='synthetic_B',
        profile_allowed=lambda profile:profile==PROFILE_A,**options)


def test_actual_receiver_persists_only_three_enums_after_original_pause(tmp_path,caplog):
    store=source(tmp_path);raw=raw_at();raw['nid_str']='PRIVATE_RAW_ID_DO_NOT_KEEP'
    raw['at']['content']='PRIVATE_BODY_DO_NOT_KEEP'
    raw['at']['schema_url']='aweme://aweme/detail/3001?cid=PRIVATE_URL_DO_NOT_KEEP'
    store.prepare_channel(9,'601');before=other_tables(store)
    with pytest.raises(NativeReceiverError,match='^'+CODE+'$'):receiver(store,raw).poll(max_pages=1)
    saved=meta(store)
    assert KEY in saved
    assert json.loads(saved[KEY])=={'field_role':'notice_id','value_kind':'str','problem':'non_decimal'}
    assert saved['pause_code']==CODE and saved['verified_at']=='0'
    assert other_tables(store)==before
    assert set(json.loads(saved[KEY]))=={'field_role','value_kind','problem'}
    for secret in ('PRIVATE_RAW_ID_DO_NOT_KEEP','PRIVATE_BODY_DO_NOT_KEEP','PRIVATE_URL_DO_NOT_KEEP'):
        assert secret not in saved[KEY] and secret not in caplog.text


def test_store_method_changes_only_fixed_meta_key_and_not_safe_status(tmp_path):
    store=source(tmp_path);store.pause(CODE)
    before=meta(store);status=store.safe_status();tables=other_tables(store)
    value={'field_role':'reply_parent_hint','value_kind':'float','problem':'wrong_type'}
    assert store.record_notice_identifier_diagnostic(value) is True
    after=meta(store)
    assert {k:v for k,v in after.items()if k!=KEY}==before
    assert json.loads(after[KEY])==value and store.safe_status()==status and other_tables(store)==tables


@pytest.mark.parametrize('pause',['session_invalid','invalid_notice_schema',''])
def test_store_requires_current_exact_identifier_pause(tmp_path,pause):
    store=source(tmp_path)
    store.pause(pause) if pause else store.set_verified()
    before=meta(store)
    with pytest.raises(NativeStoreError,match='notice_identifier_diagnostic_not_current'):
        store.record_notice_identifier_diagnostic({'field_role':'notice_id','value_kind':'int','problem':'negative'})
    assert meta(store)==before


class Opaque:
    def __str__(self):raise AssertionError('must not inspect unknown value')
    def __repr__(self):raise AssertionError('must not inspect unknown value')


@pytest.mark.parametrize('value',[None,{}, {'field_role':'notice_id','value_kind':'int','problem':'wrong_type','raw':Opaque()},
    {'field_role':Opaque(),'value_kind':'int','problem':'wrong_type'},
    {'field_role':'not_whitelisted','value_kind':'int','problem':'wrong_type'},
    {'field_role':'notice_id','value_kind':'int','problem':'RAW_SECRET'}])
def test_invalid_or_extra_diagnostic_fields_reject_before_opening_write_connection(tmp_path,monkeypatch,value):
    store=source(tmp_path)
    monkeypatch.setattr(store,'_connection',lambda **kw:pytest.fail('invalid projection cannot enter a transaction'))
    with pytest.raises(NativeStoreError,match='invalid_notice_identifier_diagnostic'):
        store.record_notice_identifier_diagnostic(value)


def test_ordinary_forged_or_subclassed_error_has_no_diagnostic_and_no_attribute_probe():
    class Forged(Exception):
        def __getattribute__(self,key):raise AssertionError('do not probe ordinary exception')
    class Subclass(events.NativeEventError):
        def __getattribute__(self,key):raise AssertionError('do not probe subclass exception')
    plain=events.NativeEventError(CODE);plain._identifier_fact=(object(),Opaque(),Opaque(),Opaque())
    plain.raw=Opaque()
    for error in (Forged(),Subclass(CODE),plain):assert events.notice_identifier_diagnostic(error) is None


def test_logging_or_diagnostic_persistence_failure_never_masks_original_rejection(tmp_path,monkeypatch):
    store=source(tmp_path);raw=raw_at();raw['nid_str']=None
    monkeypatch.setattr(receiver_module._LOG,'warning',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('PRIVATE_LOG_DETAIL')))
    monkeypatch.setattr(store,'record_notice_identifier_diagnostic',lambda value:(_ for _ in ()).throw(RuntimeError('PRIVATE_WRITE_DETAIL')))
    with pytest.raises(NativeReceiverError,match='^'+CODE+'$'):receiver(store,raw).poll(max_pages=1)
    assert meta(store)['pause_code']==CODE and meta(store)['verified_at']=='0'


def test_swallowed_auxiliary_visibility_probe_failure_is_not_persisted_as_current_error(tmp_path):
    store=source(tmp_path);raw=raw_at();raw['at']['comment_status']=2;raw['aweme_id']=None
    probe={'video_id':'3001','current_text':'synthetic text only','previous_text':None}
    result=receiver(store,raw,visibility_diagnostic_probe=probe).poll(max_pages=1)
    assert result==[] and KEY not in meta(store)
    assert meta(store)['pause_code']==''
