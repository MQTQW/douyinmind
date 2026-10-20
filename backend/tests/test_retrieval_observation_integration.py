"""Offline production-path checks for truthful, request-local retrieval diagnostics.

Use the same in-memory scope/LLM/index boundary as the existing fallback trace
tests. Real routing, fusion, neighbor expansion, prompt packing and trace
construction run; only external clients and the monotonic clock are replaced.
"""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services import rag_service as module
from app.services.retrieval_errors import EmbeddingUnavailable


def document(identifier, body, *, video="local", index=1):
    return {"chunk_id": identifier, "platform_item_id": video, "chunk_index": index,
            "title": f"合成检修手册-{video}", "score": .7,
            "text": "语音文字稿（来自视频声音）：\n" + body}


@pytest.fixture
def offline_retrieval(monkeypatch):
    monkeypatch.setattr(settings, "retrieval_hybrid_enabled", True)
    monkeypatch.setattr(settings, "retrieval_top_k", 8)
    monkeypatch.setattr(settings, "retrieval_candidate_k", 32)
    monkeypatch.setattr(settings, "retrieval_neighbor_chars", 350)
    state = SimpleNamespace(
        now=0.0, outage=False, operations=[], embeddings=[], searches=[], reads=[],
        dense=[], documents=[],
    )
    monkeypatch.setattr("app.services.retrieval_observation.time.perf_counter", lambda: state.now)

    def embed(query):
        state.operations.append("embedding")
        state.embeddings.append(query)
        state.now += 5 if state.outage else .075
        if state.outage:
            error = EmbeddingUnavailable("timeout")
            error.args = ("PRIVATE_PROVIDER_ERROR_WITH_QUERY",)
            raise error
        return [.1, .2]

    def search(vector, **kwargs):
        assert vector == [.1, .2]
        state.operations.append("vector")
        state.searches.append(kwargs)
        state.now += .010
        return deepcopy(state.dense)

    def read_documents(**kwargs):
        state.operations.append("documents")
        state.reads.append(kwargs)
        state.now += .003
        return deepcopy(state.documents)

    fusion = module.rerank_chunks
    expansion = module.expand_hit_context

    def timed_fusion(*args, **kwargs):
        state.operations.append("fusion")
        state.now += .002
        return fusion(*args, **kwargs)

    def timed_expansion(*args, **kwargs):
        state.operations.append("expansion")
        state.now += .001
        return expansion(*args, **kwargs)

    monkeypatch.setattr(module.embedding_client, "embed_text", embed)
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        search=search, get_documents=read_documents, count=lambda: len(state.documents)))
    monkeypatch.setattr(module, "rerank_chunks", timed_fusion)
    monkeypatch.setattr(module, "expand_hit_context", timed_expansion)

    def unexpected_llm(**kwargs):
        pytest.fail("Retrieval diagnostics must not add an LLM call")
    monkeypatch.setattr(module.llm_client, "chat", unexpected_llm)
    monkeypatch.setattr(module.llm_client, "stream_chat", unexpected_llm)

    local = document("local:1", "[00:00–00:05] 检修之前先关闭主电源，确认电源已经关闭。")
    second = document("local:2", "[00:05–00:10] 检修关闭电源后，还要验电确认断电再操作。", index=2)
    foreign = document("foreign:1", "FOREIGN_PRIVATE_TEXT 检修先执行另一库的操作。", video="foreign")
    metadata = {**document("local:3", "", index=3), "text": "视频标题：合成检修手册-local\n作者：合成"}
    state.dense = [local, foreign]
    state.documents = [local, second, foreign, metadata]
    return state


