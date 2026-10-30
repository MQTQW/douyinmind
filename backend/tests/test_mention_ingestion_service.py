"""Temporary SQL/receipt DBs, fake original worker/pipeline/index; no media/API."""
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes import mention_ingestion as routes
from app.db.base import Base
from app.models.entities import FavoriteVideo, KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services.mention_ingestion_service import MentionIngestionError, MentionIngestionService


VIDEO = "7529030669884853530"
OTHER = "7529030669884853531"
FIELDS = {"request_id", "binding_id", "instance_id", "video_id", "knowledge_base_id",
          "state", "membership_exists", "cache_status", "indexed_content_chunks"}


class FakeWorker:
    def __init__(self):
        self._running = True
        self.jobs, self.progress = [], {}
        self.immediate = False
        self.fail_publication = False

    def start(self):
        raise AssertionError("the import service must never start a worker")

    def submit(self, task_id, function, *args):
        self.jobs.append((task_id, function, args))
        self.progress[task_id] = {"status": "queued"}
        if self.immediate:
            self.run(task_id)
        if self.fail_publication:
            raise RuntimeError("fixture-publication-response-lost")

    def get_progress(self, task_id):
        return self.progress.get(task_id)

    def run(self, task_id):
        _, function, args = next(job for job in self.jobs if job[0] == task_id)
        self.progress[task_id] = {"status": "running"}
        function(*args, task_id=task_id)
        self.progress[task_id] = {"status": "done"}


@pytest.fixture
def context(tmp_path):
    database = tmp_path / "business.sqlite3"
    engine = create_engine("sqlite:///" + str(database))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as db:
        library = KnowledgeBase(name="临时库", name_key="临时库")
        db.add(library)
        db.commit()
        library_id = library.id
    binding = {"binding_id": "binding-fixture", "instance_id": "instance-fixture",
        "sender_profile_url": "https://www.douyin.com/user/fixture-A",
        "receiver_profile_url": "https://www.douyin.com/user/fixture-B",
        "knowledge_base_id": library_id, "active": True, "public_reply_allowed": True}
    binding_path = tmp_path / "local-binding.json"
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    worker, documents, calls = FakeWorker(), {}, []

    def pipeline(ids, *, task_id):
        calls.append((ids, task_id))
        with sessions() as db:
            cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == ids[0]))
            cache.status, cache.transcript_text = "done", "fixture speech, not real media"
            db.commit()
        documents[ids[0]] = [
            {"platform_item_id": ids[0], "text": "视频标题：临时标题"},
            {"platform_item_id": ids[0], "text": "fixture content chunk"}]

    arguments = dict(sessions=sessions, worker=worker, pipeline=pipeline,
        index_reader=lambda video_id: documents.get(video_id, []),
        binding_path=binding_path, receipt_path=tmp_path / "backend-ingestion.sqlite3",
        protected_paths=(database, tmp_path / "agent.sqlite3"))
    service = MentionIngestionService(**arguments)
    payload = {"request_id": "import-request-fixture", "binding_id": binding["binding_id"],
               "instance_id": binding["instance_id"], "video_id": VIDEO, "knowledge_base_id": library_id}
    yield SimpleNamespace(database=database, engine=engine, sessions=sessions, worker=worker,
        documents=documents, calls=calls, binding=binding, binding_path=binding_path,
        service=service, arguments=arguments, payload=payload)
    engine.dispose()


def counts(context):
    with context.sessions() as db:
        return tuple(db.scalar(select(func.count()).select_from(model))
                     for model in (VideoCache, KnowledgeBaseVideo, FavoriteVideo))


def test_deleted_library_rejects_new_import_without_cache_membership_or_work(context):
    from app.services.knowledge_base_service import knowledge_base_service
    with context.sessions() as db:
        knowledge_base_service.delete(db, context.payload["knowledge_base_id"])
        db.commit()
    with pytest.raises(MentionIngestionError, match="library_not_found"):
        context.service.submit(context.payload)
    assert counts(context) == (0, 0, 0)
    assert context.worker.jobs == context.calls == []


