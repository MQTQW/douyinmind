"""A waiting non-streaming answer must not block the app's event loop."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_health_responds_while_nonstreaming_answer_waits(monkeypatch):
    started, release = Event(), Event()
    services = ModuleType("app.services.rag_service")
    def answer(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5), "Test must release the waiting fake model"
        return {"answer": "已完成", "session_id": 1, "sources": [], "route_type": "vector"}
    services.rag_service = SimpleNamespace(answer=answer)
    database = ModuleType("app.db.session")
    def get_db():
        yield SimpleNamespace()
    database.get_db = get_db
    database.session_factory = lambda: None
    monkeypatch.setitem(sys.modules, services.__name__, services)
    monkeypatch.setitem(sys.modules, database.__name__, database)
    source = Path(__file__).resolve().parents[1] / "app/api/routes/chat.py"
    spec = importlib.util.spec_from_file_location("responsive_chat_route", source)
    routes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(routes)
    app = FastAPI()
    app.include_router(routes.router)
    @app.get("/health-test")
    async def health():
        return {"ready": True}
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        asking = pool.submit(client.post, "/chat/ask", json={"query": "等待中的有效问题"})
        try:
            assert started.wait(timeout=2)
            health_response = pool.submit(client.get, "/health-test").result(timeout=1)
            assert health_response.json() == {"ready": True}
            assert not asking.done(), "Health must respond before answer work finishes"
        finally:
            release.set()
        assert asking.result(timeout=2).json()["success"] is True
