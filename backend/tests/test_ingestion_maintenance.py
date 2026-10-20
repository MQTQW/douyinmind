"""Explicit recovery uses one-process admission, isolated SQLite and fake work."""
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes import knowledge as routes
from app.db.base import Base
from app.models.entities import VideoCache
from app.services import knowledge_service as module
from app.services.worker import Worker, WorkerBusyError


@pytest.fixture
def maintenance_context(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    worker = Worker()
    checks = []
    monkeypatch.setattr(module, "worker", worker)
    monkeypatch.setattr(routes, "worker", worker)
    monkeypatch.setattr(module.omni_service, "check_configuration", lambda: checks.append(True))
    with Session(engine, expire_on_commit=True) as db:
        for status in ("pending", "failed", "done", "downloading", "transcribing", "embedding"):
            db.add(VideoCache(platform_item_id=status, title=status, status=status,
                transcript_text=f"preserved-{status}", analysis_json='{"raw_results":["preserved"]}'))
        db.commit()
        app = FastAPI()
        app.include_router(routes.router, prefix="/api")
        app.dependency_overrides[routes.get_db] = lambda: db
        with TestClient(app) as client:
            yield db, worker, checks, client, app
    engine.dispose()


def test_explicit_reset_recovers_interrupted_rows_without_erasing_saved_content(maintenance_context):
    db, worker, checks, client, _ = maintenance_context
    response = client.post("/api/knowledge/reset-failed").json()
    assert response == {"success": True, "reset_count": 4}
    rows = list(db.scalars(select(VideoCache)).all())
    assert {row.platform_item_id: row.status for row in rows} == {
        status: "done" if status == "done" else "pending"
        for status in ("pending", "failed", "done", "downloading", "transcribing", "embedding")}
    assert all(row.transcript_text == f"preserved-{row.platform_item_id}" for row in rows)
    assert all(row.analysis_json == '{"raw_results":["preserved"]}' for row in rows)
    assert checks == [] and worker.get_current_task() is None


def test_a_queued_job_blocks_reset_before_its_current_task_is_assigned(maintenance_context):
    db, worker, _, client, _ = maintenance_context
    worker.submit("queued-job", lambda task_id: pytest.fail("No test job may run"))
    assert worker.get_current_task() is None
    result = client.post("/api/knowledge/reset-failed").json()
    assert result["success"] is False
    assert db.scalar(select(VideoCache.status).where(VideoCache.platform_item_id == "transcribing")) == "transcribing"


def test_submission_during_explicit_reset_is_rejected_without_queueing_or_model_checks(maintenance_context):
    _, worker, checks, client, _ = maintenance_context
    with worker.maintenance_guard():
        response = client.post("/api/knowledge/sync", json={"selected_ids": ["pending"]}).json()
    assert response["success"] is False
    assert checks == []
    assert worker._queue.empty()


def test_submission_closes_expiring_session_transaction_before_publishing(maintenance_context, monkeypatch):
    db, worker, checks, _, _ = maintenance_context
    original = worker.submit
    verified = []

    def publish(*args, **kwargs):
        assert not db.in_transaction(), "No lazy refresh/read transaction may leak across publication"
        with pytest.raises(WorkerBusyError):
            with worker.maintenance_guard():
                pytest.fail("Submission reservation must still exclude recovery")
        verified.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, "submit", publish)
    result = module.KnowledgeService().start_sync(db, ["pending"])
    assert result["pending_count"] == 1 and verified == [True] and checks == [True]
    assert not db.in_transaction()


def test_no_work_submission_ends_its_read_transaction_before_releasing_admission(maintenance_context):
    db, worker, _, _, _ = maintenance_context
    result = module.KnowledgeService().start_sync(db, ["done"])
    assert result["task_id"] is None and not db.in_transaction()
    with worker.maintenance_guard():
        pass


def test_validation_error_rolls_back_and_releases_submission_admission(maintenance_context):
    db, worker, _, _, _ = maintenance_context
    with pytest.raises(ValueError):
        module.KnowledgeService().start_sync(db, ["unknown"])
    assert not db.in_transaction()
    with worker.maintenance_guard():
        pass


def test_reset_sql_runs_outside_the_asgi_event_loop_thread(maintenance_context, monkeypatch):
    db, _, _, client, app = maintenance_context
    threads = {}

    async def capture_event_loop(request, call_next):
        threads["loop"] = threading.get_ident()
        return await call_next(request)

    original = db.execute
    def execute(*args, **kwargs):
        threads["sql"] = threading.get_ident()
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "execute", execute)
    # A new app instance is necessary because TestClient has already started
    # the fixture's app and FastAPI does not allow middleware edits afterward.
    isolated = FastAPI()
    isolated.include_router(routes.router, prefix="/api")
    isolated.dependency_overrides[routes.get_db] = lambda: db
    isolated.middleware("http")(capture_event_loop)
    with TestClient(isolated) as http:
        assert http.post("/api/knowledge/reset-failed").json()["success"]
    assert threads["sql"] != threads["loop"]


def test_failed_queue_publication_removes_phantom_and_does_not_run_an_orphan(monkeypatch):
    worker = Worker()
    actual_put = worker._queue.put_nowait
    called = []

    def delivered_then_failed(value):
        actual_put(value)
        raise RuntimeError("synthetic publication failure")
    monkeypatch.setattr(worker._queue, "put_nowait", delivered_then_failed)
    with pytest.raises(RuntimeError, match="synthetic publication failure"):
        worker.submit("orphan", lambda task_id: called.append(task_id))
    assert worker.get_progress("orphan") is None
    with worker.maintenance_guard():
        worker.start()
        deadline = time.monotonic() + 2
        while worker._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.005)
        assert worker._queue.unfinished_tasks == 0
    worker.stop()
    worker._thread.join(timeout=2)
    assert called == [] and not worker._thread.is_alive()
