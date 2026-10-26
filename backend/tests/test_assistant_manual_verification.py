"""Pure request/owner-queue fixtures; no server sockets or browser/accounts."""
import json
import re
import threading

import pytest

from app.assistant.manual_verification import ManualVerificationError, ManualVerificationServer

RUN = 'a' * 32
PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic-frame'


def make(**changes):
    now = [1000.0]
    trusted = {'run_id': RUN, 'process_alive': True, 'state': 'paused',
               'receiver_state': 'verification_required', 'worker_state': 'idle',
               'publication_busy': False, 'queue_worker_busy': False}
    trusted.update(changes)
    calls = []
    def allowed():
        calls.append(threading.get_ident())
        return dict(trusted)
    server = ManualVerificationServer(RUN, allowed=allowed, clock=lambda: now[0])
    headers = {'host': '127.0.0.1:8767', 'origin': server.origin, 'content-type': 'application/json'}
    code, _, html = server.request('GET', '/', headers)
    assert code == 200
    headers['x-douyinmind-manual'] = re.search(rb'name="manual-nonce" content="([a-f0-9]{64})"', html)[1].decode()
    return server, headers, trusted, calls, now


def request(server, headers, path, data):
    return server.request('POST', path, headers, json.dumps(data).encode())


def begin(server, headers):
    code, _, body = request(server, headers, '/api/begin', {})
    assert code == 202
    session = json.loads(body)['session_id']
    result = server.process_one(lambda action, payload: {'ok': True})
    assert result['state'] == 'active'
    return session


def action(server, headers, session, name, payload=None):
    command = {'session_id': session, 'action': name, 'payload': payload or {}}
    if name in {'click','drag','key','text'}:
        command['frame_generation'] = server.safe_status()['frame_generation']
    return request(server, headers, '/api/action', command)


def screenshot(server, headers, session):
    assert action(server, headers, session, 'screenshot')[0] == 202
    server.process_one(lambda name, args: {'ok': True, 'frame': PNG, 'width': 1100, 'height': 800,
                                         'input_enabled': True, 'auth_region_known': True})


def test_passive_root_no_owner_or_browser_work_and_safe_csp():
    server, headers, _, calls, _ = make()
    code, response_headers, body = server.request('GET', '/', headers)
    assert code == 200 and calls == [] and not server.session_active
    assert "frame-ancestors 'none'" in response_headers['Content-Security-Policy']
    assert response_headers['Cache-Control'] == 'no-store'
    assert b'__MANUAL_NONCE__' not in body and b'http://' not in body


@pytest.mark.parametrize('field,value', [('host','localhost:8767'), ('host','evil.example:8767'),
    ('origin','https://evil.example'), ('origin',None), ('x-douyinmind-manual',None)])
def test_post_requires_exact_host_origin_and_nonce(field, value):
    server, headers, _, calls, _ = make()
    if value is None:
        headers.pop(field, None)
    else:
        headers[field] = value
    assert request(server, headers, '/api/begin', {})[0] == 403
    assert calls == [] and not server.session_active


def test_nonloopback_and_iframe_and_unprotected_frame_rejected():
    server, headers, _, _, _ = make()
    assert server.request('GET','/',headers,client_host='192.0.2.1')[0] == 403
    assert server.request('GET','/',{**headers,'sec-fetch-dest':'iframe'})[0] == 403
    assert server.request('GET','/api/frame',{'host':headers['host']})[0] == 403


def test_begin_is_queued_and_browser_handler_only_on_owner_thread():
    server, headers, _, calls, _ = make()
    seen = []
    assert request(server, headers, '/api/begin', {})[0] == 202
    assert calls == [] and server.session_active
    server.process_one(lambda action, args: seen.append((action, threading.get_ident())) or {'ok': True})
    assert seen == [('begin', threading.get_ident())]
    assert calls == [threading.get_ident(), threading.get_ident()]


