"""Selection boundaries for ingestion; all model/worker actions are stubbed."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.routes.knowledge import router
from app.db.base import Base
from app.db.session import get_db
from app.models.entities import VideoCache
from app.services import knowledge_service as knowledge_module


@pytest.fixture
def selection_context(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        for item_id, status in [("pending-1", "pending"), ("pending-2", "pending"),
                                ("failed-1", "failed"), ("failed-2", "failed"),
                                ("done-1", "done"), ("running-1", "transcribing")]:
            db.add(VideoCache(platform_item_id=item_id, title=item_id, status=status,
                              transcript_text=f"保留原文-{item_id}",
                              visual_analysis_text=f"保留画面-{item_id}",
                              analysis_json='{"raw_results":["原始返回"]}',
                              error_message="旧失败信息" if status == "failed" else ""))
        db.commit()
        submissions, checks = [], []
        monkeypatch.setattr(knowledge_module.worker, "submit",
                            lambda *args: submissions.append(args))
        monkeypatch.setattr(knowledge_module.omni_service, "check_configuration",
                            lambda: checks.append(True))
        app = FastAPI()
        app.include_router(router, prefix="/api")
        def test_db():
            yield db
        app.dependency_overrides[get_db] = test_db
        with TestClient(app) as client:
            yield SimpleNamespace(db=db, client=client, submissions=submissions,
                                  checks=checks)
    engine.dispose()


@pytest.mark.parametrize("body", [{}, {"selected_ids": None}])
def test_omitted_or_null_selection_keeps_all_pending_behavior(selection_context, body):
    context = selection_context
    result = context.client.post("/api/knowledge/sync", json=body).json()
    assert result["success"] is True
    assert result["pending_count"] == 2
    assert context.submissions[0][2] == ["pending-1", "pending-2"]
    assert context.checks == [True]


def test_explicit_empty_selection_never_submits_all_pending(selection_context):
    context = selection_context
    response = context.client.post("/api/knowledge/sync", json={"selected_ids": []})
    assert response.json()["success"] is False
    assert "至少选择" in response.json()["message"]
    assert context.submissions == []
    assert context.checks == []


def test_selection_deduplicates_retries_only_selected_failures_and_preserves_data(selection_context):
    context = selection_context
    before = [(v.platform_item_id, v.status, v.transcript_text, v.visual_analysis_text,
               v.analysis_json, v.error_message) for v in context.db.query(VideoCache).all()]
    selected = ["failed-1", "pending-2", "failed-1", "done-1", "running-1"]
    result = context.client.post("/api/knowledge/sync", json={"selected_ids": selected}).json()
    assert result["success"] is True
    assert result["pending_count"] == 2
    assert result["skipped_count"] == 2
    assert len(context.submissions) == 1
    assert context.submissions[0][2] == ["failed-1", "pending-2"]
    after = [(v.platform_item_id, v.status, v.transcript_text, v.visual_analysis_text,
              v.analysis_json, v.error_message) for v in context.db.query(VideoCache).all()]
    assert after == before


@pytest.mark.parametrize("selected", [["unknown"], ["pending-1", "unknown"], [" "]])
def test_unknown_or_blank_selection_rejects_entire_job(selection_context, selected):
    context = selection_context
    result = context.client.post("/api/knowledge/sync", json={"selected_ids": selected}).json()
    assert result["success"] is False
    assert context.submissions == []
    assert context.checks == []


def test_completed_and_running_selection_does_not_repeat_processing(selection_context):
    context = selection_context
    result = context.client.post("/api/knowledge/sync", json={"selected_ids": ["done-1", "running-1"]}).json()
    assert result["success"] is True
    assert result["task_id"] is None
    assert result["pending_count"] == 0
    assert result["skipped_count"] == 2
    assert context.submissions == []
    assert context.checks == []