def test_unknown_deleted_library_receipt_keeps_its_replay_barrier(context):
    from app.services.knowledge_base_service import knowledge_base_service
    context.worker.fail_publication = True
    assert context.service.submit(context.payload)["state"] == "unknown"
    with context.sessions() as db:
        knowledge_base_service.delete(db, context.payload["knowledge_base_id"])
        db.commit()
    assert context.service.get(context.payload["request_id"])["state"] == "unknown"
    assert context.service.submit(context.payload)["state"] == "unknown"
    with pytest.raises(MentionIngestionError, match="video_import_inflight"):
        context.service.submit({**context.payload, "request_id": "synthetic-second-request"})
    assert len(context.worker.jobs) == 1 and context.calls == []
    assert counts(context) == (1, 0, 0)


def test_old_queued_receipt_does_not_run_pipeline_after_library_disappears(context):
    from app.services.knowledge_base_service import knowledge_base_service
    context.service.submit(context.payload)
    with context.sessions() as db:
        knowledge_base_service.delete(db, context.payload["knowledge_base_id"])
        db.commit()
    context.worker.run(context.payload["request_id"])
    assert context.calls == []
    assert context.service.get(context.payload["request_id"])["state"] == "failed"


def test_legacy_library_schema_still_imports_only_its_explicit_scope(context):
    with sqlite3.connect(context.database) as db:
        db.execute("ALTER TABLE knowledge_bases DROP COLUMN deleted_at")
    result = context.service.submit(context.payload)
    assert result["state"] == "accepted" and result["membership_exists"]
    assert len(context.worker.jobs) == 1


def test_worker_maintenance_prevents_receipt_creation_or_database_writes(context):
    from app.services.worker import Worker
    worker = Worker()
    context.service.worker = worker
    before = context.database.read_bytes()
    with worker.maintenance_guard():
        with pytest.raises(MentionIngestionError, match="backend_worker_busy"):
            context.service.submit(context.payload)
    assert context.database.read_bytes() == before
    assert not context.service.receipt_path.exists()
    assert context.calls == []


def test_constructor_and_missing_get_do_not_create_receipts(context):
    assert not context.service.receipt_path.exists()
    with pytest.raises(MentionIngestionError, match="request_not_found"):
        context.service.get(context.payload["request_id"])
    assert not context.service.receipt_path.exists()
    assert context.calls == context.worker.jobs == []


def test_not_running_worker_rejects_without_any_data_side_effect(context):
    context.worker._running = False
    before = hashlib.sha256(context.database.read_bytes()).hexdigest()
    with pytest.raises(MentionIngestionError, match="backend_worker_not_running"):
        context.service.submit(context.payload)
    assert hashlib.sha256(context.database.read_bytes()).hexdigest() == before
    assert not context.service.receipt_path.exists()
    assert counts(context) == (0, 0, 0)


def test_new_video_registers_only_cache_and_library_and_keeps_request_id(context):
    result = context.service.submit(context.payload)
    assert set(result) == FIELDS
    assert result["state"] == "accepted"
    assert result["membership_exists"] and result["cache_status"] == "pending"
    assert result["indexed_content_chunks"] == 0
    assert counts(context) == (1, 1, 0)
    assert context.worker.jobs[0][0] == context.payload["request_id"]
    assert context.calls == []
    with context.sessions() as db:
        assert db.scalar(select(VideoCache)).title == "抖音视频" + VIDEO


def test_same_request_is_idempotent_and_conflicting_request_is_rejected(context):
    first = context.service.submit(context.payload)
    assert context.service.submit(context.payload) == first
    assert len(context.worker.jobs) == 1
    with pytest.raises(MentionIngestionError, match="request_id_conflict"):
        context.service.submit({**context.payload, "video_id": OTHER})
    assert counts(context) == (1, 1, 0)


def test_video_level_barrier_blocks_second_inflight_request(context):
    context.service.submit(context.payload)
    with pytest.raises(MentionIngestionError, match="video_import_inflight"):
        context.service.submit({**context.payload, "request_id": "different-request"})
    assert len(context.worker.jobs) == 1 and counts(context) == (1, 1, 0)