@pytest.mark.parametrize('changes', [{'run_id':'b'*32}, {'process_alive':False}, {'state':'running'},
    {'receiver_state':'ready'}, {'worker_state':'processing'}, {'publication_busy':True}, {'queue_worker_busy':True}])
def test_trusted_owner_scope_must_be_paused_idle_verification(changes):
    server, headers, _, _, _ = make(**changes)
    request(server, headers, '/api/begin', {})
    touched = []
    status = server.process_one(lambda *args: touched.append(True))
    assert status['state'] == 'verification_not_ready' and touched == []
    assert not server.session_active


def test_scope_rechecked_after_browser_work():
    server, headers, trusted, _, _ = make()
    request(server, headers, '/api/begin', {})
    def handler(*args):
        trusted['publication_busy'] = True
        return {'ok': True}
    assert server.process_one(handler)['state'] == 'verification_not_ready'


def test_worker_thread_cannot_drain_browser_queue():
    server, headers, _, _, _ = make()
    request(server, headers, '/api/begin', {})
    errors = []
    def other_thread():
        try:
            server.process_one(lambda *_: pytest.fail('not owner'))
        except ManualVerificationError as error:
            errors.append(error.code)
    thread = threading.Thread(target=other_thread); thread.start(); thread.join()
    assert errors == ['manual_owner_thread_required']


def test_frame_memory_nonce_guard_and_expiry():
    server, headers, _, _, now = make()
    session = begin(server, headers); screenshot(server, headers, session)
    code, response_headers, body = server.request('GET','/api/frame',headers)
    assert code == 200 and body == PNG and response_headers['Cache-Control'] == 'no-store'
    now[0] += 21
    assert server.request('GET','/api/frame',headers)[0] == 404
    assert not server.safe_status()['frame_available']


@pytest.mark.parametrize('payload', [{'x':True,'y':1}, {'x':1100,'y':1}, {'x':-1,'y':0},
    {'x':1.5,'y':1}, {'x':1,'y':800}, {'x':1,'y':1,'url':'https://evil.example'}])
def test_click_uses_bounded_current_frame_coordinates(payload):
    server, headers, _, _, _ = make()
    session = begin(server, headers); screenshot(server, headers, session)
    assert action(server, headers, session, 'click', payload)[0] == 400


def test_click_without_frame_rejected_and_valid_click_delivered_once():
    server, headers, _, _, _ = make(); session = begin(server, headers)
    assert action(server, headers, session, 'click', {'x':1,'y':1})[0] == 400
    screenshot(server, headers, session)
    assert action(server, headers, session, 'click', {'x':10,'y':20})[0] == 202
    seen = []
    server.process_one(lambda name, payload: seen.append((name, dict(payload))) or {'ok': True})
    assert seen == [('click', {'x':10,'y':20})]
    assert server.process_one(lambda *_: pytest.fail('repeat')) is None


@pytest.mark.parametrize('name,payload', [('goto',{'url':'https://www.douyin.com'}),
    ('evaluate',{'js':'alert(1)'}),('cookie',{}),('key',{'key':'Control+L'}),
    ('key',{'key':'F12'}),('text',{'text':'x'*257}),('text',{'text':'abc\nxyz'}),
    ('text',{'text':''}),('text',{'text':'x','extra':True})])
def test_no_navigation_js_cdp_cookie_or_unbounded_input(name,payload):
    server, headers, _, _, _ = make(); session = begin(server, headers)
    screenshot(server, headers, session)
    assert action(server, headers, session, name, payload)[0] == 400


def test_text_never_echoed_or_in_status_repr_and_payload_reference_cleared():
    server, headers, _, _, _ = make(); session = begin(server, headers)
    screenshot(server, headers, session)
    secret = '仅合成的验证码123456'
    response = action(server, headers, session, 'text', {'text': secret})
    assert response[0] == 202 and secret.encode() not in response[2]
    references, values = [], []
    def handler(name, payload):
        references.append(payload); values.append(payload['text'])
        raise RuntimeError(secret)
    status = server.process_one(handler)
    assert values == [secret] and references == [{}]
    assert secret not in repr(server) + json.dumps(status,ensure_ascii=False)
    assert not server.session_active


