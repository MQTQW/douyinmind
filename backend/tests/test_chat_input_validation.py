"""Reject meaningless chat requests before retrieval/generation, with no real DB."""
import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client_context(monkeypatch):
    calls = []

    def answer(db, query, session_id, collection_id, **kwargs):
        calls.append(("answer", query))
        return {"answer": "已按原文回答", "session_id": 1, "sources": []}

    def stream(db, query, session_id, collection_id, **kwargs):
        calls.append(("stream", query))
        yield "delta", {"text": "已按原文回答"}
        yield "done", {"ok": True}

    service_module = ModuleType("app.services.rag_service")
    service_module.rag_service = SimpleNamespace(answer=answer, answer_stream=stream)
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)

    database_module = ModuleType("app.db.session")

    def get_db():
        yield SimpleNamespace()

    @contextmanager
    def session_factory():
        yield SimpleNamespace()

    database_module.get_db = get_db
    database_module.session_factory = session_factory
    monkeypatch.setitem(sys.modules, database_module.__name__, database_module)
    path = Path(__file__).resolve().parents[1] / "app/api/routes/chat.py"
    spec = importlib.util.spec_from_file_location("input_validation_routes_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        yield client, calls


@pytest.mark.parametrize("path", ["/chat/ask", "/chat/ask/stream"])
@pytest.mark.parametrize("query", ["", "   ", "\n\t", "？" * 2001])
def test_invalid_query_is_422_before_any_rag_call(client_context, path, query):
    client, calls = client_context
    response = client.post(path, json={"query": query})
    assert response.status_code == 422
    assert calls == []


@pytest.mark.parametrize("path, mode", [("/chat/ask", "answer"), ("/chat/ask/stream", "stream")])
@pytest.mark.parametrize("query", ["你好", "  视频里提到的两个原因是什么？\n", "题" * 2000])
def test_valid_query_is_trimmed_and_both_interfaces_keep_working(client_context, path, mode, query):
    client, calls = client_context
    response = client.post(path, json={"query": query})
    assert response.status_code == 200
    assert calls == [(mode, query.strip())]
    if mode == "stream":
        assert "event: delta" in response.text and "event: done" in response.text
    else:
        assert response.json()["success"] is True


@pytest.mark.parametrize("path", ["/chat/ask", "/chat/ask/stream"])
def test_non_string_query_remains_invalid(client_context, path):
    client, calls = client_context
    assert client.post(path, json={"query": 123}).status_code == 422
    assert calls == []