@pytest.mark.parametrize("mode", ["hybrid", "semantic", "lexical_fallback", "empty_scope"])
def test_actual_phases_match_work_done_and_failed_embedding_time_is_not_fake_zero(
    offline_retrieval, monkeypatch, mode,
):
    state = offline_retrieval
    state.outage = mode == "lexical_fallback"
    monkeypatch.setattr(settings, "retrieval_hybrid_enabled", mode != "semantic")
    scope = set() if mode == "empty_scope" else {"local"}
    hits = module.RagService()._dense_retrieve("检修前怎样关闭电源并验电？", scope)
    details = hits.diagnostics
    assert hits.mode == mode
    assert details["scope_video_count"] == len(scope)
    assert len(state.embeddings) == (0 if mode == "empty_scope" else 1)
    if mode == "empty_scope":
        assert state.operations == []
        assert details == {"scope_video_count": 0, "stages_ms": {}, "counts": {"final_hits": 0}}
    elif mode == "semantic":
        assert state.operations == ["embedding", "vector"]
        assert details["stages_ms"] == {"query_embedding": 75, "vector_search": 10}
        assert details["counts"] == {"dense_candidates": 1, "final_hits": 1, "expanded_hits": 0}
        assert state.reads == []
    else:
        expected = ["embedding", "documents", "fusion", "expansion"]
        expected_ms = {"query_embedding": 5000, "scope_documents": 3,
                       "keyword_fusion": 2, "neighbor_expansion": 1}
        if mode == "hybrid":
            expected.insert(1, "vector")
            expected_ms.update(query_embedding=75, vector_search=10)
        assert state.operations == expected
        assert details["stages_ms"] == expected_ms
        assert details["counts"] == {"dense_candidates": 0 if state.outage else 1,
                                     "scope_records": 2, "final_hits": 2, "expanded_hits": 0}
        if state.outage:
            assert state.searches == []
            assert "vector_search" not in details["stages_ms"]
            assert hits.fallback_reason == "timeout"
    assert all(hit["platform_item_id"] in scope for hit in hits)
    assert "FOREIGN_PRIVATE_TEXT" not in str(hits)
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(details, allow_nan=False)


def test_observation_does_not_change_fusion_order_text_or_client_call_count(offline_retrieval, monkeypatch):
    state = offline_retrieval
    service = module.RagService()
    query = "检修应该怎样断电和验电？"
    measured = service._dense_retrieve(query, {"local"})
    measured_operations = list(state.operations)
    measured_embeddings = len(state.embeddings)
    state.operations.clear()
    state.embeddings.clear()

    class DiscardObservation:
        def __init__(self, **kwargs):
            pass

        @contextmanager
        def measure(self, name):
            yield

        def set_count(self, *args):
            pass

        def to_dict(self):
            return {}

    monkeypatch.setattr(module, "RetrievalObservation", DiscardObservation)
    unmeasured = service._dense_retrieve(query, {"local"})
    # Same real retrieval algorithm, with observation alone disabled. This
    # catches accidental mutation/reordering without recreating the algorithm.
    assert list(measured) == list(unmeasured)
    assert measured_operations == state.operations
    assert measured_embeddings == len(state.embeddings) == 1
    assert state.embeddings == [query]
    assert all(c["allowed_video_ids"] == {"local"} for c in state.searches + state.reads)


def test_expansion_count_means_hits_that_actually_gained_source_text(offline_retrieval):
    state = offline_retrieval
    body = "".join(f"第{i}句说明检修条件和关闭电源后的安全结果，应保持这句话的完整原文。" for i in range(12))
    state.documents = [document(f"local:{i}", body[start:start + 120].strip(), index=i)
                       for i, start in enumerate(range(0, len(body), 80))]
    state.dense = [state.documents[2]]
    originals = {d["chunk_id"]: d["text"] for d in state.documents}
    hits = module.RagService()._dense_retrieve("检修条件以及关闭电源后的结果？", {"local"}, top_k=1)
    actually_expanded = sum(len(hit["text"]) > len(originals[hit["chunk_id"]]) for hit in hits)
    assert actually_expanded == 1, "The fixture must exercise a real neighbor repair"
    assert hits.diagnostics["counts"]["expanded_hits"] == actually_expanded
    assert hits.diagnostics["counts"]["final_hits"] == len(hits) == 1
    assert hits.diagnostics["counts"]["scope_records"] == len(state.documents)


def test_sequential_requests_keep_global_scope_distinct_and_diagnostics_isolated(offline_retrieval):
    state = offline_retrieval
    service = module.RagService()
    state.outage = True
    first = service._dense_retrieve("PRIVATE_USER_QUESTION 检修如何关闭电源？", {"local"})
    first_details = deepcopy(first.diagnostics)
    state.outage = False
    second = service._dense_retrieve("另一条检修问题怎样验电？", None)
    assert first.diagnostics == first_details
    assert first.diagnostics["stages_ms"]["query_embedding"] == 5000
    assert second.diagnostics["stages_ms"]["query_embedding"] == 75
    assert first.diagnostics["scope_video_count"] == 1
    assert second.diagnostics["scope_video_count"] is None
    second.diagnostics["counts"]["final_hits"] = 99
    assert first.diagnostics == first_details
    assert "PRIVATE_USER_QUESTION" not in json.dumps(first_details)
    assert "PRIVATE_PROVIDER_ERROR" not in str(first) + str(second) + json.dumps(first_details)