def test_cancel_discards_pending_text_without_browser_work():
    server, headers, _, _, _ = make(); session = begin(server, headers)
    screenshot(server, headers, session)
    action(server, headers, session, 'text', {'text':'synthetic private input'})
    assert action(server, headers, session, 'cancel')[0] == 200
    assert not server.session_active and server.process_one(lambda *_: pytest.fail('cancelled')) is None


def test_session_absolute_ttl_not_extended_by_status_or_input():
    server, headers, _, _, now = make(); session = begin(server, headers)
    now[0] += 299
    screenshot(server, headers, session)
    assert server.request('GET','/api/status',headers)[0] == 200
    action(server, headers, session, 'text', {'text':'synthetic'})
    now[0] += 2
    assert not server.session_active
    assert server.process_one(lambda *_: pytest.fail('expired text')) is None
    assert server.safe_status()['state'] == 'expired'


def test_finish_requires_explicit_verified_and_does_not_resume_anything():
    server, headers, trusted, _, _ = make(); session = begin(server, headers)
    action(server, headers, session, 'finish')
    assert server.process_one(lambda *_: {'ok':True, 'verified':False})['state'] == 'active'
    action(server, headers, session, 'finish')
    assert server.process_one(lambda *_: {'ok':True, 'verified':True})['state'] == 'completed'
    assert trusted['state'] == 'paused' and not server.session_active


def test_other_viewer_cannot_read_active_frame_or_command_session():
    server, headers, _, _, _ = make(); session = begin(server, headers); screenshot(server, headers, session)
    _, _, page = server.request('GET','/',headers)
    other = {**headers,'x-douyinmind-manual':re.search(rb'name="manual-nonce" content="([a-f0-9]{64})"',page)[1].decode()}
    assert server.request('GET','/api/frame',other)[0] == 409
    assert action(server,other,session,'finish')[0] == 409


def test_json_duplicate_keys_large_body_nonjson_and_extra_begin_rejected():
    server, headers, _, _, _ = make()
    for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b'[]',b'x'*4097):
        assert server.request('POST','/api/begin',headers,raw)[0] in (400,409)
    assert request(server,headers,'/api/begin',{'extra':1})[0] == 409
    assert server.request('POST','/api/begin',{**headers,'content-type':'text/plain'},b'{}')[0] == 400


def test_no_other_method_or_endpoint_and_close_clears_all_memory():
    server, headers, _, _, _ = make(); session = begin(server, headers); screenshot(server, headers, session)
    assert server.request('GET','/api/action',headers)[0] == 405
    assert server.request('POST','/api/goto',headers,b'{}')[0] == 405
    server.close()
    assert server.request('GET','/',headers)[0] == 503
    assert not server.session_active and server._frame is None and not server._pending


def test_unknown_auth_region_is_view_only_not_just_a_disabled_button():
    server, headers, _, _, _ = make(); session = begin(server, headers)
    action(server, headers, session, 'screenshot')
    server.process_one(lambda *_: {'ok':True,'frame':PNG,'width':1100,'height':800})
    assert server.safe_status()['frame_available'] and not server.safe_status()['input_enabled']
    for name,payload in [('text',{'text':'synthetic'}),('key',{'key':'Enter'}),
                         ('click',{'x':1,'y':1}),('drag',{'points':[{'x':1,'y':1,'t':0},{'x':2,'y':2,'t':20}]})]:
        assert action(server, headers, session, name, payload)[0] == 400
    assert action(server, headers, session, 'finish')[0] == 202


