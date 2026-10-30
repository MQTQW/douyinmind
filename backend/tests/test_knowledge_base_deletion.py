"""Library deletion uses temporary SQLite and an idle, never-started worker."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.routes import knowledge_bases as routes
from app.api.routes import knowledge as video_routes
from app.assistant.registry import Registry, read_local_catalog
from app.db.base import Base
from app.db.migrations import ensure_knowledge_base_tombstones
from app.db.session import get_db
from app.mention_agent.contracts import Binding
from app.mention_agent.knowledge import LocalKnowledgeTools
from app.models.entities import (ChatMessage, ChatSession, FavoriteCollection, FavoriteVideo,
                                KnowledgeBase, KnowledgeBaseVideo, VideoCache)
from app.services.knowledge_base_service import KnowledgeBaseNotFound, knowledge_base_service as service
from app.services.worker import Worker, WorkerBusyError


@pytest.fixture
def context(tmp_path, monkeypatch):
    path = tmp_path / "business.sqlite3"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    ensure_knowledge_base_tombstones(engine)
    worker = Worker()  # No thread, media, model, browser or product lifecycle starts.
    monkeypatch.setattr(routes, "worker", worker)
    monkeypatch.setattr(video_routes, "worker", worker)
    with Session(engine, expire_on_commit=False) as db:
        first = service.create(db, "阅读资料")["id"]
        second = service.create(db, "其他资料")["id"]
        db.add(VideoCache(platform_item_id="shared", title="合成标题", status="done",
                          transcript_text="合成完整原文", visual_analysis_text="合成画面",
                          analysis_json='{"synthetic":true}'))
        db.flush()
        service.assign_videos(db, first, ["shared"])
        service.assign_videos(db, second, ["shared"])
        history = ChatSession(title="合成旧对话", knowledge_base_id=first)
        db.add(history)
        db.flush()
        db.add(ChatMessage(session_id=history.id, role="assistant", content="合成历史回答"))
        db.commit()
        app = FastAPI()
        app.include_router(routes.router, prefix="/api")
        app.include_router(video_routes.router, prefix="/api")
        def test_db():
            yield db
        app.dependency_overrides[get_db] = test_db
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                        headers={"origin": "http://127.0.0.1:5173", "content-type": "application/json"}) as client:
            yield SimpleNamespace(db=db, client=client, app=app, engine=engine, path=path,
                                  first=first, second=second, worker=worker, history_id=history.id)
    engine.dispose()


def remove(context, library_id):
    return context.client.request("DELETE", f"/api/knowledge-bases/{library_id}", json={})


def test_loaded_backend_advertises_guarded_deletion_capabilities(context, monkeypatch):
    result = context.client.get('/api/knowledge-bases').json()
    assert result['capabilities']['delete_knowledge_base'] is True
    monkeypatch.setattr(video_routes.knowledge_service, 'get_stats', lambda _db: {'video_cache': {'done': 1}})
    stats = context.client.get('/api/knowledge/stats').json()
    assert stats['capabilities']['guarded_video_delete'] is True
    assert stats['video_cache'] == {'done': 1}


def test_delete_keeps_shared_results_other_membership_and_history(context):
    response = remove(context, context.first)
    assert response.status_code == 200
    assert response.json() == {"success": True, "deleted_id": context.first, "removed_count": 1}
    tombstone = context.db.get(KnowledgeBase, context.first)
    assert tombstone.deleted_at is not None and tombstone.name == "阅读资料"
    assert tombstone.name_key == f"\x00deleted:{context.first}"
    assert context.db.get(KnowledgeBaseVideo, (context.first, "shared")) is None
    assert service.list_video_ids(context.db, context.second) == ["shared"]
    cache = context.db.scalar(select(VideoCache))
    assert (cache.status, cache.transcript_text, cache.visual_analysis_text, cache.analysis_json) == (
        "done", "合成完整原文", "合成画面", '{"synthetic":true}')
    history = context.db.get(ChatSession, context.history_id)
    assert history.knowledge_base_id == context.first and history.title == "合成旧对话"
    assert context.db.scalar(select(ChatMessage.content)) == "合成历史回答"
    assert [item["id"] for item in context.client.get("/api/knowledge-bases").json()["items"]] == [context.second]


def test_tombstone_is_unavailable_to_every_library_operation_and_evidence(context):
    remove(context, context.first)
    assert remove(context, context.first).status_code == 404
    assert context.client.get(f"/api/knowledge-bases/{context.first}/videos").status_code == 404
    assert context.client.patch(f"/api/knowledge-bases/{context.first}", json={"name": "复活"}).status_code == 404
    for action in (lambda: service.require_base(context.db, context.first),
                   lambda: service.get(context.db, context.first),
                   lambda: service.assign_videos(context.db, context.first, ["shared"]),
                   lambda: service.list_video_ids(context.db, context.first),
                   lambda: service.remove_video(context.db, context.first, "shared")):
        with pytest.raises(KnowledgeBaseNotFound):
            action()
    tools = LocalKnowledgeTools(context.path, context.first, rag=SimpleNamespace())
    with pytest.raises(KnowledgeBaseNotFound):
        tools.describe()
    with pytest.raises(KnowledgeBaseNotFound):
        tools.query("合成问题，不允许回退全部资料")


def test_same_name_recreation_never_reuses_deleted_max_id_or_grant(context, tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3", database_path=context.path)
    binding = Binding("old-library-binding", "old-library-instance",
        "https://www.douyin.com/user/synthetic-A", "https://www.douyin.com/user/synthetic-B",
        context.second, True, True)
    registry.import_legacy(binding, verified_by="synthetic-owner", allow_import=True)
    assert registry.authorize_binding(binding.binding_id, binding.instance_id, context.second) is not None
    before = registry.path.read_bytes()
    assert remove(context, context.second).status_code == 200
    assert registry.path.read_bytes() == before  # Do not edit grants or assistant task state.
    assert registry.authorize_binding(binding.binding_id, binding.instance_id, context.second) is None
    new = service.create(context.db, "其他资料"); context.db.commit()
    assert new["id"] > context.second
    assert registry.authorize_binding(binding.binding_id, binding.instance_id, new["id"]) is None
    assert [item["id"] for item in read_local_catalog(context.path)] == [context.first, new["id"]]
    assert remove(context, new["id"]).status_code == 200
    newer = service.create(context.db, "其他资料"); context.db.commit()
    assert newer["id"] > new["id"]


def test_collection_default_name_creates_new_scope_instead_of_reusing_tombstone(context):
    folder = FavoriteCollection(platform_collection_id="synthetic-folder", title="阅读资料")
    context.db.add(folder); context.db.flush()
    context.db.add(FavoriteVideo(collection_id=folder.id, platform_item_id="favorite-only", title="合成收藏"))
    context.db.commit(); remove(context, context.first)
    new, reused = service.from_collection(context.db, "synthetic-folder"); context.db.commit()
    assert not reused and new["id"] > context.first and new["name"] == "阅读资料"
    again, reused = service.from_collection(context.db, "synthetic-folder")
    assert reused and again["id"] == new["id"]


def test_delete_is_transactional_and_stale_session_cannot_reuse_deleted_row(context):
    service.delete(context.db, context.first); context.db.rollback()
    assert service.list_video_ids(context.db, context.first) == ["shared"]
    with Session(context.engine) as stale:
        old = stale.get(KnowledgeBase, context.first)
        assert old.deleted_at is None
        remove(context, context.first)
        with pytest.raises(KnowledgeBaseNotFound):
            service.require_base(stale, context.first)


@pytest.mark.parametrize("busy", ["queued", "submitting", "maintenance"])
def test_busy_worker_rejects_delete_without_changing_library(context, busy):
    if busy == "queued":
        context.worker.submit("synthetic-never-run", lambda **_: pytest.fail("worker must not run"))
        response = remove(context, context.first)
    else:
        guard = context.worker.submission_guard if busy == "submitting" else context.worker.maintenance_guard
        with guard():
            response = remove(context, context.first)
    assert response.status_code == 409
    assert service.list_video_ids(context.db, context.first) == ["shared"]
    assert context.db.get(KnowledgeBase, context.first).deleted_at is None


def test_delete_requires_local_origin_json_positive_id_and_no_scope_body(context):
    with TestClient(context.app, base_url="http://127.0.0.1", client=("203.0.113.7", 50000)) as remote:
        assert remote.request("DELETE", f"/api/knowledge-bases/{context.first}", json={}).status_code == 403
    assert context.client.request("DELETE", f"/api/knowledge-bases/{context.first}",
        headers={"origin": "https://outside.invalid"}, json={}).status_code == 403
    assert context.client.request("DELETE", f"/api/knowledge-bases/{context.first}",
        headers={"content-type": "text/plain"}, content="{}").status_code == 415
    assert remove(context, 0).status_code == 422
    assert remove(context, -1).status_code == 422
    assert context.client.request("DELETE", f"/api/knowledge-bases/{context.first}",
        json={"knowledge_base_id": context.second, "query_scope": "all_local"}).status_code == 422
    assert service.list_video_ids(context.db, context.first) == ["shared"]
    with pytest.raises(ValueError, match="无效字符"):
        service.create(context.db, "\x00deleted:1")


def test_old_database_migration_is_idempotent_preserves_rows_and_blocks_old_writers(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE knowledge_bases(id INTEGER PRIMARY KEY,name TEXT,name_key TEXT UNIQUE)"))
        db.execute(text("CREATE TABLE knowledge_base_videos(knowledge_base_id INTEGER,platform_item_id TEXT)"))
        db.execute(text("INSERT INTO knowledge_bases VALUES(8,'合成旧库','合成旧库'),(9,'合成另一个库','合成另一个库')"))
        db.execute(text("INSERT INTO knowledge_base_videos VALUES(9,'shared')"))
    ensure_knowledge_base_tombstones(engine); ensure_knowledge_base_tombstones(engine)
    assert [column["name"] for column in inspect(engine).get_columns("knowledge_bases")].count("deleted_at") == 1
    with engine.begin() as db:
        assert db.execute(text("SELECT name,deleted_at FROM knowledge_bases WHERE id=8")).one() == ("合成旧库", None)
        db.execute(text("UPDATE knowledge_bases SET deleted_at='2026-01-01' WHERE id=8"))
    for statement in ("INSERT INTO knowledge_base_videos VALUES(8,'new')",
                      "UPDATE knowledge_base_videos SET knowledge_base_id=8 WHERE knowledge_base_id=9"):
        with pytest.raises(IntegrityError, match="knowledge_base_deleted"):
            with engine.begin() as db:
                db.execute(text(statement))
    with engine.connect() as db:
        assert db.execute(text("SELECT * FROM knowledge_base_videos")).all() == [(9, "shared")]
    engine.dispose()


@pytest.mark.parametrize("busy", ["queued", "submitting", "maintenance"])
def test_global_video_delete_refuses_busy_worker_before_shared_deletion(context, monkeypatch, busy):
    monkeypatch.setattr(video_routes.knowledge_service, "delete_video",
        lambda *_: pytest.fail("busy delete must not touch SQL, Chroma, transcripts or files"))
    if busy == "queued":
        context.worker.submit("synthetic-queued-video", lambda **_: pytest.fail("worker must not run"))
        response = context.client.delete("/api/knowledge/videos/shared")
    else:
        guard = context.worker.submission_guard if busy == "submitting" else context.worker.maintenance_guard
        with guard():
            response = context.client.delete("/api/knowledge/videos/shared")
    assert response.status_code == 200 and response.json()["success"] is False
    assert context.db.scalar(select(VideoCache.transcript_text)) == "合成完整原文"
    assert service.list_video_ids(context.db, context.first) == ["shared"]


@pytest.mark.parametrize("outcome", ["success", "validation", "unknown"])
def test_global_video_delete_keeps_admission_through_commit_or_rollback(context, monkeypatch, outcome):
    rolled_back = []
    original_rollback = context.db.rollback
    def rollback():
        with pytest.raises(WorkerBusyError):
            with context.worker.submission_guard():
                pytest.fail("rollback must finish before deletion admission is released")
        rolled_back.append(True)
        original_rollback()
    def delete_video(db, video_id):
        with pytest.raises(WorkerBusyError):
            with context.worker.submission_guard():
                pytest.fail("no new import may begin during global deletion")
        if outcome == "validation":
            raise ValueError("合成视频不可删除")
        if outcome == "unknown":
            raise RuntimeError("synthetic unknown deletion result")
        db.commit()
        return {"platform_item_id": video_id, "status": "pending"}
    monkeypatch.setattr(context.db, "rollback", rollback)
    monkeypatch.setattr(video_routes.knowledge_service, "delete_video", delete_video)
    if outcome == "unknown":
        with pytest.raises(RuntimeError, match="synthetic unknown deletion result"):
            context.client.delete("/api/knowledge/videos/shared")
    else:
        assert context.client.delete("/api/knowledge/videos/shared").json()["success"] is (outcome == "success")
    assert bool(rolled_back) is (outcome != "success")
    monkeypatch.setattr(context.db, "rollback", original_rollback)
    with context.worker.submission_guard():
        pass


@pytest.mark.parametrize("failure", ["validation", "commit"])
def test_library_delete_rolls_back_before_releasing_admission(context, monkeypatch, failure):
    rolled_back = []
    original_rollback, original_commit = context.db.rollback, context.db.commit
    def rollback():
        with pytest.raises(WorkerBusyError):
            with context.worker.submission_guard():
                pytest.fail("library rollback must keep deletion admission")
        rolled_back.append(True)
        original_rollback()
    monkeypatch.setattr(context.db, "rollback", rollback)
    if failure == "validation":
        def invalid(*_):
            raise ValueError("合成删除校验失败")
        monkeypatch.setattr(service, "delete", invalid)
        assert remove(context, context.first).status_code == 400
    else:
        def failed_commit():
            raise RuntimeError("synthetic library commit failure")
        monkeypatch.setattr(context.db, "commit", failed_commit)
        with pytest.raises(RuntimeError, match="synthetic library commit failure"):
            remove(context, context.first)
    assert rolled_back == [True]
    assert service.list_video_ids(context.db, context.first) == ["shared"]
    assert context.db.get(KnowledgeBase, context.first).deleted_at is None
    monkeypatch.setattr(context.db, "rollback", original_rollback)
    monkeypatch.setattr(context.db, "commit", original_commit)
    with context.worker.submission_guard():
        pass
