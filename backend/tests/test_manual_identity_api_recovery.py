"""Explicit human recovery retains strict fresh native identity checks.

All account identifiers, browser data and responses are invented fixtures. No
vault, network, process, database or running product state is used by tests.
"""
import json
import socket
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from app.assistant import native_lifecycle as lifecycle_module
from app.assistant.native_lifecycle import NativeSessionLifecycle, SameContextCapture, SessionLifecycleError
from app.assistant.native_transport import NativeTransport
from app.assistant.native_receiver import NativeReceiver
from app.assistant.manual_verification_owner import verify_registered_receiver as finish
from app.mention_receiver.browser import SELF_URL

UID='123456789987654321';SEC='synthetic_owned_B';PROFILE='https://www.douyin.com/user/'+SEC

@pytest.fixture(autouse=True)
def deny_actual_io(monkeypatch):
    def deny(*a,**kw):raise AssertionError('no actual network/process/database')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket,'getaddrinfo',deny)
    monkeypatch.setattr(sqlite3,'connect',deny);monkeypatch.setattr(subprocess,'run',deny)
    monkeypatch.setattr(subprocess,'Popen',deny)
    monkeypatch.setattr(lifecycle_module,'visible_draft_is_empty',lambda adapter:True)


def case():
    now=[1000.];events=[];network_mode={'code':0,'uid':UID,'status':200,'captcha':False}
    class Metadata:
        def send(self,method,args):
            assert method=='Browser.getVersion' and args=={}
            return {'userAgent':'synthetic-UA'}
        def detach(self):events.append('metadata_detach')
    class Context:
        browser=SimpleNamespace(new_browser_cdp_session=lambda:Metadata())
        def cookies(self,urls):
            assert tuple(urls)==lifecycle_module._URLS
            events.append('fresh_capture')
            return [{'domain':'.douyin.com','path':'/','name':'synthetic_session',
                'value':'invented_only','expires':-1}]
    class Locator:
        first=property(lambda self:self)
        def and_(self,other):return self
        def wait_for(self,**kwargs):events.append('visible_self_identifier')
    class Page:
        context=Context()
        def goto(self,url,**kwargs):
            assert url==SELF_URL;events.append('normal_self_navigation')
        def get_by_text(self,*args,**kwargs):return Locator()
        def locator(self,*args,**kwargs):return Locator()
    adapter=SimpleNamespace(page=Page(),expected_profile_url=PROFILE,
        check_intervention=lambda:None,identity=lambda:events.append('ui_identity_passed') or object())
    class Vault:
        expected_uid=UID
        def __init__(self):self.saved=[]
        def load(self):
            error=RuntimeError('fixed synthetic missing');error.code='authorization_not_provisioned';raise error
        def save(self,value):self.saved.append(value);events.append('seal_memory_only')
    vault=Vault()
    capture=SameContextCapture(adapter,PROFILE,UID,draft_is_empty=lambda:True,clock=lambda:now[0])
    def network(url,headers,timeout,max_bytes):
        assert '/aweme/v1/web/query/user/' in url;events.append('fixed_uid_get')
        return network_mode['status'],json.dumps({'status_code':network_mode['code'],
            'user_uid':network_mode['uid'],'captcha_required':network_mode['captcha']}).encode()
    life=NativeSessionLifecycle(expected_uid=UID,owned_vault=vault,context_capture=capture,
        network=network,clock=lambda:now[0],require_seal=True)
    class Store:
        receiver_uid=UID;receiver_profile_url=PROFILE
        def __init__(self):self.verified=0;self.pauses=[]
        def set_verified(self):self.verified+=1;events.append('source_set_verified')
        def pause(self,code):self.pauses.append(code);events.append('source_paused')
    store=Store()
    receiver=NativeReceiver(NativeTransport(life.provider,UID,network,clock=lambda:now[0]),store,
        receiver_uid=UID,receiver_sec_uid=SEC,profile_allowed=lambda profile:True)
    lane=SimpleNamespace(session_lifecycle=life,native_receiver=receiver,
        _public_native_session_status=lambda:lifecycle_module.project_native_session_status(life.safe_status()))
    return SimpleNamespace(life=life,adapter=adapter,receiver=receiver,store=store,
        lane=lane,network_mode=network_mode,events=events,vault=vault)


def establish_identity_api_error(c):
    with c.life.cycle():pass
    assert c.life.safe_status()['generation']==1
    c.network_mode['code']=18
    with pytest.raises(Exception,match='api_error'):
        with c.life.cycle(reset_failures_on_success=False):c.receiver.reverify_identity_checkpoint()
    status=c.life.safe_status()
    assert status['state']=='paused' and status['reason_code']=='api_error'
    assert status['transport_facts']=={'endpoint_role':'identity','http_status':200,'api_status_code':18}


def test_explicit_finish_should_freshly_revalidate_fixed_uid_after_ui_identity_passes():
    c=case();establish_identity_api_error(c);c.network_mode['code']=0
    status=finish(c.adapter,c.lane)
    assert status['state']=='ready' and c.store.verified==1
    assert c.events.count('fresh_capture')==2 and c.events.count('fixed_uid_get')==4


def test_allowed_existing_pause_still_requires_real_fresh_uid_before_source_verified():
    c=case();c.life.pause('verification_required')
    status=finish(c.adapter,c.lane)
    assert status['state']=='ready' and c.store.verified==1
    assert c.events.count('fresh_capture')==1 and c.events.count('fixed_uid_get')==2
    assert c.events.index('source_set_verified')>c.events.index('fixed_uid_get')


