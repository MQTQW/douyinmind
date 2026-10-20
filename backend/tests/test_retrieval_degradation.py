"""Synthetic retrieval outages; no real credentials, network, model or database.

The typed failure is supplied by a fake client module. These tests constrain
the RAG boundary and leave query-client transport classification to its own
unit tests. In particular, a local index bug must never become a successful
empty-library response.
"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.services.retrieval_errors import EmbeddingUnavailable


@pytest.fixture
def degradation_context(monkeypatch):
    config = ModuleType("app.core.config")
    config.settings = SimpleNamespace(
        retrieval_top_k=8, retrieval_hybrid_enabled=True,
        retrieval_candidate_k=32, retrieval_mmr_fetch_k=32,
        retrieval_mmr_lambda=.55, retrieval_neighbor_chars=350,
    )
    monkeypatch.setitem(sys.modules, config.__name__, config)

    def unavailable(query):
        raise EmbeddingUnavailable("timeout")

    llm = ModuleType("app.services.llm_service")
    llm.EmbeddingUnavailable = EmbeddingUnavailable
    llm.embedding_client = SimpleNamespace(embed_text=unavailable, embed_query=unavailable)
    llm.llm_client = SimpleNamespace()
    monkeypatch.setitem(sys.modules, llm.__name__, llm)
    chroma = ModuleType("app.services.chroma_service")
    chroma.get_chroma_service = lambda: SimpleNamespace()
    monkeypatch.setitem(sys.modules, chroma.__name__, chroma)
    path = Path(__file__).resolve().parents[1] / "app/services/rag_service.py"
    spec = importlib.util.spec_from_file_location("degradation_rag_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, EmbeddingUnavailable


def document(identifier, text, video="local"):
    return {"chunk_id": identifier, "platform_item_id": video,
            "chunk_index": 1, "title": "合成设备手册",
            "text": "语音文字稿（来自视频声音）：\n" + text, "score": .2}


@pytest.mark.parametrize("reason", ["timeout", "connection", "rate_limit", "upstream"])
def test_only_typed_service_outage_can_use_same_scope_lexical_evidence(
    degradation_context, monkeypatch, caplog, reason,
):
    module, unavailable_type = degradation_context
    wanted = document("local:1", "停机检修必须先关闭主电源，并验电确认断电。")
    forbidden = document("foreign:1", "FOREIGN_SECRET 停机检修用另一个库的方法。", "foreign")
    reads = []

    def fail_embedding(query):
        error = unavailable_type(reason)
        # Keep the real shared exception type while simulating a dependency
        # that accidentally attached raw provider text. RAG may expose only the
        # safe reason, even if a wrapper's message regresses in the future.
        error.args = ("EXTERNAL_ERROR_SENTINEL_NEVER_LOG",)
        raise error

    def read_documents(**kwargs):
        reads.append(kwargs["allowed_video_ids"])
        return [wanted, forbidden]

    def forbidden_dense_search(*args, **kwargs):
        pytest.fail("No vector can be searched when query embedding failed")

    monkeypatch.setattr(module.embedding_client, "embed_text", fail_embedding)
    monkeypatch.setattr(module.embedding_client, "embed_query", fail_embedding)
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        search=forbidden_dense_search, get_documents=read_documents))
    hits = module.RagService()._dense_retrieve("停机检修如何关闭电源并验电？", {"local"})
    assert reads == [{"local"}]
    assert [hit["chunk_id"] for hit in hits] == ["local:1"]
    assert "FOREIGN_SECRET" not in str(hits)
    assert "EXTERNAL_ERROR_SENTINEL" not in caplog.text


def test_lexical_fallback_with_no_matching_text_returns_no_fabricated_evidence(degradation_context, monkeypatch):
    module, _ = degradation_context
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        get_documents=lambda **kwargs: [document("local:1", "蝴蝶幼虫以植物叶片为食。")]))
    assert module.RagService()._dense_retrieve("离心泵叶轮检修参数", {"local"}) == []


def test_empty_scope_short_circuits_all_external_and_index_calls(degradation_context, monkeypatch):
    module, _ = degradation_context

    def forbidden(*args, **kwargs):
        pytest.fail("Empty scope requires no embedding or index work")

    monkeypatch.setattr(module, "get_chroma_service", forbidden)
    monkeypatch.setattr(module.embedding_client, "embed_text", forbidden)
    monkeypatch.setattr(module.embedding_client, "embed_query", forbidden)
    assert module.RagService()._dense_retrieve("未导入资料有什么参数？", set()) == []


@pytest.mark.parametrize("error", [ValueError("synthetic-program-bug"), RuntimeError("synthetic-invalid-payload")])
def test_ordinary_embedding_errors_are_not_silently_downgraded(degradation_context, monkeypatch, error):
    module, _ = degradation_context

    def fail(query):
        raise error

    def forbidden(**kwargs):
        pytest.fail("Only the typed outage may trigger fallback")

    monkeypatch.setattr(module.embedding_client, "embed_text", fail)
    monkeypatch.setattr(module.embedding_client, "embed_query", fail)
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(get_documents=forbidden))
    with pytest.raises(type(error), match=str(error)):
        module.RagService()._dense_retrieve("合成检修问题", {"local"})


def test_local_index_corruption_during_fallback_remains_an_error(degradation_context, monkeypatch):
    module, _ = degradation_context

    def fail_index(**kwargs):
        raise RuntimeError("synthetic-index-corruption")

    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(get_documents=fail_index))
    with pytest.raises(RuntimeError, match="synthetic-index-corruption"):
        module.RagService()._dense_retrieve("合成检修问题", {"local"})


def test_hybrid_disabled_does_not_secretly_enable_a_different_retrieval_mode(degradation_context, monkeypatch):
    module, unavailable_type = degradation_context
    monkeypatch.setattr(module.settings, "retrieval_hybrid_enabled", False)
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        get_documents=lambda **kwargs: pytest.fail("Explicitly disabled lexical route must stay disabled")))
    with pytest.raises(unavailable_type):
        module.RagService()._dense_retrieve("合成检修问题", {"local"})


def test_dense_index_program_error_is_not_misclassified_as_embedding_outage(degradation_context, monkeypatch):
    module, _ = degradation_context

    def fail_index(*args, **kwargs):
        raise ValueError("synthetic-dimension-mismatch")

    monkeypatch.setattr(module.embedding_client, "embed_text", lambda query: [.1, .2])
    monkeypatch.setattr(module.embedding_client, "embed_query", lambda query: [.1, .2])
    monkeypatch.setattr(module, "get_chroma_service", lambda: SimpleNamespace(
        search=fail_index, get_documents=lambda **kwargs: []))
    with pytest.raises(ValueError, match="synthetic-dimension-mismatch"):
        module.RagService()._dense_retrieve("合成检修问题", {"local"})
