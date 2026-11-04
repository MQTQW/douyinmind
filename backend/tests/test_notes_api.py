"""Loopback ASGI tests with TEMP business SQL and the inert notes fixtures."""
import json
from types import SimpleNamespace
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import notes as routes
from app.api.routes import knowledge as legacy
from tests.test_notes_persistence import context, submit, VIDEO, PRIVATE


@pytest.fixture
def api(context, monkeypatch):
    c = context
    monkeypatch.setattr(routes, 'notes_service', c.service)
    from app.services import notes_service as service_module
    monkeypatch.setattr(service_module, 'notes_service', c.service)
    app = FastAPI(); app.include_router(routes.router, prefix='/api'); app.include_router(legacy.router, prefix='/api')
    def db():
        yield c.db
    app.dependency_overrides[routes.get_db] = db
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50001),
                    headers={'origin': 'http://127.0.0.1:5173'}) as client:
        yield SimpleNamespace(c=c, client=client, app=app)


def body(c, **kwargs):
    value = {'request_id': str(uuid.uuid4()), 'confirm': True,
             'expected_source_fingerprint': c.service.source_view(c.db, VIDEO)['fingerprint']}
    value.update(kwargs)
    return value


def test_capabilities_and_history_are_read_only_without_model_or_login(api):
    c = api.c
    capability = api.client.get('/api/notes/capabilities').json()
    assert capability['version'] == 1 and capability['ready'] is True and capability['max_expected_calls'] == 11
    assert capability['usage_is_invoice'] is False
    history = api.client.get(f'/api/notes/videos/{VIDEO}').json()
    assert history['source']['status'] == 'ready' and history['source']['expected_call_count'] == 1
    assert history['items'] == [] and history['total'] == 0
    assert PRIVATE not in json.dumps(history) and not c.model.calls and not c.queue.items
    result = api.client.get('/api/notes/requests/' + str(uuid.uuid4())).json()
    assert result['success'] and result['found'] is False and not c.model.calls


def test_known_source_rejection_reports_no_new_request_but_queue_unknown_does_not(api):
    c, client = api.c, api.client
    invalid = client.post(f'/api/notes/videos/{VIDEO}', json=body(c, expected_source_fingerprint='f' * 64))
    assert invalid.json()['request_registered'] is False and not invalid.json()['needs_review']
    c.queue.fail = True
    unknown = client.post(f'/api/notes/videos/{VIDEO}', json=body(c))
    assert unknown.json()['needs_review'] is True and unknown.json().get('request_registered') is not False


@pytest.mark.parametrize('override', [{'confirm': False}, {'confirm': 1}, {'redo': 1}, {'extra': 'unused'},
                                     {'expected_source_fingerprint': 'invalid'}])
def test_invalid_or_unconfirmed_request_cannot_submit(api, override):
    response = api.client.post(f'/api/notes/videos/{VIDEO}', json=body(api.c, **override))
    assert response.status_code in {409, 422} and not api.c.queue.items and not api.c.model.calls


def test_uuid_post_lookup_and_download_do_not_regenerate_and_legacy_ai_get_is_pure(api):
    c, client = api.c, api.client
    request = body(c)
    response = client.post(f'/api/notes/videos/{VIDEO}', json=request).json()
    assert response['success'] and response['item']['state'] == 'queued'
    c.queue.execute()
    result = client.get('/api/notes/requests/' + request['request_id']).json()['item']
    assert result['state'] == 'done' and len(c.model.calls) == 1
    expected = f"/api/notes/artifacts/{result['note_id']}.md"
    assert result['download_url'] == expected
    artifact = client.get(expected)
    assert artifact.status_code == 200 and artifact.headers['cache-control'] == 'no-store'
    assert PRIVATE in artifact.text and '[00:01' in artifact.text
    assert client.get(expected).content == artifact.content
    assert client.post(f'/api/notes/videos/{VIDEO}', json=request).json()['duplicate']
    compatibility = client.get(f'/api/knowledge/export/{VIDEO}?mode=ai')
    assert compatibility.status_code == 200 and compatibility.content == artifact.content
    assert len(c.model.calls) == 1 and not c.queue.items


def test_legacy_ai_get_without_saved_notes_does_not_create_or_pay(api):
    result = api.client.get(f'/api/knowledge/export/{VIDEO}?mode=ai')
    assert result.status_code == 409 and result.json()['code'] == 'NOTES_ARTIFACT_UNAVAILABLE'
    assert not api.c.queue.items and not api.c.model.calls


def test_unknown_post_result_remains_same_uuid_get_lookup_and_does_not_auto_queue(api):
    c, client = api.c, api.client
    request = body(c); c.queue.fail = True
    response = client.post(f'/api/notes/videos/{VIDEO}', json=request)
    assert response.status_code == 409 and response.json()['needs_review'] is True
    result = client.get('/api/notes/requests/' + request['request_id']).json()
    assert result['found'] and result['item']['state'] == 'needs_review'
    assert client.post(f'/api/notes/videos/{VIDEO}', json=request).json()['duplicate']
    c.queue.execute()
    assert not c.model.calls and not c.queue.items


@pytest.mark.parametrize('path', ['/api/notes/capabilities', f'/api/notes/videos/{VIDEO}',
                                 '/api/notes/requests/00000000-0000-0000-0000-000000000001'])
def test_notes_loopback_boundary_is_checked_before_read_or_mutation(api, path):
    with TestClient(api.app, base_url='http://127.0.0.1:8000', client=('203.0.113.10', 50001)) as remote:
        assert remote.get(path).status_code == 403
    assert api.client.get(path, headers={'origin': 'https://outside.invalid'}).status_code == 403
    assert not api.c.queue.items and not api.c.model.calls


def test_history_query_validation_is_bounded_and_bad_download_cannot_generate(api):
    for query in ['limit=0', 'limit=101', 'offset=-1']:
        assert api.client.get(f'/api/notes/videos/{VIDEO}?{query}').status_code == 422
    assert api.client.get('/api/notes/artifacts/not-a-uuid.md').status_code == 422
    assert api.client.get('/api/notes/artifacts/' + str(uuid.uuid4()) + '.md').status_code == 409
    assert not api.c.model.calls


def test_routes_schema_and_startup_register_notes_recovery_without_background_replay():
    import ast
    from pathlib import Path
    root = Path(__file__).parents[1] / 'app'
    main = ast.parse((root / 'main.py').read_text(encoding='utf-8'))
    calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)]
    assert any(isinstance(node.func, ast.Name) and node.func.id == 'recover_incomplete_notes' for node in calls)
    router = (root / 'api/router.py').read_text(encoding='utf-8')
    assert 'include_router(notes.router)' in router
