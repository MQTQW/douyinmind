"""ASGI + TEMP SQL + fake ready index only; no models, browser or storage init."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.api.routes import maintenance as routes
from app.db.base import Base
from app.models.entities import VideoCache
from tests.test_material_audit import Reader, census, count_row


PRIVATE = 'PRIVATE_SOURCE_KEY_SIGNED_URL_NEVER_EXPORTED'


@pytest.fixture
def context(tmp_path, monkeypatch):
    path = tmp_path / 'maintenance.sqlite3'
    engine = create_engine(f'sqlite:///{path}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.add(VideoCache(platform_item_id='local', title='合成标题', status='done', transcript_text=PRIVATE))
    db.commit()
    reader = Reader(census([count_row()]))
    monkeypatch.setattr(routes.chroma_service, '_chroma_service', reader)
    def forbidden(*args, **kwargs):
        pytest.fail('Maintenance GET must not initialize a client or storage')
    monkeypatch.setattr(routes.chroma_service, 'get_chroma_service', forbidden)
    monkeypatch.setattr(routes.chroma_service, 'ChromaService', forbidden)
    app = FastAPI(); app.include_router(routes.router, prefix='/api')
    def readonly():
        yield db
    app.dependency_overrides[routes.readonly_db] = readonly
    try:
        with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50000),
                        headers={'origin':'http://127.0.0.1:5173'}) as client:
            yield SimpleNamespace(db=db, engine=engine, path=path, reader=reader, app=app, client=client)
    finally:
        db.close(); engine.dispose()


@pytest.mark.parametrize('endpoint', ['material-audit', 'index-snapshot'])
def test_uninitialized_index_is_fixed_unknown_without_constructing_default_storage(context, monkeypatch, endpoint):
    monkeypatch.setattr(routes.chroma_service, '_chroma_service', None)
    def forbidden(*args, **kwargs):
        pytest.fail('Uninitialized maintenance must not execute SQL')
    monkeypatch.setattr(context.db, 'execute', forbidden)
    response = context.client.get('/api/maintenance/' + endpoint)
    assert response.status_code == 503 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body['code'] == 'MATERIAL_INDEX_NOT_INITIALIZED'
    assert body['read_complete'] is False and body['summary'] is None and body['total_issues'] is None
    assert context.reader.calls == []


@pytest.mark.parametrize('endpoint', ['material-audit', 'index-snapshot'])
def test_local_request_requires_loopback_host_and_same_origin_before_observation(context, endpoint):
    path = '/api/maintenance/' + endpoint
    with TestClient(context.app, base_url='http://127.0.0.1:8000', client=('203.0.113.9', 50000)) as remote:
        assert remote.get(path).status_code == 403
    with TestClient(context.app, base_url='http://outside.invalid', client=('127.0.0.1', 50000)) as wrong_host:
        assert wrong_host.get(path).status_code == 403
    assert context.client.get(path, headers={'origin':'https://outside.invalid'}).status_code == 403
    assert context.reader.calls == []


def test_audit_is_read_only_bounded_and_keeps_plaintext_out_of_the_response(context, monkeypatch):
    before = hashlib.sha256(context.path.read_bytes()).hexdigest()
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only observation must not commit or flush')
    monkeypatch.setattr(context.db, 'commit', forbidden)
    monkeypatch.setattr(context.db, 'flush', forbidden)
    response = context.client.get('/api/maintenance/material-audit?limit=2&offset=0')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body['read_complete'] and body['summary']['sql_done'] == body['summary']['index_records'] == 1
    assert body['issues'] == [] and body['repair_supported'] is False
    assert PRIVATE not in response.text and hashlib.sha256(context.path.read_bytes()).hexdigest() == before
    assert context.reader.calls == [{'verify_legacy_text':False}, {'verify_legacy_text':False}]


def test_legacy_text_verification_requires_an_explicit_query_and_stays_local(context):
    response = context.client.get('/api/maintenance/material-audit?verify_legacy_text=true')
    assert response.status_code == 200
    assert context.reader.calls == [{'verify_legacy_text':True}, {'verify_legacy_text':True}]
    assert PRIVATE not in response.text


def test_metadata_export_projects_whitelist_and_can_be_used_as_a_cli_snapshot(context):
    value = census([count_row()]); value.update(documents=[PRIVATE], embeddings=[[.1]], api_key=PRIVATE,
                                               signed_url='https://unit.invalid/?token=' + PRIVATE)
    context.reader.values = (value,)
    response = context.client.get('/api/maintenance/index-snapshot')
    assert response.status_code == 200
    body = response.json()
    assert body['read_only'] and body['read_complete'] and body['index_records'] == 1
    assert body['counts'][0]['content_chunks'] == 1
    assert not {'documents','embeddings','api_key','signed_url'} & body.keys()
    assert PRIVATE not in json.dumps(body) and 'https://' not in response.text
    from app.services.material_audit import validate_index_snapshot
    assert validate_index_snapshot(body)['index_records'] == 1


@pytest.mark.parametrize('endpoint', ['material-audit', 'index-snapshot'])
def test_snapshot_change_is_unknown_and_never_a_zero_count_healthy_result(context, endpoint):
    context.reader.values = (census([count_row()]), census([count_row(source=0, metadata=1)]))
    response = context.client.get('/api/maintenance/' + endpoint)
    assert response.status_code == 503
    body = response.json()
    assert body['snapshot_changed'] is True and body['read_complete'] is False and body['total_issues'] is None
    if body['summary'] is not None:
        assert body['summary']['issue_count'] is None and body['summary']['index_records'] is None


@pytest.mark.parametrize('endpoint', ['material-audit', 'index-snapshot'])
def test_index_read_errors_stay_unknown_and_never_echo_provider_diagnostics(context, endpoint):
    context.reader.values = (RuntimeError(PRIVATE),)
    response = context.client.get('/api/maintenance/' + endpoint)
    assert response.status_code == 503 and response.json()['read_complete'] is False
    assert response.json()['total_issues'] is None and PRIVATE not in response.text


def test_sql_read_error_does_not_erase_index_export_or_become_missing_material(context, monkeypatch):
    def broken(*args, **kwargs):
        raise OperationalError('synthetic', {}, RuntimeError(PRIVATE))
    monkeypatch.setattr(context.db, 'execute', broken)
    response = context.client.get('/api/maintenance/material-audit')
    assert response.status_code == 503
    body = response.json()
    assert body['summary']['sql_materials'] is None and body['total_issues'] is None
    assert PRIVATE not in response.text
    # This endpoint has no SQL dependency at all.
    assert context.client.get('/api/maintenance/index-snapshot').status_code == 200


@pytest.mark.parametrize('query', ['limit=0', 'limit=201', 'offset=-1', 'verify_legacy_text=not-a-bool'])
def test_invalid_query_never_reads_an_index(context, query):
    assert context.client.get('/api/maintenance/material-audit?' + query).status_code == 422
    assert context.reader.calls == []


@pytest.mark.parametrize('method', ['POST', 'DELETE', 'PUT', 'PATCH'])
def test_no_repair_or_mutation_method_is_exposed(context, method):
    assert context.client.request(method, '/api/maintenance/material-audit', json={}).status_code == 405
    assert context.reader.calls == []


def test_production_read_session_never_commits_on_success(monkeypatch):
    calls = []
    class ReadSession:
        def close(self): calls.append('close')
        def commit(self): pytest.fail('Read session must not commit')
    def factory(**kwargs):
        assert kwargs == {'autoflush':False}; return ReadSession()
    monkeypatch.setattr(routes, 'session_factory', factory)
    generator = routes.readonly_db()
    assert isinstance(next(generator), ReadSession)
    generator.close()
    assert calls == ['close']
