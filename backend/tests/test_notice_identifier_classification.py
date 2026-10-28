"""Malformed native IDs stay rejected; classification must not invent login."""
from contextlib import contextmanager
from copy import deepcopy
import json
import socket
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs,urlsplit

import pytest

from app.assistant import __main__ as owner
from app.assistant.native_events import project_page
from app.assistant.native_lane import NativeLane,NativeSessionPaused
from app.assistant.native_lifecycle import NativeSessionLifecycle,SessionLifecycleError
from app.assistant.native_receiver import NativeReceiver
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_transport import NativeTransport,SecretSession
from app.mention_receiver.browser import ReceiverInterventionRequired

CODE='invalid_notice_identifier'
B,A='9001','8001'
PROFILE_B='https://www.douyin.com/user/synthetic_B'
PROFILE_A='https://www.douyin.com/user/synthetic_A'


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def deny(*a,**kw):raise AssertionError('no real network or process allowed')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket,'getaddrinfo',deny)
    monkeypatch.setattr(subprocess,'run',deny);monkeypatch.setattr(subprocess,'Popen',deny)


def empty_page(rows=()):
    return {'status_code':0,'notice_list_v2':list(rows),'has_more':False,'min_time':'0','max_time':'0'}


def poll_failure(tmp_path):
    now=[100.];calls=[]
    source=NativeSourceStore(tmp_path/'sources.sqlite3',B,PROFILE_B,create=True,clock=lambda:now[0])
    for channel,group in ((9,'601'),(7,'2')):
        context=source.prepare_channel(channel,group)
        projection=project_page(empty_page(),channel,receiver_uid=B,receiver_sec_uid='synthetic_B',profile_allowed=lambda p:p==PROFILE_A)
        source.record_page(context,projection,next_cursor=('0','0'),has_more=False)
    source.arm_if_ready(True);source.set_verified();now[0]=102.
    source.prepare_channel(9,'601')  # Snapshot after normal scan initialization.
    def persisted():
        with source._connection() as db:
            return {name:[tuple(r) for r in db.execute('SELECT * FROM '+name+' ORDER BY rowid')]
                for name in ('channels','scan_pages','sources','notice_refs','notice_skips')}
    before=persisted()
    with source._connection() as db:
        arm_before=db.execute("SELECT value FROM meta WHERE key='armed_at'").fetchone()[0]
    raw={'nid_str':'invalid-native-id','type':45,'user_id':B,'aweme_id':'3001','create_time':'102',
        'at':{'sub_type':2,'aweme':{'aweme_id':'3001'},'user_info':{'uid':A,'sec_uid':'synthetic_A'},
          'content':'synthetic question','comment_status':1,'comment_unvisible':0,
          'schema_url':'aweme://aweme/detail/3001?cid=2001'}}
    def network(url,headers,timeout,max_bytes):
        path=urlsplit(url).path;calls.append(path)
        if path.endswith('query/user/'):value={'status_code':0,'user_uid':B}
        elif path.endswith('notice/count/'):value={'status_code':0,'interactive_group':[{'type':9,'group':'601'},{'type':7,'group':'2'}]}
        else:
            assert parse_qs(urlsplit(url).query)['notice_group']==['601']
            value=empty_page([raw])
        return 200,json.dumps(value).encode()
    secret=SecretSession('synthetic_session=unused','synthetic-agent',B,1000.)
    class Capture:
        expected_uid=B
        calls=0
        def guard(self):pass
        def capture(self):self.calls+=1;return secret
    capture=Capture()
    vault=SimpleNamespace(expected_uid=B,load=lambda:secret,save=lambda _:pytest.fail('no fresh capture expected'))
    life=NativeSessionLifecycle(expected_uid=B,owned_vault=vault,context_capture=capture,network=network,clock=lambda:now[0])
    receiver=NativeReceiver(NativeTransport(life.provider,B,network,clock=lambda:now[0]),source,
        receiver_uid=B,receiver_sec_uid='synthetic_B',profile_allowed=lambda p:p==PROFILE_A)
    lane=SimpleNamespace(_owner=lambda:None,should_stop=lambda:False,source_store=source,
        native_receiver=receiver,session_lifecycle=life,
        _with_native_session_status=lambda e:e)
    with pytest.raises(ReceiverInterventionRequired) as caught:NativeLane.poll_native(lane)
    return SimpleNamespace(error=caught.value,source=source,life=life,calls=calls,capture=capture,
        now=now,before=before,persisted=persisted,arm_before=arm_before)


def test_lifecycle_preserves_fixed_notice_identifier_reason():
    assert SessionLifecycleError(CODE).code==CODE


def test_rejected_id_page_never_commits_source_or_cursor_and_keeps_current_marker(tmp_path):
    c=poll_failure(tmp_path)
    assert c.persisted()==c.before
    assert c.source.safe_status()['pause_code']==CODE
    with c.source._connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='verified_at'").fetchone()[0]=='0'
        assert db.execute("SELECT value FROM meta WHERE key='armed_at'").fetchone()[0]==c.arm_before
    assert isinstance(c.error,NativeSessionPaused)
    assert c.error.native_failure_code==CODE
    assert c.error.native_intake_data_code==CODE


def test_identifier_failure_is_sticky_not_transient_or_manual_recoverable(tmp_path):
    c=poll_failure(tmp_path)
    status=c.life.safe_status()
    assert status['reason_code']==CODE and status['state']=='paused'
    assert status['transient_failures']==0 and status['retry_after_seconds']==0
    before=len(c.calls),c.capture.calls
    c.now[0]+=10000
    with pytest.raises(SessionLifecycleError,match='^'+CODE+'$'):
        with c.life.cycle():pytest.fail('format error may not poll automatically')
    with pytest.raises(SessionLifecycleError,match='^'+CODE+'$'):c.life.resume_after_explicit_start()
    assert before==(len(c.calls),c.capture.calls)


def test_browser_challenge_cannot_be_reclassified_by_old_identifier_marker():
    error=ReceiverInterventionRequired('网页要求本人核验')
    error.native_intake_data_code=CODE
    error.native_session_status={'state':'paused','reason_code':CODE,'generation':1,'refresh_count':1,
        'source':'same_context','seal_state':'sealed','transient_failures':0,
        'last_transient_code':None,'retry_after_seconds':0}
    assert owner.intervention_health(error)==('verification_required','human_intervention_required')


def test_current_typed_identifier_failure_is_data_review_not_human_identity(tmp_path):
    c=poll_failure(tmp_path)
    assert owner.intervention_health(c.error)==('page_unavailable','native_notice_format_unavailable')