def test_existing_worker_runs_exact_one_video_through_injected_original_pipeline(context):
    context.service.submit(context.payload)
    context.worker.run(context.payload["request_id"])
    result = context.service.get(context.payload["request_id"])
    assert result["state"] == "succeeded"
    assert result["membership_exists"] and result["cache_status"] == "done"
    assert result["indexed_content_chunks"] == 1
    assert context.calls == [([VIDEO], context.payload["request_id"])]
    context.worker.run(context.payload["request_id"])
    assert len(context.calls) == 1
    assert context.service.submit(context.payload)["state"] == "succeeded"
    assert len(context.worker.jobs) == 1


def test_done_video_reuses_material_without_pipeline_or_overwriting_title(context):
    with context.sessions() as db:
        db.add(VideoCache(platform_item_id=VIDEO, title="preserved title", status="done",
                          transcript_text="preserved transcript"))
        db.commit()
    context.documents[VIDEO] = [{"platform_item_id": VIDEO, "text": "real fixture content"}]
    result = context.service.submit({**context.payload, "title": "do not overwrite"})
    assert result["state"] == "succeeded"
    assert context.worker.jobs == context.calls == []
    with context.sessions() as db:
        cache = db.scalar(select(VideoCache))
        assert cache.title == "preserved title" and cache.transcript_text == "preserved transcript"


@pytest.mark.parametrize("status", ["downloading", "transcribing", "embedding"])
def test_existing_processing_is_preserved_without_dispatch(context, status):
    with context.sessions() as db:
        db.add(VideoCache(platform_item_id=VIDEO, title="existing", status=status, transcript_text="keep"))
        db.commit()
    result = context.service.submit(context.payload)
    assert result["state"] == "unknown" and result["cache_status"] == status
    assert result["membership_exists"]
    assert context.worker.jobs == context.calls == []
    with context.sessions() as db:
        assert db.scalar(select(VideoCache)).transcript_text == "keep"


@pytest.mark.parametrize("missing", ["membership", "done", "content"])
def test_success_requires_all_three_evidence_sources(context, missing):
    context.service.submit(context.payload)
    context.worker.run(context.payload["request_id"])
    with context.sessions() as db:
        if missing == "membership":
            db.delete(db.get(KnowledgeBaseVideo, (context.payload["knowledge_base_id"], VIDEO)))
        elif missing == "done":
            db.scalar(select(VideoCache)).status = "embedding"
        db.commit()
    if missing == "content":
        context.documents[VIDEO] = [{"platform_item_id": VIDEO, "text": "视频标题：only metadata"},
                                   {"platform_item_id": OTHER, "text": "wrong video"}]
    result = context.service.get(context.payload["request_id"])
    assert result["state"] == "unknown"
    assert context.worker.get_progress(context.payload["request_id"])["status"] == "done"


def test_restart_reconciles_receipt_but_never_redispatches(context):
    context.service.submit(context.payload)
    restarted_worker = FakeWorker()
    restarted = MentionIngestionService(**{**context.arguments, "worker": restarted_worker})
    result = restarted.submit(context.payload)
    assert result["state"] == "unknown"
    assert restarted_worker.jobs == []
    assert counts(context) == (1, 1, 0)
    context.worker.run(context.payload["request_id"])
    assert context.calls == [], "old queued wrapper must respect the durable unknown fence"


def test_publication_failure_stays_unknown_and_stale_delivered_job_cannot_run(context):
    context.worker.fail_publication = True
    result = context.service.submit(context.payload)
    assert result["state"] == "unknown"
    assert context.service.submit(context.payload)["state"] == "unknown"
    assert len(context.worker.jobs) == 1
    context.worker.run(context.payload["request_id"])
    assert context.calls == []


def test_fast_worker_completion_is_not_overwritten_with_queued(context):
    context.worker.immediate = True
    result = context.service.submit(context.payload)
    assert result["state"] == "succeeded"
    assert context.calls == [([VIDEO], context.payload["request_id"])]


def test_pipeline_error_is_sanitized_and_not_automatically_retried(context):
    def fail(ids, *, task_id):
        with context.sessions() as db:
            db.scalar(select(VideoCache)).status = "failed"
            db.commit()
        raise RuntimeError("fixture-private-provider-error")
    context.service.pipeline = fail
    context.service.submit(context.payload)
    context.worker.run(context.payload["request_id"])
    result = context.service.get(context.payload["request_id"])
    assert result["state"] == "failed"
    assert "fixture-private-provider-error" not in json.dumps(result)
    context.service.submit(context.payload)
    assert len(context.worker.jobs) == 1


