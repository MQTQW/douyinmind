"""Stream regression tests with real SQLite persistence and fake upstream AI services.

No API keys, network access, or ChromaDB installation are needed for this file.
"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import ChatMessage


@pytest.fixture
def rag_module(monkeypatch):
    chroma = ModuleType("app.services.chroma_service")
    chroma.get_chroma_service = lambda: SimpleNamespace(count=lambda: 0)
    llm = ModuleType("app.services.llm_service")
    llm.llm_client = SimpleNamespace(stream_chat=lambda **kwargs: iter(["你好，", "世界！"]))
    llm.embedding_client = SimpleNamespace()
    monkeypatch.setitem(sys.modules, chroma.__name__, chroma)
    monkeypatch.setitem(sys.modules, llm.__name__, llm)
    path = Path(__file__).resolve().parents[1] / "app/services/rag_service.py"
    spec = importlib.util.spec_from_file_location("streaming_rag_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def test_deltas_arrive_before_completion_and_messages_are_saved(rag_module, db):
    stream = rag_module.RagService().answer_stream(db, "你好", None)
    assert next(stream) == ("sources", {"sources": []})
    assert next(stream) == ("delta", {"text": "你好，"})
    assert list(db.scalars(select(ChatMessage))) == []
    assert next(stream) == ("delta", {"text": "世界！"})
    meta_event, meta = next(stream)
    assert meta_event == "meta"
    assert meta["answer"] == "你好，世界！"
    assert meta["session_id"] > 0
    assert len(meta["trace"]["steps"]) == 4
    rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.id)))
    assert [(m.role, m.content) for m in rows] == [("user", "你好"), ("assistant", meta["answer"])]
    assert next(stream) == ("done", {"ok": True})
    assert list(stream) == []

    followup = list(rag_module.RagService().answer_stream(db, "你好", meta["session_id"]))
    assert followup[-2][1]["session_id"] == meta["session_id"]
    assert len(list(db.scalars(select(ChatMessage)))) == 4


def test_final_answer_matches_saved_answer_and_retains_sources_and_trace(rag_module, db, monkeypatch):
    service = rag_module.RagService()
    hit = {"platform_item_id": "test-video", "chunk_id": "test-video-0", "title": "测试视频", "text": "学习内容", "score": 0.9}
    monkeypatch.setattr(service, "_dense_retrieve", lambda *args: [hit])
    monkeypatch.setattr(service, "_build_context", lambda *args: "学习内容")
    monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter(["## 要点\n", "- 学习内容[来源: 资料1]"]))
    events = list(service.answer_stream(db, "总结学习方法", None))
    meta = events[-2][1]
    assert events[-2][0] == "meta"
    assert events[-1] == ("done", {"ok": True})
    assert "##" not in meta["answer"]
    assert "测试视频" in meta["answer"]
    assert meta["sources"][0]["platform_item_id"] == "test-video"
    assert meta["trace"]["chunks"][0]["chunk_id"] == "test-video-0"
    assistant = db.scalar(select(ChatMessage).where(ChatMessage.role == "assistant"))
    assert assistant.content == meta["answer"]


def test_generation_failure_has_no_done_or_partial_history(rag_module, db, monkeypatch):
    def fail(**kwargs):
        yield "已经显示的部分"
        raise RuntimeError("模拟生成失败")

    monkeypatch.setattr(rag_module.llm_client, "stream_chat", fail)
    events = list(rag_module.RagService().answer_stream(db, "你好", None))
    assert [name for name, _ in events] == ["sources", "delta", "error"]
    assert events[-1][1]["code"] == "CHAT_INTERNAL_ERROR"
    assert "模拟生成失败" not in events[-1][1]["message"]
    assert list(db.scalars(select(ChatMessage))) == []


def test_route_streams_in_thread_pool_with_session_alive(rag_module, monkeypatch):
    import asyncio
    import threading
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    service_module = ModuleType("app.services.rag_service")
    state = {"open": False, "closed": False, "on_event_loop": None}

    class ManagedSession:
        def __enter__(self):
            state["open"] = True
            return self

        def __exit__(self, *args):
            state["open"] = False
            state["closed"] = True

    def fake_answer_stream(db, query, session_id, collection_id):
        assert state["open"]
        assert (query, session_id, collection_id) == ("测试", 7, "收藏夹1")
        try:
            asyncio.get_running_loop()
            state["on_event_loop"] = True
        except RuntimeError:
            state["on_event_loop"] = False
        state["started"].set()
        assert state["release"].wait(5)
        yield "delta", {"text": "你好"}
        assert state["open"]
        yield "meta", {"session_id": 7, "answer": "你好", "sources": []}
        yield "done", {"ok": True}

    state["started"] = threading.Event()
    state["release"] = threading.Event()
    service_module.rag_service = SimpleNamespace(answer_stream=fake_answer_stream)
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)
    path = Path(__file__).resolve().parents[1] / "app/api/routes/chat.py"
    spec = importlib.util.spec_from_file_location("streaming_route_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "session_factory", ManagedSession)
    app = FastAPI()
    app.include_router(module.router)

    @app.get("/health")
    async def health():
        return {"ok": True}

    with TestClient(app) as client:
        result = {}

        def request():
            result["response"] = client.post("/chat/ask/stream", json={"query": "测试", "session_id": 7, "collection_id": "收藏夹1"})

        thread = threading.Thread(target=request)
        thread.start()
        try:
            assert state["started"].wait(5)
            started = time.perf_counter()
            assert client.get("/health").json() == {"ok": True}
            assert time.perf_counter() - started < 2
        finally:
            state["release"].set()
            thread.join(timeout=5)
        assert not thread.is_alive()
        response = result["response"]
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert 'event: delta\ndata: {"text": "你好"}\n\n' in response.text
        assert response.text.index("event: meta") < response.text.index("event: done")
        assert state["on_event_loop"] is False
        assert state["closed"] is True
