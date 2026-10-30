"""ASGI route contracts driven without sockets, threads or an event loop."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from app.assistant import evidence_routes as routes
from app.assistant.routes import local_request
from app.assistant.operator_draft_service import OperatorDraftError


TASK = 'a' * 32


def finish(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError('route unexpectedly requested external/asynchronous I/O')


def request(body=b'{}', *, origin='http://127.0.0.1:5173', content_type='application/json'):
    async def stream():
        yield body
    return SimpleNamespace(method='POST', client=SimpleNamespace(host='127.0.0.1'),
        url=SimpleNamespace(hostname='127.0.0.1'),
        headers={'origin': origin, 'content-type': content_type}, stream=stream)


@pytest.fixture(autouse=True)
def inline_threadpool(monkeypatch):
    async def immediate(function, *args):
        return function(*args)
    monkeypatch.setattr(routes, 'run_in_threadpool', immediate)


def test_existing_execution_url_and_new_local_only_routes_remain_exact():
    paths = {route.path for route in routes.router.routes}
    assert '/assistant/evidence/query' in paths
    assert {'/assistant/operator-query-draft/{task_id}/evidence',
        '/assistant/operator-query-draft/{task_id}',
        '/assistant/operator-query-draft/{task_id}/bundle'} <= paths
    assert routes.router.dependencies
    assert routes.router.dependencies[0].dependency is local_request


def test_new_routes_only_delegate_evidence_register_bundle_without_task_execution():
    calls = []
    def action(name):
        def call(*args):
            calls.append((name, args))
            return {'kind': name}
        return call
    service = SimpleNamespace(evidence=action('evidence'), register=action('register'), bundle=action('bundle'))
    assert finish(routes.operator_draft_evidence(TASK, service)) == {'kind': 'evidence'}
    body = {'text': 'Synthetic answer', 'source_ids': ['source1'],
            'expected_snapshot_digest': 'b' * 64, 'expected_evidence_digest': 'c' * 64}
    raw = json.dumps(body).encode()
    assert finish(routes.operator_draft_register(TASK, request(raw), service)) == {'kind': 'register'}
    assert finish(routes.operator_draft_bundle(TASK, service)) == {'kind': 'bundle'}
    assert calls == [('evidence', (TASK,)), ('register', (TASK, body)), ('bundle', (TASK,))]


@pytest.mark.parametrize('raw', [b'[]x', b'{"text":"x","text":"y"}', b'{"n":NaN}'])
def test_bad_or_duplicate_json_never_reaches_candidate_registration(raw):
    service = SimpleNamespace(register=lambda *_: pytest.fail('bad request reached registration'))
    with pytest.raises(HTTPException) as error:
        finish(routes.operator_draft_register(TASK, request(raw), service))
    assert error.value.status_code == 422


def test_large_stream_is_refused_before_service():
    service = SimpleNamespace(register=lambda *_: pytest.fail('oversized registration'))
    with pytest.raises(HTTPException) as error:
        finish(routes.operator_draft_register(TASK, request(b'x' * (routes.MAX_REQUEST_BYTES + 1)), service))
    assert error.value.status_code == 413


def test_fixed_errors_do_not_echo_private_provider_or_candidate_data():
    def known(*_):
        raise OperatorDraftError('operator_draft_scope_changed', 403)
    with pytest.raises(HTTPException) as error:
        finish(routes.operator_draft_evidence(TASK, SimpleNamespace(evidence=known)))
    assert error.value.status_code == 403 and error.value.detail == {'code': 'operator_draft_scope_changed'}
    def private(*_):
        raise RuntimeError('PRIVATE-CANDIDATE-TEXT https://private.invalid secret')
    with pytest.raises(HTTPException) as error:
        finish(routes.operator_draft_bundle(TASK, SimpleNamespace(bundle=private)))
    assert error.value.status_code == 503 and error.value.detail == {'code': 'operator_draft_unavailable'}


def test_nonlocal_origin_or_nonjson_cannot_register_a_candidate():
    with pytest.raises(HTTPException) as error:
        local_request(request(origin='https://foreign.invalid'))
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        local_request(request(content_type='text/plain'))
    assert error.value.status_code == 415


def test_recorded_evidence_cold_index_never_initializes_or_creates_collection(monkeypatch):
    from app.services import chroma_service
    from app.assistant.operator_draft_service import _default_documents
    monkeypatch.setattr(chroma_service, '_chroma_service', None)
    monkeypatch.setattr(chroma_service, 'get_chroma_service',
        lambda: pytest.fail('operator GET initialized Chroma'))
    with pytest.raises(OperatorDraftError, match='operator_draft_index_not_loaded'):
        _default_documents({'7999999000000000001'})
    calls = []
    existing = SimpleNamespace(get_documents=lambda scope: calls.append(scope) or [])
    monkeypatch.setattr(chroma_service, '_chroma_service', existing)
    assert _default_documents({'7999999000000000001'}) == []
    assert calls == [{'7999999000000000001'}]
