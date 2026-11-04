"""Small synthetic CPU fixtures only; no DB, provider, browser or media import."""
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services import retrieval_benchmark as benchmark


def test_corpus_and_dense_fixture_generation_are_repeatable_without_global_randomness():
    first = benchmark.build_dataset(32, seed=17)
    assert first == benchmark.build_dataset(32, seed=17)
    assert first != benchmark.build_dataset(32, seed=18)
    documents, cases = first
    assert len(documents) == 33 and len(cases) == 6
    assert all(len(item["text"]) == 384 for item in documents[:-1])
    assert documents[-1]["metadata_only"] is True
    assert any(hit["chunk_id"] == "outside-authorized-scope" for hit in cases[0]["dense_hits"])


def test_small_real_fusion_probe_retains_expected_original_chunks_and_scope():
    documents, cases = benchmark.build_dataset(32)
    before = deepcopy((documents, cases))
    module = benchmark.load_fusion_module()
    for case in cases:
        result, timing = benchmark.profile_fusion(module, case["query"], case["dense_hits"], documents)
        checked = benchmark.assess_result(result, case, documents)
        assert checked["passed"] is True, (case["id"], checked)
        assert timing["total_wall_ms"] >= timing["tokenization_wall_ms"] >= 0
        # A prepared source index may reuse document tokens, while each query
        # still has at least its own fresh tokenizer call.
        assert timing["tokenization_calls"] >= 1
    assert (documents, cases) == before


def test_measurement_hook_is_removed_even_when_ranking_raises():
    def terms(text, **kwargs):
        return Counter(text)
    def broken(*args):
        raise RuntimeError("synthetic ranking failure")
    module = SimpleNamespace(_terms=terms, rerank_chunks=broken)
    with pytest.raises(RuntimeError):
        benchmark.profile_fusion(module, "合成", [], [])
    assert module._terms is terms


def test_fixture_checker_detects_out_of_scope_metadata_and_changed_original_fields():
    documents, cases = benchmark.build_dataset(16)
    case = cases[0]
    good = documents[0]
    assert benchmark.assess_result([good], case, documents)["passed"] is True
    for result in ([{**good, "text": "changed"}], [documents[-1]], [case["dense_hits"][0]], [good, good], []):
        assert benchmark.assess_result(result, case, documents)["passed"] is False


def test_small_matrix_records_raw_samples_hashes_and_honest_scope():
    report = benchmark.benchmark_report([16], warmups=0, repeats=2, measure_memory=False)
    assert report["correctness_passed"] is True and report["source_stable"] is True
    assert report["parameters"]["repeats"] == 2
    assert report["scope"]["network"] is False and report["scope"]["chroma_io"] is False
    assert report["scope"]["embedding"] is False and report["scope"]["llm"] is False
    assert report["source_sha256_before"] == report["source_sha256_after"]
    row = report["rows"][0]
    assert row["corpus_unchanged"] is True and len(row["corpus_sha256"]) == 64
    for query in row["queries"]:
        assert len(query["samples"]) == 2 and query["memory"] is None
        assert query["timing"]["sample_count"] == 2
        assert query["correctness"]["stable_across_runs"] is True


def test_memory_probe_is_separate_and_does_not_claim_native_or_rss_memory():
    report = benchmark.benchmark_size(16, warmups=0, repeats=1, measure_memory=True)
    assert report["correctness_passed"] is True
    for query in report["queries"]:
        assert len(query["samples"]) == 1
        memory = query["memory"]
        assert memory["peak_python_alloc_bytes"] >= memory["retained_python_alloc_bytes"] >= 0
        assert memory["latency_probe_excluded"] is True and memory["corpus_prebuilt"] is True
        assert memory["native_or_rss_memory_included"] is False


@pytest.mark.parametrize("value", [0, 15, True, 100001])
def test_invalid_scale_is_rejected_before_any_measurement(value):
    with pytest.raises(ValueError):
        benchmark.build_dataset(value)
