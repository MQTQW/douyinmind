"""History management uses isolated SQLite and fake model clients only."""
import datetime
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.entities import ChatMessage, ChatSession, VideoCache


@pytest.fixture
def service(monkeypatch):
    chroma = ModuleType("app.services.chroma_service")
    chroma.get_chroma_service = lambda: SimpleNamespace(count=lambda: 0)
    llm = ModuleType("app.services.llm_service")
    llm.llm_client = SimpleNamespace(
        chat=lambda **kwargs: "继续回答。",
        stream_chat=lambda **kwargs: iter(["继续回答。"]),
    )
    llm.embedding_client = SimpleNamespace()
    monkeypatch.setitem(sys.modules, chroma.__name__, chroma)
    monkeypatch.setitem(sys.modules, llm.__name__, llm)
    path = Path(__file__).resolve().parents[1] / "app/services/rag_service.py"
    spec = importlib.util.spec_from_file_location("history_rag_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RagService()


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(service, db, monkeypatch):
    service_module = ModuleType("app.services.rag_service")
    service_module.rag_service = service
    monkeypatch.setitem(sys.modules, service_module.__name__, service_module)
    path = Path(__file__).resolve().parents[1] / "app/api/routes/chat.py"
    spec = importlib.util.spec_from_file_location("history_route_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)

    def test_db():
        yield db

    app.dependency_overrides[module.get_db] = test_db
    with TestClient(app) as result:
        yield result


def conversation(db, title="测试对话"):
    item = ChatSession(title=title)
    db.add(item)
    db.flush()
    db.add_all([
        ChatMessage(session_id=item.id, role="user", content="旧问题"),
        ChatMessage(session_id=item.id, role="assistant", content="旧回答"),
    ])
    db.commit()
    return item


def test_restore_history_with_sources_and_stable_message_order(service, db):
    item = conversation(db)
    rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.id)))
    same_time = datetime.datetime(2026, 10, 14, 9)
    for row in rows:
        row.created_at = same_time
    rows[0].retrieved_video_ids = '["sample"]'
    rows[1].retrieved_video_ids = '["sample", "sample", "missing"]'
    rows[1].latency_ms = 123
    db.add(VideoCache(platform_item_id="sample", title="来源视频"))
    db.commit()

    messages = service.get_messages(db, item.id)
    assert [message["content"] for message in messages] == ["旧问题", "旧回答"]
    assert messages[0]["sources"] == []
    assert messages[1]["latency_ms"] == 123
    assert messages[1]["sources"] == [
        {"platform_item_id": "sample", "title": "来源视频", "url": "https://www.douyin.com/video/sample", "score": None},
        {"platform_item_id": "missing", "title": "视频 missing", "url": "https://www.douyin.com/video/missing", "score": None},
    ]
    rows[1].retrieved_video_ids = "malformed legacy JSON"
    db.commit()
    assert service.get_messages(db, item.id)[1]["sources"] == []


def test_rename_and_delete_only_target_session(client, db):
    first = conversation(db, "原名字")
    second = conversation(db, "保留的对话")
    response = client.patch(f"/chat/sessions/{first.id}", json={"title": "  新名字  "})
    assert response.json() == {"success": True, "session_id": first.id, "title": "新名字"}
    history = client.get(f"/chat/sessions/{first.id}/messages").json()
    assert history["title"] == "新名字"
    assert [item["content"] for item in history["items"]] == ["旧问题", "旧回答"]
    listed = client.get("/chat/sessions").json()
    assert {item["title"] for item in listed["items"]} == {"新名字", "保留的对话"}

    assert client.delete(f"/chat/sessions/{first.id}").json()["success"] is True
    assert db.get(ChatSession, first.id) is None
    assert list(db.scalars(select(ChatMessage).where(ChatMessage.session_id == first.id))) == []
    assert db.get(ChatSession, second.id).title == "保留的对话"
    assert len(list(db.scalars(select(ChatMessage).where(ChatMessage.session_id == second.id)))) == 2


@pytest.mark.parametrize("title", ["", " \n\t ", "字" * 129])
def test_invalid_rename_keeps_original_title(client, db, title):
    item = conversation(db, "保留原名字")
    assert client.patch(f"/chat/sessions/{item.id}", json={"title": title}).status_code == 422
    assert db.get(ChatSession, item.id).title == "保留原名字"


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/chat/sessions/999/messages", None),
    ("PATCH", "/chat/sessions/999", {"title": "新名字"}),
    ("DELETE", "/chat/sessions/999", None),
    ("POST", "/chat/ask", {"query": "你好", "session_id": 999}),
])
def test_unknown_session_returns_404_without_creating_history(client, db, method, path, body):
    assert client.request(method, path, json=body).status_code == 404
    assert list(db.scalars(select(ChatSession))) == []
    assert list(db.scalars(select(ChatMessage))) == []


def test_followup_uses_selected_history_and_preserves_title(service, db, monkeypatch):
    item = conversation(db, "自定义名字")
    prompts = []
    llm = sys.modules["app.services.llm_service"].llm_client

    def stream_chat(**kwargs):
        prompts.append(kwargs["user_prompt"])
        yield "继续回答。"

    monkeypatch.setattr(llm, "stream_chat", stream_chat)
    events = list(service.answer_stream(db, "你好，继续说", item.id))
    assert events[-2][1]["session_id"] == item.id
    assert "旧问题" in prompts[0]
    assert "旧回答" in prompts[0]
    assert len(service.get_messages(db, item.id)) == 4
    assert db.get(ChatSession, item.id).title == "自定义名字"
    assert len(list(db.scalars(select(ChatSession)))) == 1


def test_stream_unknown_session_never_calls_model_or_recreates_history(service, db, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("Unknown history must fail before model use")

    monkeypatch.setattr(sys.modules["app.services.llm_service"].llm_client, "stream_chat", forbidden)
    events = list(service.answer_stream(db, "你好", 999))
    assert len(events) == 1
    assert events[0][0] == "error"
    assert list(db.scalars(select(ChatSession))) == []
    assert list(db.scalars(select(ChatMessage))) == []
