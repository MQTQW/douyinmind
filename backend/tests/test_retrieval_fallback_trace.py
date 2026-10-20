"""A fallback's mode and reason belong to one request, including empty results."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services import rag_service as module
from app.services.retrieval_errors import EmbeddingUnavailable


@pytest.mark.parametrize("streaming", [False, True])
def test_fallback_is_explicit_and_does_not_claim_vector_similarity(tmp_path, monkeypatch, streaming):
    monkeypatch.setattr(settings, "retrieval_hybrid_enabled", True)
    doc = {"chunk_id": "local:1", "platform_item_id": "local", "chunk_index": 1,
           "title": "检修", "text": "语音文字稿（来自视频声音）：\n[00:00–00:05] 检修先断电，再确认电源已关闭。", "score": 0.0}
    service = module.RagService()
    def timeout(_):
        exc = EmbeddingUnavailable("timeout")
        exc.args = ("SYNTHETIC_UPSTREAM_PRIVATE_STRING",)
        raise exc
    monkeypatch.setattr(module.embedding_client, "embed_text", timeout)
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        count=lambda: 1, get_documents=lambda **kwargs: [doc],
        search=lambda *args, **kwargs: pytest.fail("Fallback must not call vector index search")))
    monkeypatch.setattr(module.llm_client, "chat", lambda **kwargs: "先断电，再确认电源关闭。[来源: 资料1]")
    monkeypatch.setattr(module.llm_client, "stream_chat", lambda **kwargs: iter(["先断电，再确认电源关闭。[来源: 资料1]"]))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(KnowledgeBase(id=1, name="范围", name_key="范围"))
        db.add(VideoCache(platform_item_id="local", title="检修", status="done"))
        db.add(KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="local"))
        db.commit()
        if streaming:
            events = list(service.answer_stream(db, "检修前怎样断电？", None, knowledge_base_id=1))
            assert events[-1] == ("done", {"ok": True})
            result = next(data for event, data in events if event == "meta")
        else:
            result = service.answer(db, "检修前怎样断电？", None, knowledge_base_id=1)
    engine.dispose()
    assert result["trace"]["retrieval_mode"] == "lexical_fallback"
    assert result["trace"]["retrieval_fallback_reason"] == "timeout"
    assert result["trace"]["chunks"][0]["score_kind"] == "lexical"
    assert result["sources"][0]["score"] is None
    assert "SYNTHETIC_UPSTREAM_PRIVATE_STRING" not in str(result)


def test_two_calls_do_not_share_fallback_diagnostics(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_hybrid_enabled", True)
    doc = {"chunk_id": "local:1", "platform_item_id": "local", "chunk_index": 1,
           "title": "合成手册", "text": "关闭电源后进行检修。", "score": .8}
    def embed(query):
        if query == "故障时检修电源":
            raise EmbeddingUnavailable("connection")
        return [.1, .2]
    monkeypatch.setattr(module.embedding_client, "embed_text", embed)
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        get_documents=lambda **kwargs: [doc], search=lambda *args, **kwargs: [doc]))
    service = module.RagService()
    first = service._dense_retrieve("故障时检修电源", {"local"})
    second = service._dense_retrieve("正常时检修电源", {"local"})
    assert first.mode == "lexical_fallback" and first.fallback_reason == "connection"
    assert second.mode == "hybrid" and second.fallback_reason is None
    assert first.mode == "lexical_fallback", "A later success must not erase a past call's diagnostic"