def test_real_pointer_samples_are_passed_unchanged_once_and_not_in_status():
    server, headers, _, _, _ = make(); session = begin(server, headers); screenshot(server, headers, session)
    points=[{'x':10,'y':20,'t':0},{'x':30,'y':22,'t':150},{'x':40,'y':25,'t':180}]
    assert action(server,headers,session,'drag',{'points':points})[0] == 202
    values=[]
    server.process_one(lambda name,payload: values.append((name,[dict(p) for p in payload['points']])) or {'ok':True})
    assert values == [('drag',points)]
    assert 'points' not in json.dumps(server.safe_status())


@pytest.mark.parametrize('points', [[],[{'x':1,'y':1,'t':0}],
    [{'x':1,'y':1,'t':1},{'x':2,'y':2,'t':20}],
    [{'x':1,'y':1,'t':0},{'x':2,'y':2,'t':10001}],
    [{'x':1,'y':1,'t':0},{'x':2,'y':2,'t':20},{'x':3,'y':3,'t':19}],
    [{'x':1,'y':1,'t':0},{'x':1100,'y':2,'t':20}],
    [{'x':1,'y':1,'t':0},{'x':2,'y':2,'t':True}],
    [{'x':1,'y':1,'t':0},{'x':2,'y':2,'t':20,'extra':1}],
    [{'x':i,'y':2,'t':i} for i in range(81)]])
def test_drag_rejects_out_of_frame_time_shape_and_point_bounds(points):
    server, headers, _, _, _ = make(); session = begin(server, headers); screenshot(server, headers, session)
    assert action(server,headers,session,'drag',{'points':points})[0] == 400


def test_explicit_process_guard_false_rejects_before_owner_handler():
    server, headers, _, calls, _ = make()
    server._guard = lambda: False
    request(server,headers,'/api/begin',{})
    assert server.process_one(lambda *_: pytest.fail('unregistered owner'))['state'] == 'verification_not_ready'
    assert calls == []


def test_owner_control_read_cannot_extend_expired_input_lease():
    server, headers, trusted, _, now = make(); session = begin(server, headers); screenshot(server, headers, session)
    action(server,headers,session,'click',{'x':10,'y':10})
    def slow_guard():
        now[0] += 301
        return dict(trusted)
    server._allowed = slow_guard
    seen=[]
    assert server.process_one(lambda *args: seen.append(True) or {'ok':True})['state'] == 'expired'
    assert seen == []


def test_input_requires_exact_generation_bound_to_frame_response():
    server, headers, _, _, _ = make(); session = begin(server,headers); screenshot(server,headers,session)
    _, response_headers, _ = server.request('GET','/api/frame',headers)
    first = int(response_headers['X-DouyinMind-Frame-Generation'])
    screenshot(server,headers,session)
    command={'session_id':session,'action':'click','payload':{'x':10,'y':10}}
    assert request(server,headers,'/api/action',command)[0] == 400
    assert request(server,headers,'/api/action',{**command,'frame_generation':first})[0] == 400
    assert request(server,headers,'/api/action',{**command,'frame_generation':True})[0] == 400


def test_frame_changes_during_control_guard_refuse_before_browser_input():
    server, headers, trusted, _, _ = make(); session = begin(server,headers); screenshot(server,headers,session)
    action(server,headers,session,'text',{'text':'synthetic'})
    def changed_guard():
        server._frame_generation += 1
        return dict(trusted)
    server._allowed = changed_guard
    seen=[]
    assert server.process_one(lambda *args: seen.append(True) or {'ok':True})['state'] == 'verification_not_ready'
    assert seen == []


def test_cancel_during_control_guard_does_not_execute_old_command():
    server, headers, trusted, _, _ = make(); session = begin(server,headers); screenshot(server,headers,session)
    action(server,headers,session,'text',{'text':'synthetic'})
    def changed_guard():
        action(server,headers,session,'cancel')
        return dict(trusted)
    server._allowed = changed_guard
    seen=[]
    assert server.process_one(lambda *args: seen.append(True) or {'ok':True})['state'] == 'cancelled'
    assert seen == []