def test_dom_alone_cannot_pass_existing_allowed_recovery_when_api_still_fails():
    c=case();c.life.pause('verification_required');c.network_mode['code']=18
    with pytest.raises(Exception,match='api_error'):finish(c.adapter,c.lane)
    assert c.store.verified==0 and c.vault.saved==[]
    assert c.life.safe_status()['reason_code']=='api_error'


def test_persistent_api18_remains_sticky_without_seal_or_timer_retry():
    c=case();establish_identity_api_error(c)
    before_capture=c.events.count('fresh_capture');before_uid=c.events.count('fixed_uid_get')
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='^api_error$'):finish(c.adapter,c.lane)
    assert c.life.safe_status()['reason_code']=='api_error' and c.store.verified==0
    assert c.events.count('fresh_capture')==before_capture+1
    assert c.events.count('fixed_uid_get')==before_uid+1 and len(c.vault.saved)==1
    for _ in range(2):
        with pytest.raises(lifecycle_module.SessionLifecycleError,match='^api_error$'):
            with c.life.cycle():pytest.fail('sticky failure cannot poll automatically')
    assert c.events.count('fresh_capture')==before_capture+1


def test_new_uid_mismatch_rejects_and_does_not_update_source_or_seal():
    c=case();establish_identity_api_error(c)
    c.network_mode.update(code=0,uid='998877665544332211')
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='session_identity_mismatch'):
        finish(c.adapter,c.lane)
    assert c.store.verified==0 and len(c.vault.saved)==1
    assert c.life.safe_status()['reason_code']=='session_identity_mismatch'


@pytest.mark.parametrize('facts',[
    {},{'endpoint_role':'notices','http_status':200,'api_status_code':18},
    {'endpoint_role':'identity','http_status':403,'api_status_code':18},
    {'endpoint_role':'identity','http_status':200,'api_status_code':0},
    {'endpoint_role':'identity','http_status':'200','api_status_code':18},
    {'endpoint_role':'identity','http_status':200,'api_status_code':True},
    {'endpoint_role':'identity','http_status':200,'api_status_code':18,'untrusted':object()},
])
def test_missing_wrong_role_or_bad_type_facts_cannot_unlock_api_error(facts):
    c=case();establish_identity_api_error(c);c.network_mode['code']=0
    c.life._last_transport_facts=facts
    before=(c.events.count('fresh_capture'),c.events.count('fixed_uid_get'))
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='^api_error$'):finish(c.adapter,c.lane)
    assert before==(c.events.count('fresh_capture'),c.events.count('fixed_uid_get'))
    assert c.store.verified==0


@pytest.mark.parametrize('failure',['session_invalid','verification_required'])
def test_new_http_auth_or_real_challenge_is_not_treated_as_success(failure):
    c=case();establish_identity_api_error(c)
    c.network_mode.update(code=0,status=401 if failure=='session_invalid' else 200,
        captcha=failure=='verification_required')
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='^'+failure+'$'):finish(c.adapter,c.lane)
    assert c.store.verified==0 and len(c.vault.saved)==1
    assert c.life.safe_status()['reason_code']==failure


def test_existing_draft_and_unconfirmed_ui_do_not_clear_old_block(monkeypatch):
    c=case();establish_identity_api_error(c);c.network_mode['code']=0
    original=len(c.events)
    monkeypatch.setattr(lifecycle_module,'visible_draft_is_empty',lambda adapter:False)
    with pytest.raises(ValueError,match='verification_existing_draft'):finish(c.adapter,c.lane)
    assert len(c.events)==original and c.life.safe_status()['reason_code']=='api_error'
    monkeypatch.setattr(lifecycle_module,'visible_draft_is_empty',lambda adapter:True)
    c.adapter.identity=lambda:None
    before=c.events.count('fresh_capture')
    with pytest.raises(ValueError,match='verification_identity_unconfirmed'):finish(c.adapter,c.lane)
    assert c.events.count('fresh_capture')==before and c.store.verified==0


def test_broken_vault_is_not_added_to_recovery_allowlist():
    c=case()
    def broken_load():
        error=RuntimeError('synthetic unreadable capsule');error.code='capsule_invalid';raise error
    c.vault.load=broken_load
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='capsule_invalid'):
        with c.life.cycle():pass
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='capsule_invalid'):finish(c.adapter,c.lane)
    assert c.events.count('fresh_capture')==0 and c.store.verified==0 and c.vault.saved==[]


def test_failed_required_seal_keeps_source_unverified():
    c=case();establish_identity_api_error(c);c.network_mode['code']=0
    c.vault.save=lambda value:(_ for _ in ()).throw(ValueError('synthetic unsafe path'))
    with pytest.raises(lifecycle_module.SessionLifecycleError,match='session_refresh_unavailable'):finish(c.adapter,c.lane)
    assert c.store.verified==0 and c.life.safe_status()['state']=='paused'


def test_second_uid_checkpoint_api18_remains_a_fresh_sticky_failure():
    c=case();establish_identity_api_error(c);c.network_mode['code']=0
    first_uid_network=c.life._network
    def capture_identity_succeeds_but_next_checkpoint_fails(*args):
        response=first_uid_network(*args)
        c.network_mode['code']=18
        return response
    c.life._network=capture_identity_succeeds_but_next_checkpoint_fails
    with pytest.raises(Exception,match='^api_error$'):finish(c.adapter,c.lane)
    assert c.store.verified==0 and c.store.pauses[-1]=='api_error'
    assert c.life.safe_status()['reason_code']=='api_error'
    assert c.life.safe_status()['transport_facts']=={
        'endpoint_role':'identity','http_status':200,'api_status_code':18}
    # The first fresh strict identity may seal; its later failure is still not
    # a completed verification and never sets Source.verified or returns True.
    assert len(c.vault.saved)==2
