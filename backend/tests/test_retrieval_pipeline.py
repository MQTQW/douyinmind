"""Offline integration checks for scoped hybrid retrieval and source provenance."""
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services import rag_service as rag_module
from app.services.chroma_service import ChromaService


def document(identifier, body, *, video_id="local", index=1):
    return {"chunk_id": identifier, "platform_item_id": video_id,
            "chunk_index": index, "title": "设备检修记录",
            "text": "语音文字稿（来自视频声音）：\n" + body, "score": .5}


def test_empty_document_scope_never_reads_index():
    service = ChromaService.__new__(ChromaService)
    # Deliberately no _collection: an empty library must short circuit.
    assert service.get_documents(set()) == []


def test_document_lookup_pushes_scope_into_index_and_rechecks_returned_rows():
    calls = []
    service = ChromaService.__new__(ChromaService)
    def get(**kwargs):
        calls.append(kwargs)
        return {"ids": ["local:1", "foreign:1"],
                "metadatas": [{"platform_item_id": "local", "title": "记录", "chunk_index": 1},
                              {"platform_item_id": "foreign", "title": "另库", "chunk_index": 1}],
                "documents": ["范围内原文。", "不得进入当前库的原文。"]}
    service._collection = SimpleNamespace(get=get)
    rows = service.get_documents({"local"})
    assert [r["chunk_id"] for r in rows] == ["local:1"]
    assert rows[0]["chunk_index"] == 1
    assert calls[0]["where"] == {"platform_item_id": {"$in": ["local"]}}
    assert calls[0]["include"] == ["metadatas", "documents"]


def test_document_lookup_paginates_without_changing_or_embedding_records():
    service = ChromaService.__new__(ChromaService)
    offsets = []
    def get(**kwargs):
        offsets.append(kwargs["offset"])
        indices = list(range(kwargs["offset"], min(501, kwargs["offset"] + kwargs["limit"])))
        return {"ids": [f"local:{i}" for i in indices],
                "metadatas": [{"platform_item_id": "local", "chunk_index": i} for i in indices],
                "documents": [f"原始文字{i}" for i in indices]}
    service._collection = SimpleNamespace(get=get)
    rows = service.get_documents({"local"})
    assert len(rows) == 501 and offsets == [0, 500]
    assert rows[-1]["text"] == "原始文字500"


def test_hybrid_retrieval_can_find_lexical_evidence_missing_from_dense_candidates(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_hybrid_enabled", True)
    monkeypatch.setattr(settings, "retrieval_candidate_k", 32)
    monkeypatch.setattr(settings, "retrieval_neighbor_chars", 350)
    wanted = document("local:2", "[00:10–00:20] 检修前必须关闭主电源，并验电确认已经断电。", index=2)
    background = document("local:1", "[00:00–00:10] 检修常见工具包括螺丝刀与绝缘手套。")
    foreign = document("foreign:2", "[00:10–00:20] FOREIGN_SECRET 检修先执行另一库操作。", video_id="foreign", index=2)
    calls = []
    def search(vector, **kwargs):
        calls.append(kwargs)
        return [background, foreign]
    def get_documents(**kwargs):
        assert kwargs["allowed_video_ids"] == {"local"}
        # A buggy dependency still must not make another library's text visible.
        return [background, wanted, foreign]
    monkeypatch.setattr(rag_module, "get_chroma_service", lambda: SimpleNamespace(search=search, get_documents=get_documents))
    monkeypatch.setattr(rag_module.embedding_client, "embed_text", lambda query: [.1, .2])
    hits = rag_module.RagService()._dense_retrieve("检修前必须怎样断电和验电？", {"local"})
    assert wanted["chunk_id"] in {h["chunk_id"] for h in hits}
    assert all(h["platform_item_id"] == "local" for h in hits)
    assert not any("FOREIGN_SECRET" in h["text"] for h in hits)
    assert calls[0]["top_k"] == 32 and calls[0]["use_mmr"] is False
    assert calls[0]["allowed_video_ids"] == {"local"}
    assert any(h.get("retrieval_scores", {}).get("lexical_rank") for h in hits)


def test_empty_hybrid_scope_does_not_embed_or_read_any_source(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An empty library must not search global documents")
    monkeypatch.setattr(rag_module, "get_chroma_service", forbidden)
    monkeypatch.setattr(rag_module.embedding_client, "embed_text", forbidden)
    assert rag_module.RagService()._dense_retrieve("未入库资料中的参数是多少？", set()) == []


def test_saved_cited_chunk_ids_include_original_neighbors_only_for_cited_videos():
    hits = [document("local:2", "完整原文。", index=2) | {"expanded_from_chunk_ids": ["local:1", "local:2", "local:3"]},
            document("uncited:2", "没引用的原文。", video_id="uncited", index=2) | {"expanded_from_chunk_ids": ["uncited:1"]}]
    assert rag_module.RagService()._context_chunk_ids(hits, ["local"]) == ["local:2", "local:1", "local:3"]