def test_long_input_owner_can_check_lease_before_each_real_point():
    server, headers, _, _, now = make(); session=begin(server,headers); screenshot(server,headers,session)
    assert not server.input_lease_current()
    action(server,headers,session,'drag',{'points':[{'x':1,'y':1,'t':0},{'x':2,'y':2,'t':20}]})
    seen=[]
    def handler(*args):
        seen.append(server.input_lease_current())
        now[0] += 301
        seen.append(server.input_lease_current())
        return {'ok':True}
    server.process_one(handler)
    assert seen == [True,False] and not server.input_lease_current()


def test_operation_current_covers_begin_finish_only_during_same_owner_call():
    server,headers,_,_,_=make()
    request(server,headers,'/api/begin',{})
    seen=[]
    def begin_handler(*args):
        seen.append((server.operation_current(),server.input_lease_current()))
        return {'ok':True}
    server.process_one(begin_handler)
    assert not server.operation_current()
    session=json.loads(server.request('GET','/api/status',headers)[2])['session_id']
    action(server,headers,session,'finish')
    def finish_handler(*args):
        seen.append((server.operation_current(),server.input_lease_current()))
        return {'ok':True,'verified':True}
    server.process_one(finish_handler)
    assert seen == [(True,False),(True,False)] and not server.operation_current()


def test_cancel_before_owner_pump_keeps_sticky_pause_finalization():
    server,headers,_,_,_=make()
    response=request(server,headers,'/api/begin',{})
    session=json.loads(response[2])['session_id']
    epoch=server.finalization_epoch
    assert server.owner_finalization_pending
    assert action(server,headers,session,'cancel')[0] == 200
    assert not server.session_active and server.owner_finalization_pending
    assert server.acknowledge_finalization(epoch)
    assert not server.owner_finalization_pending


def test_expired_pending_begin_keeps_owner_pause_signal():
    server,headers,_,_,now=make();request(server,headers,'/api/begin',{})
    epoch=server.finalization_epoch;now[0]+=301
    assert not server.session_active and server.owner_finalization_pending
    assert server.process_one(lambda *_: pytest.fail('expired')) is None
    assert server.acknowledge_finalization(epoch)


def test_ack_never_clears_active_or_newer_cancelled_begin():
    server,headers,_,_,_=make();session=begin(server,headers)
    old=server.finalization_epoch
    assert not server.acknowledge_finalization(old)
    action(server,headers,session,'cancel');server.process_one(lambda *_:{'ok':True})
    newer=json.loads(request(server,headers,'/api/begin',{})[2])['session_id']
    latest=server.finalization_epoch
    assert latest>old
    action(server,headers,newer,'cancel')
    assert not server.acknowledge_finalization(old) and server.owner_finalization_pending
    assert server.acknowledge_finalization(latest)


def test_finalization_ack_rechecks_trusted_pause_and_is_owner_only():
    server,headers,trusted,_,_=make();session=begin(server,headers)
    action(server,headers,session,'cancel');server.process_one(lambda *_:{'ok':True})
    trusted['state']='running'
    assert not server.acknowledge_finalization(server.finalization_epoch)
    assert server.owner_finalization_pending
    errors=[]
    def other():
        try:server.acknowledge_finalization(server.finalization_epoch)
        except ManualVerificationError as error:errors.append(error.code)
    thread=threading.Thread(target=other);thread.start();thread.join()
    assert errors==['manual_owner_thread_required']


def test_new_begin_during_ack_control_read_cannot_be_consumed():
    server,headers,trusted,_,_=make();session=begin(server,headers)
    action(server,headers,session,'cancel');server.process_one(lambda *_:{'ok':True})
    old=server.finalization_epoch
    def changed():
        new=json.loads(request(server,headers,'/api/begin',{})[2])['session_id']
        action(server,headers,new,'cancel')
        return dict(trusted)
    server._allowed=changed
    assert not server.acknowledge_finalization(old)
    assert server.owner_finalization_pending and server.finalization_epoch>old