@pytest.fixture
def scoped_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(KnowledgeBase(id=1, name="检修库", name_key="检修库"))
        db.add_all([VideoCache(platform_item_id="local", title="合成检修手册-local", status="done"),
                    VideoCache(platform_item_id="other", title="合成检修手册-other", status="done")])
        db.flush()
        db.add_all([KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="local"),
                    KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id="other")])
        db.commit()
        yield db
    engine.dispose()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("outage", [False, True])
@pytest.mark.parametrize("budget", [140, 400])
def test_trace_context_counts_match_exact_model_evidence_not_retrieval_candidates(
    offline_retrieval, scoped_db, monkeypatch, streaming, outage, budget,
):
    state = offline_retrieval
    state.outage = outage
    monkeypatch.setattr(settings, "rag_context_count", 8)
    monkeypatch.setattr(settings, "rag_prompt_max_context_chars", budget)
    state.documents = [
        document("local:1", "[00:00–00:05] 检修必须先关闭主电源，并验电确认已经断电，禁止带电拆卸设备。"),
        document("local:2", "[00:05–00:10] 检修关闭电源以后还要挂好提示牌，提醒其他人员不要擅自送电。", index=2),
        document("other:1", "[00:00–00:05] 检修之前关闭设备电源，还应检查隔离位置与现场操作条件。", video="other"),
    ]
    state.dense = deepcopy(state.documents)
    model_calls = []

    def chat(**kwargs):
        model_calls.append(("chat", kwargs))
        return "检修先关闭电源。[来源: 资料1]"

    def stream_chat(**kwargs):
        model_calls.append(("stream_chat", kwargs))
        yield "检修先关闭电源。"
        yield "[来源: 资料1]"

    monkeypatch.setattr(module.llm_client, "chat", chat)
    monkeypatch.setattr(module.llm_client, "stream_chat", stream_chat)
    service = module.RagService()
    if streaming:
        events = list(service.answer_stream(scoped_db, "检修前怎样关闭电源？", None, knowledge_base_id=1))
        assert events[-1] == ("done", {"ok": True})
        assert not any(event == "error" for event, _ in events)
        result = next(payload for event, payload in events if event == "meta")
    else:
        result = service.answer(scoped_db, "检修前怎样关闭电源？", None, knowledge_base_id=1)
    assert len(model_calls) == 1, "Diagnostics/packing must not add a compression or judge model call"
    assert model_calls[0][0] == ("stream_chat" if streaming else "chat")
    actual_prompt = model_calls[0][1]["user_prompt"]
    actual_evidence = actual_prompt.split("\n\n相关内容：\n", 1)[1]
    headers = re.findall(r"(?m)^【资料\d+｜([^】]+)】$", actual_evidence)
    stats = result["trace"]["context_stats"]
    assert stats == {"provided_chunks": len(headers), "provided_videos": len(set(headers)),
                     "provided_chars": len(actual_evidence), "max_chars": budget}
    assert len(headers) == len(result["trace"]["chunks"])
    assert 0 < len(actual_evidence) <= budget
    details = result["trace"]["retrieval_details"]
    assert details["counts"]["scope_records"] == 3
    assert details["counts"]["final_hits"] == 3
    if budget == 140:
        assert stats["provided_chunks"] == 1 < details["counts"]["final_hits"]
    else:
        assert stats["provided_chunks"] == 3
        assert stats["provided_videos"] == 2, "Two chunks from one video must not count as two videos"
    assert details["scope_video_count"] == 2
    assert len(state.embeddings) == 1
    if outage:
        assert result["trace"]["retrieval_mode"] == "lexical_fallback"
        assert details["stages_ms"]["query_embedding"] == 5000
        assert "vector_search" not in details["stages_ms"]
        assert all(s["score"] is None for s in result["sources"])
    else:
        assert result["trace"]["retrieval_mode"] == "hybrid"
        assert details["stages_ms"]["query_embedding"] == 75
    assert "PRIVATE_PROVIDER_ERROR" not in json.dumps(result, ensure_ascii=False)