@pytest.mark.parametrize("key,value", [("binding_id", "stranger"), ("instance_id", "other-device"),
                                     ("knowledge_base_id", 999)])
def test_backend_binding_rejects_scope_changes_before_registration(context, key, value):
    with pytest.raises(MentionIngestionError, match="binding_scope_mismatch"):
        context.service.submit({**context.payload, key: value})
    assert counts(context) == (0, 0, 0)
    assert not context.service.receipt_path.exists()


def test_revoked_binding_blocks_queued_pipeline(context):
    context.service.submit(context.payload)
    context.binding_path.write_text(json.dumps({**context.binding, "active": False}), encoding="utf-8")
    context.worker.run(context.payload["request_id"])
    assert context.calls == []
    with pytest.raises(MentionIngestionError, match="binding_inactive"):
        context.service.get(context.payload["request_id"])


def test_agentstore_or_foreign_database_is_not_reused_as_receipts(context):
    with sqlite3.connect(context.service.receipt_path) as db:
        db.execute("CREATE TABLE agent_tasks(private_data TEXT)")
        db.execute("INSERT INTO agent_tasks VALUES('preserve')")
    before = context.service.receipt_path.read_bytes()
    with pytest.raises(MentionIngestionError, match="foreign_receipt_database"):
        context.service.submit(context.payload)
    assert context.service.receipt_path.read_bytes() == before
    assert counts(context) == (0, 0, 0)


def test_api_loopback_and_exact_body_contract_without_default_service(context, monkeypatch):
    monkeypatch.setattr(routes, "build_default_service",
                        lambda: pytest.fail("tests must not touch the real default backend dependencies"))
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.get_service] = lambda: context.service
    with TestClient(app, client=("203.0.113.10", 50000)) as client:
        result = client.post("/api/mention-ingestion", json=context.payload)
        assert result.status_code == 403
        assert result.json() == {"detail": "loopback_required"}
    assert not context.service.receipt_path.exists()
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/mention-ingestion", json={**context.payload, "url": "https://elsewhere.invalid"}).status_code == 422
        result = client.post("/api/mention-ingestion", json=context.payload)
        assert result.status_code == 200 and set(result.json()) == FIELDS
        assert client.get("/api/mention-ingestion/" + context.payload["request_id"]).json()["state"] == "accepted"


def test_processing_receipt_without_live_worker_job_is_unknown_not_retried(context):
    context.service.submit(context.payload)
    context.service._state(context.payload["request_id"], "processing")
    context.worker.progress[context.payload["request_id"]] = {"status": "failed"}
    assert context.service.get(context.payload["request_id"])["state"] == "unknown"
    assert len(context.worker.jobs) == 1 and context.calls == []


def test_deleted_cache_reports_explicit_missing_string(context):
    context.service.submit(context.payload)
    with context.sessions() as db:
        db.delete(db.scalar(select(VideoCache)))
        db.commit()
    result = context.service.get(context.payload["request_id"])
    assert result["cache_status"] == "missing"
    assert result["state"] != "succeeded"
    assert result["indexed_content_chunks"] == 0


@pytest.mark.parametrize("identity", [{"chunk_index": 0}, {"chunk_index": "0"},
                                     {"chunk_id": VIDEO + ":0"}])
def test_only_metadata_zero_cannot_prove_success_but_real_content_can(context, identity):
    context.service.submit(context.payload)
    context.worker.run(context.payload["request_id"])
    context.documents[VIDEO] = [{"platform_item_id": VIDEO, "text": "另一种格式的标题和作者", **identity}]
    result = context.service.get(context.payload["request_id"])
    assert result["state"] == "unknown"
    assert result["cache_status"] == "done" and result["membership_exists"]
    assert result["indexed_content_chunks"] == 0
    context.documents[VIDEO].append({"platform_item_id": VIDEO, "text": "真实内容形状的临时原文",
                                     "chunk_index": 1, "chunk_id": VIDEO + ":1"})
    result = context.service.get(context.payload["request_id"])
    assert result["state"] == "succeeded" and result["indexed_content_chunks"] == 1
    assert len(context.worker.jobs) == 1
