"""Cached source preparation must preserve exact uncached ranking and scope."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import tracemalloc

import pytest

from app.services import retrieval_fusion as fusion
from app.services.prepared_lexical import PreparedLexicalCache


def doc(identity, text, *, title=None, video=None):
    return {"platform_item_id": video or "synthetic-video-" + identity, "chunk_id": identity,
            "title": title or "合成资料" + identity, "text": text, "source": "audio"}


def scope_a():
    return [doc("a", "Python 3.12 虚拟环境隔离项目依赖，使用 python -m venv 创建。"),
            doc("b", "咖啡萃取需要检查水温与研磨度。"),
            doc("c", "SQLite WAL 在读写并发时保留读取快照。")]


def uncached(monkeypatch, query, dense, documents, top_k=8):
    with monkeypatch.context() as local:
        local.setattr(fusion, "_PREPARED_LEXICAL_CACHE", PreparedLexicalCache(0))
        return fusion.rerank_chunks(query, dense, documents, top_k)


def test_cold_warm_and_different_queries_equal_uncached_full_scores(monkeypatch):
    documents = scope_a()
    dense = [documents[1], doc("outside", "Python 3.12 范围外信息")]
    queries = ["Python 3.12 虚拟环境如何隔离依赖？", "水温与研磨度怎么调整？", "SQLite WAL 读写并发", "不存在的 qqq927461"]
    expected = [uncached(monkeypatch, query, dense, documents) for query in queries]
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    before = deepcopy((documents, dense))
    for _ in range(2):
        for query, baseline in zip(queries, expected):
            assert fusion.rerank_chunks(query, dense, documents, 8) == baseline
    assert (documents, dense) == before
    assert cache.stats()["builds"] == 1 and cache.stats()["hits"] == 7


def test_warm_call_reuses_only_document_tokens_not_query_tokens(monkeypatch):
    original = fusion._terms
    calls = []
    def counted(text, **kwargs):
        calls.append((text, kwargs.get("query", False)))
        return original(text, **kwargs)
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    monkeypatch.setattr(fusion, "_terms", counted)
    documents = scope_a()
    fusion.rerank_chunks("Python 虚拟环境", [], documents, 3)
    assert sum(not is_query for _, is_query in calls) == len(documents)
    calls.clear()
    fusion.rerank_chunks("new_query_marker_927461", [], documents, 3)
    assert calls and all(is_query for _, is_query in calls)
    assert all("new_query_marker_927461" not in index.postings for index in cache._entries.values())


@pytest.mark.parametrize("mutation", ["text", "title", "video", "chunk", "order", "delete", "duplicate"])
def test_source_content_identity_title_and_order_changes_invalidate(monkeypatch, mutation):
    documents = scope_a()
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    fusion.rerank_chunks("Python 3.12 虚拟环境", [], documents, 8)
    changed = deepcopy(documents)
    if mutation == "text": changed[0]["text"] += " 当前原文补充新的独有事实。"
    elif mutation == "title": changed[0]["title"] = "新的公开资料标题"
    elif mutation == "video": changed[0]["platform_item_id"] = "new-video"
    elif mutation == "chunk": changed[0]["chunk_id"] = "new-chunk"
    elif mutation == "order": changed.reverse()
    elif mutation == "delete": changed.pop(0)
    else: changed.append(deepcopy(changed[0]))
    dense = [documents[0]]
    expected = uncached(monkeypatch, "Python 3.12 虚拟环境", dense, changed)
    assert fusion.rerank_chunks("Python 3.12 虚拟环境", dense, changed, 8) == expected
    assert cache.stats()["builds"] == 2


def test_alternating_scopes_never_return_unprovided_chunks(monkeypatch):
    first = scope_a()
    second = [doc("other", "Python 范围乙的独立资料，不能流入范围甲。")]
    dense = first + second
    expected = [uncached(monkeypatch, "Python 虚拟环境", dense, scope) for scope in (first, second)]
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    for _ in range(3):
        for scope, baseline in zip((first, second), expected):
            result = fusion.rerank_chunks("Python 虚拟环境", dense, scope, 8)
            assert result == baseline
            assert all(hit["chunk_id"] in {item["chunk_id"] for item in scope} for hit in result)
    assert cache.stats()["builds"] == 2 and cache.stats()["hits"] == 4


def test_equal_text_different_ids_remain_distinct_and_return_mutation_does_not_poison(monkeypatch):
    text = "相同原文内容必须保留不同的视频和文字块身份。"
    documents = [doc("one", text), doc("two", text)]
    expected = uncached(monkeypatch, "原文内容", [], documents)
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    result = fusion.rerank_chunks("原文内容", [], documents, 8)
    assert {item["chunk_id"] for item in result} == {"one", "two"}
    result[0]["text"] = "caller changed returned body"
    result[0]["retrieval_scores"]["bm25"] = -1
    assert fusion.rerank_chunks("原文内容", [], documents, 8) == expected
    assert all(item["text"] == text for item in documents)


def test_threads_share_preparation_without_sharing_mutable_results(monkeypatch):
    documents = scope_a()
    expected = uncached(monkeypatch, "Python 3.12 虚拟环境", documents, documents)
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    def run(_):
        result = fusion.rerank_chunks("Python 3.12 虚拟环境", documents, documents, 8)
        assert result == expected
        result[0]["retrieval_scores"]["bm25"] = -100
        return result[0]["chunk_id"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert len(set(pool.map(run, range(12)))) == 1
    assert cache.stats()["builds"] == 1 and cache.stats()["hits"] == 11


def test_oversize_budget_falls_back_without_publishing_partial_index(monkeypatch):
    documents = [doc(str(row), " ".join(f"token_{row}_{col}" for col in range(12))) for row in range(200)]
    expected = uncached(monkeypatch, "token_0_1", [], documents)
    cache = PreparedLexicalCache(512 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    assert fusion.rerank_chunks("token_0_1", [], documents, 8) == expected
    stats = cache.stats()
    assert stats["entries"] == stats["prepared_index_bytes"] == 0 and stats["fallbacks"] == 1
    assert stats["negative_entries"] == 1 and stats["retained_bytes"] <= stats["max_bytes"]
    assert stats["peak_reserved_bytes"] <= stats["max_bytes"]


def test_cold_preparation_peak_is_within_its_conservative_budget():
    documents = [doc(str(row), " ".join(f"word_{row}_{col}" for col in range(10))) for row in range(160)]
    items = {fusion._identity(hit): hit for hit in documents}
    cache = PreparedLexicalCache(512 * 1024)
    tracemalloc.start()
    try:
        cache.features(documents, items, {"word_0_1"}, fusion._terms)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak <= cache.max_bytes
    assert cache.stats()["peak_reserved_bytes"] <= cache.max_bytes


def test_cache_exception_odd_unicode_and_large_chunk_preserve_original_behavior(monkeypatch):
    documents = [doc("unicode", "Python \ud800 原文仍按既有分词规则处理。"), doc("large", "原文" * 11000)]
    expected = uncached(monkeypatch, "Python 原文", [], documents)
    cache = PreparedLexicalCache(1024 * 1024)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    assert fusion.rerank_chunks("Python 原文", [], documents, 8) == expected
    monkeypatch.setattr(cache, "features", lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic cache failure")))
    assert fusion.rerank_chunks("Python 原文", [], documents, 8) == expected


def test_entry_count_and_retained_bytes_are_bounded_and_empty_scope_stays_empty(monkeypatch):
    cache = PreparedLexicalCache(1024 * 1024, max_entries=2)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    assert fusion.rerank_chunks("Python", [], [], 8) == []
    for number in range(5):
        fusion.rerank_chunks("Python", [], [doc(str(number), "Python " + str(number))], 8)
    stats = cache.stats()
    assert stats["entries"] == 2 and stats["evictions"] == 3
    assert stats["retained_bytes"] <= stats["max_bytes"] and stats["peak_reserved_bytes"] <= stats["max_bytes"]


def test_hard_budget_refusal_avoids_rebuilding_but_still_runs_original_tokens(monkeypatch):
    documents = [doc(str(row), " ".join(f"token_{row}_{col}" for col in range(12))) for row in range(200)]
    expected = uncached(monkeypatch, "token_0_1", [], documents)
    cache = PreparedLexicalCache(512 * 1024)
    original = fusion._terms
    source_calls = []
    def counted(text, **kwargs):
        if not kwargs.get("query", False):
            source_calls.append(text)
        return original(text, **kwargs)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    monkeypatch.setattr(fusion, "_terms", counted)
    assert fusion.rerank_chunks("token_0_1", [], documents, 8) == expected
    assert len(source_calls) > len(documents), "first partial preparation should expose its cold fallback cost"
    source_calls.clear()
    assert fusion.rerank_chunks("token_0_1", [], documents, 8) == expected
    assert len(source_calls) == len(documents) and cache.stats()["negative_hits"] == 1
    cache.clear()
    source_calls.clear()
    assert fusion.rerank_chunks("token_0_1", [], documents, 8) == expected
    assert len(source_calls) > len(documents)
    cache.max_bytes = 4 * 1024 * 1024
    assert cache.stats()["negative_entries"] == 0
    source_calls.clear()
    assert fusion.rerank_chunks("token_0_1", [], documents, 8) == expected
    assert len(source_calls) == len(documents) and cache.stats()["builds"] == 1
    source_calls.clear()
    assert fusion.rerank_chunks("token_0_1", [], documents, 8) == expected
    assert source_calls == []


def test_hard_single_chunk_refusals_are_bounded_and_source_changes_invalidate():
    cache = PreparedLexicalCache(512 * 1024, max_rejections=3)
    calls = []
    def must_not_tokenize(text):
        calls.append(text)
        raise AssertionError("a known oversized chunk cannot enter preparation")
    for number in range(6):
        documents = [doc(str(number), "原" * 20001)]
        items = {fusion._identity(hit): hit for hit in documents}
        assert cache.features(documents, items, {"原文"}, must_not_tokenize) is None
        assert cache.features(documents, items, {"原文"}, must_not_tokenize) is None
    stats = cache.stats()
    assert calls == [] and stats["negative_entries"] == 3 and stats["negative_hits"] == 6
    assert stats["retained_bytes"] <= cache.max_bytes and stats["peak_reserved_bytes"] <= cache.max_bytes
    cache.clear()
    assert cache.stats()["negative_entries"] == cache.stats()["retained_bytes"] == 0


def test_transient_cache_exception_is_not_remembered_as_a_hard_refusal(monkeypatch):
    documents = scope_a()
    expected = uncached(monkeypatch, "Python 虚拟环境", [], documents)
    cache = PreparedLexicalCache(1024 * 1024)
    original = cache._build
    attempts = []
    def transient(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise MemoryError("synthetic transient cache pressure")
        return original(*args, **kwargs)
    monkeypatch.setattr(cache, "_build", transient)
    monkeypatch.setattr(fusion, "_PREPARED_LEXICAL_CACHE", cache)
    assert fusion.rerank_chunks("Python 虚拟环境", [], documents, 8) == expected
    assert cache.stats()["negative_entries"] == 0
    assert fusion.rerank_chunks("Python 虚拟环境", [], documents, 8) == expected
    assert len(attempts) == 2 and cache.stats()["builds"] == 1
