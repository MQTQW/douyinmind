"""Owner checks use a fake page only, no account, socket, process or model."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from app.assistant.manual_verification_owner import ManualVerificationOwner


@pytest.fixture
def case(monkeypatch):
    events=[]
    class Page:
        url='https://www.douyin.com/user/self'
        context=object()
        viewport_size={'width':1100,'height':800}
        region={'known':True,'x':100,'y':100,'width':400,'height':400,
                'viewport_width':1100,'viewport_height':800,'focused':True,'active_tag':'INPUT'}
        hit=True
        def is_closed(self):return False
        def evaluate(self,script,arg=None):
            return self.hit if arg is not None else deepcopy(self.region)
        def screenshot(self,**kwargs):events.append('screenshot');return b'\x89PNG\r\n\x1a\nfixture'
        def goto(self,url,**kwargs):events.append(('goto',url));self.url=url
        def wait_for_timeout(self,ms):events.append(('wait',ms))
        def get_by_text(self,*a,**kw):return self
        def locator(self,*a,**kw):return self
        def and_(self,*a):return self
        @property
        def first(self):return self
        def wait_for(self,**kwargs):pass
    page=Page()
    page.mouse=SimpleNamespace(click=lambda x,y:events.append(('click',x,y)),
        move=lambda x,y:events.append(('move',x,y)),down=lambda:events.append('down'),up=lambda:events.append('up'))
    page.keyboard=SimpleNamespace(press=lambda key:events.append(('key',key)),
        insert_text=lambda value:events.append(('text_length',len(value))))
    class Lifecycle:
        def resume_after_explicit_start(self):events.append('explicit_refresh')
        @contextmanager
        def cycle(self,**kwargs):events.append('identity_cycle');yield
    status={'run_id':'a'*32,'state':'paused','desired_state':'paused','process_alive':True,
            'receiver_state':'verification_required','worker_state':'idle'}
    counts={}
    identity=[object()]
    uid=[True]
    adapter=SimpleNamespace(page=page,check_intervention=lambda:None,identity=lambda:identity[0])
    lane=SimpleNamespace(session_lifecycle=Lifecycle(),
        native_receiver=SimpleNamespace(reverify_identity_checkpoint=lambda:events.append('real_uid_checkpoint') or uid[0]),
        _public_native_session_status=lambda:{'state':'ready'})
    control=SimpleNamespace(snapshot=lambda:dict(status),heartbeat=lambda *a,**kw:events.append('heartbeat'))
    queue=SimpleNamespace(counts=lambda:dict(counts))
    owner=ManualVerificationOwner(adapter,lane,control,queue,'a'*32)
    monkeypatch.setattr('app.assistant.native_lifecycle.visible_draft_is_empty',lambda adapter:True)
    return SimpleNamespace(owner=owner,page=page,status=status,counts=counts,events=events,identity=identity,uid=uid)


@pytest.mark.parametrize('mutation',[{'desired_state':'running'},{'run_id':'b'*32},
    {'process_alive':False},{'worker_state':'processing'},{'receiver_state':'ready'}])
def test_scope_change_denies_before_any_page_input(case,mutation):
    case.status.update(mutation)
    with pytest.raises(ValueError):case.owner('click',{'x':120,'y':140})
    assert case.events==[]


@pytest.mark.parametrize('state',['publishing','executing'])
def test_manual_page_cannot_overlap_existing_work(case,state):
    case.counts[state]=1
    with pytest.raises(ValueError):case.owner('screenshot',{})
    assert case.events==[]


def test_already_dispatched_import_wait_does_not_deadlock_human_verification(case):
    case.counts['waiting_ingestion']=1
    assert case.owner.allowed()['queue_worker_busy'] is False
    assert case.owner('screenshot',{})['ok'] is True
    assert case.events==['screenshot']


def test_unknown_auth_surface_is_read_only_and_no_key_or_text(case):
    case.page.region={'known':False}
    image=case.owner('screenshot',{})
    assert image['input_enabled'] is False and image['auth_region_known'] is False
    for action,payload in [('click',{'x':120,'y':140}),('key',{'key':'Enter'}),('text',{'text':'synthetic'})]:
        with pytest.raises(ValueError):case.owner(action,payload)
    assert case.events==['screenshot']


@pytest.mark.parametrize('point',[{'x':50,'y':150},{'x':120,'y':800},{'x':float('nan'),'y':140},{'x':True,'y':140}])
def test_outside_or_invalid_coordinate_never_clicks(case,point):
    with pytest.raises(ValueError):case.owner('click',point)
    assert case.events==[]


def test_overlay_or_changed_auth_focus_prevents_stale_enter(case):
    case.page.hit=False
    with pytest.raises(ValueError):case.owner('click',{'x':120,'y':140})
    case.page.region['focused']=False
    with pytest.raises(ValueError):case.owner('key',{'key':'Enter'})
    assert case.events==[]


def test_known_manual_input_has_no_automatic_followup_or_echo(case):
    assert case.owner('click',{'x':120,'y':140})=={'ok':True}
    assert case.owner('text',{'text':'synthetic-value'})=={'ok':True}
    assert case.events==[('click',120,140),('text_length',15)]


def test_frame_url_context_and_owner_thread_cannot_change(case):
    case.page.url='https://evil.example/login'
    with pytest.raises(ValueError):case.owner('screenshot',{})
    case.page.url='https://www.douyin.com/user/self'
    case.page.context=object()
    with pytest.raises(ValueError):case.owner('screenshot',{})
    failures=[]
    def wrong_thread():
        try:case.owner('screenshot',{})
        except ValueError:failures.append(True)
    thread=threading.Thread(target=wrong_thread);thread.start();thread.join()
    assert failures==[True] and case.events==[]


def test_finish_checks_both_ui_and_native_identity_without_resuming_tasks(case):
    assert case.owner('finish',{})=={'ok':True,'verified':True}
    assert 'explicit_refresh' in case.events and 'real_uid_checkpoint' in case.events
    assert case.status['desired_state']=='paused'
    assert not any(event in ('model','import','publish') for event in case.events if isinstance(event,str))


def test_unconfirmed_identity_or_existing_draft_cannot_finish(case,monkeypatch):
    monkeypatch.setattr('app.assistant.native_lifecycle.visible_draft_is_empty',lambda adapter:False)
    with pytest.raises(ValueError):case.owner('finish',{})
    assert case.events==[]
    monkeypatch.setattr('app.assistant.native_lifecycle.visible_draft_is_empty',lambda adapter:True)
    case.identity[0]=None
    with pytest.raises(ValueError):case.owner('finish',{})
    assert 'real_uid_checkpoint' not in case.events


def test_native_uid_mismatch_is_not_a_successful_finish(case):
    case.uid[0]=False
    with pytest.raises(ValueError):case.owner('finish',{})
    assert 'heartbeat' not in case.events and case.status['desired_state']=='paused'


def test_drag_uses_only_recorded_points_and_always_releases_mouse(case):
    path=[{'x':120,'y':140,'t':0},{'x':130,'y':140,'t':2},{'x':140,'y':140,'t':3}]
    assert case.owner('drag',{'points':path})=={'ok':True}
    moves=[event for event in case.events if isinstance(event,tuple) and event[0]=='move']
    assert moves==[('move',120,140),('move',130,140),('move',140,140)]
    assert case.events[-1]=='up'
