"""Destination imports and retrieval isolation using in-memory data and fake AI."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import ChatSession, KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services import knowledge_service as knowledge_module
from app.services import rag_service as rag_module


@pytest.fixture
def context(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        first, second, empty = [KnowledgeBase(name=name, name_key=name.casefold())
                                for name in ["技术", "运动", "空库"]]
        db.add_all([first, second, empty])
        db.flush()
        for item_id, status in [("first", "done"), ("second", "done"),
                                ("pending", "pending"), ("failed", "failed")]:
            db.add(VideoCache(platform_item_id=item_id, title=f"TITLE_{item_id}", status=status,
                              transcript_text=f"CONTENT_{item_id}"))
        db.flush()
        db.add_all([
            KnowledgeBaseVideo(knowledge_base_id=first.id, platform_item_id="first"),
            KnowledgeBaseVideo(knowledge_base_id=first.id, platform_item_id="pending"),
            KnowledgeBaseVideo(knowledge_base_id=second.id, platform_item_id="second"),
        ])
        db.commit()
        submissions, checks, prompts, retrievals = [], [], [], []
        monkeypatch.setattr(knowledge_module.worker, "submit", lambda *args: submissions.append(args))
        monkeypatch.setattr(knowledge_module.omni_service, "check_configuration", lambda: checks.append(True))
        def search(vector, **kwargs):
            retrievals.append(kwargs)
            # Return an out-of-scope hit as well; defense in depth must discard it.
            return [dict(platform_item_id=item, title=f"TITLE_{item}", text=f"CONTENT_{item}",
                         score=0.9, chunk_id=f"{item}:0") for item in ["first", "second", "pending"]]
        monkeypatch.setattr(rag_module, "get_chroma_service",
                            lambda: SimpleNamespace(count=lambda: 3, search=search))
        monkeypatch.setattr(rag_module.embedding_client, "embed_text", lambda text: [1.0, 0.0])
        def answer(**kwargs):
            prompts.append(kwargs)
            return "已根据资料回答。"
        monkeypatch.setattr(rag_module.llm_client, "chat", answer)
        monkeypatch.setattr(rag_module.llm_client, "stream_chat", lambda **kwargs: iter([answer(**kwargs)]))
        yield SimpleNamespace(db=db, first=first, second=second, empty=empty,
                              submissions=submissions, checks=checks, prompts=prompts,
                              retrievals=retrievals)
    engine.dispose()


def memberships(db, kb_id):
    return list(db.scalars(select(KnowledgeBaseVideo.platform_item_id).where(
        KnowledgeBaseVideo.knowledge_base_id == kb_id).order_by(KnowledgeBaseVideo.platform_item_id)))


def test_completed_import_only_assigns_and_reuses_transcript(context):
    result = knowledge_module.knowledge_service.start_sync(
        context.db, ["first", "first"], context.second.id)
    assert result["assigned_count"] == 1
    assert result["existing_count"] == 0
    assert result["task_id"] is None
    assert context.checks == context.submissions == []
    assert memberships(context.db, context.second.id) == ["first", "second"]
    again = knowledge_module.knowledge_service.start_sync(context.db, ["first"], context.second.id)
    assert again["assigned_count"] == 0 and again["existing_count"] == 1
    assert "已在该库" in again["message"]
    assert context.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "first")).transcript_text == "CONTENT_first"


def test_queued_import_reuses_video_finished_by_an_earlier_task(context, monkeypatch):
    monkeypatch.setattr(knowledge_module, "get_chroma_service", lambda: SimpleNamespace())
    def unexpected(*args, **kwargs):
        raise AssertionError("已经完成的视频不能再次识别")
    monkeypatch.setattr(knowledge_module, "download_audio", unexpected)
    monkeypatch.setattr(knowledge_module, "download_video", unexpected)
    monkeypatch.setattr(knowledge_module.omni_service, "analyze_audio", unexpected)
    monkeypatch.setattr(knowledge_module.omni_service, "analyze_video", unexpected)
    knowledge_module.knowledge_service._process_videos(context.db, ["first"], "duplicate-job")
    assert context.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == "first")).status == "done"


def test_pending_and_failed_import_assigns_only_selection_and_schedules_analysis(context):
    result = knowledge_module.knowledge_service.start_sync(
        context.db, ["first", "pending", "failed"], context.second.id)
    assert result["assigned_count"] == 3
    assert result["pending_count"] == 2
    assert context.submissions[0][2] == ["pending", "failed"]
    assert memberships(context.db, context.second.id) == ["failed", "first", "pending", "second"]


def test_configuration_failure_does_not_partially_assign_selection(context, monkeypatch):
    def fail():
        raise RuntimeError("识别服务未配置")
    monkeypatch.setattr(knowledge_module.omni_service, "check_configuration", fail)
    with pytest.raises(RuntimeError, match="未配置"):
        knowledge_module.knowledge_service.start_sync(context.db, ["first", "pending"], context.second.id)
    assert memberships(context.db, context.second.id) == ["second"]
    assert context.submissions == []


@pytest.mark.parametrize("ids,kb", [(["first", "unknown"], "second"),
                                    ([], "second"), (None, "second"), (["first"], "missing")])
def test_invalid_import_rejects_entire_request_without_classifying(context, ids, kb):
    before = memberships(context.db, context.second.id)
    with pytest.raises(ValueError):
        knowledge_module.knowledge_service.start_sync(
            context.db, ids, context.second.id if kb == "second" else 9999)
    assert memberships(context.db, context.second.id) == before
    assert context.checks == context.submissions == []


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("query", ["有哪些视频", "总结一下", "解释视频里的技巧"])
def test_every_answer_route_uses_only_ready_video_members(context, streaming, query):
    service = rag_module.RagService()
    if streaming:
        events = list(service.answer_stream(context.db, query, None, knowledge_base_id=context.first.id))
        result = next(payload for name, payload in events if name == "meta")
        assert events[-1] == ("done", {"ok": True})
    else:
        result = service.answer(context.db, query, None, knowledge_base_id=context.first.id)
    sent = "\n".join(prompt["user_prompt"] for prompt in context.prompts)
    assert "TITLE_first" in sent
    assert "CONTENT_second" not in sent and "TITLE_second" not in sent
    assert "CONTENT_pending" not in sent and "TITLE_pending" not in sent
    assert all(source["platform_item_id"] == "first" for source in result["sources"])
    assert all(retrieval["allowed_video_ids"] == {"first"} for retrieval in context.retrievals)
    session = context.db.get(ChatSession, result["session_id"])
    assert session.knowledge_base_id == context.first.id


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("query", ["有哪些视频", "总结一下", "解释视频里的技巧"])
def test_empty_library_never_falls_back_to_other_library(context, streaming, query):
    service = rag_module.RagService()
    if streaming:
        events = list(service.answer_stream(context.db, query, None, knowledge_base_id=context.empty.id))
        result = next(payload for name, payload in events if name == "meta")
    else:
        result = service.answer(context.db, query, None, knowledge_base_id=context.empty.id)
    assert result["sources"] == []
    assert context.retrievals == []
    sent = str(context.prompts)
    assert "TITLE_first" not in sent and "CONTENT_second" not in sent
    assert "暂无已入库资料" in sent


def test_history_cannot_bring_another_library_into_current_answer(context):
    service = rag_module.RagService()
    old = ChatSession(title="旧库对话", knowledge_base_id=context.second.id)
    context.db.add(old)
    context.db.commit()
    with pytest.raises(ValueError, match="另一个知识库"):
        service.answer(context.db, "继续总结", old.id, knowledge_base_id=context.first.id)
    events = list(service.answer_stream(context.db, "继续总结", old.id, knowledge_base_id=context.first.id))
    assert events == [("error", {"code": "CHAT_SCOPE_MISMATCH", "message": "这段历史对话属于另一个知识库，请切回原知识库或新建对话。"})]
    assert context.prompts == context.retrievals == []


def test_history_keeps_the_original_library_when_continued(context):
    service = rag_module.RagService()
    result = service.answer(context.db, "解释视频里的技巧", None, knowledge_base_id=context.first.id)
    followup = service.answer(context.db, "继续解释", result["session_id"], knowledge_base_id=context.first.id)
    assert followup["session_id"] == result["session_id"]
    assert service.list_sessions(context.db)[0]["knowledge_base_id"] == context.first.id
    assert "CONTENT_second" not in str(context.prompts)


def test_shared_video_deletion_removes_every_membership(context, monkeypatch, tmp_path):
    knowledge_module.knowledge_service.start_sync(context.db, ["first"], context.second.id)
    deleted = []
    monkeypatch.setattr(knowledge_module, "get_chroma_service",
                        lambda: SimpleNamespace(delete_by_video=lambda item: deleted.append(item)))
    monkeypatch.setattr(knowledge_module.settings, "audio_cache_dir", str(tmp_path / "audio"))
    monkeypatch.setattr(knowledge_module.settings, "video_cache_dir", str(tmp_path / "video"))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    retained_progress = audio_dir / "first.wav.analysis.json"
    retained_progress.write_text('{"private_transcript": "saved recognition"}', encoding="utf-8")
    other_progress = audio_dir / "second.wav.analysis.json"
    other_progress.write_text("other video", encoding="utf-8")
    knowledge_module.knowledge_service.delete_video(context.db, "first")
    assert memberships(context.db, context.first.id) == ["pending"]
    assert memberships(context.db, context.second.id) == ["second"]
    assert deleted == ["first"]
    assert not retained_progress.exists()
    assert other_progress.exists()


def test_chroma_query_applies_scope_before_retrieving_candidates():
    from app.services.chroma_service import ChromaService
    calls = []
    service = ChromaService.__new__(ChromaService)
    service._collection = SimpleNamespace(count=lambda: 3,
        query=lambda **kwargs: calls.append(kwargs) or {"ids": [[]], "metadatas": [[]]})
    assert service.search([1.0], allowed_video_ids={"second", "first"}) == []
    assert calls[0]["where"] == {"platform_item_id": {"$in": ["first", "second"]}}
    calls.clear()
    assert service.search([1.0], allowed_video_ids=set()) == []
    assert calls == []
