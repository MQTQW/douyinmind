"""Request-scoped retrieval timings; no database, network, model or credentials."""
import json
import math

import pytest

from app.services.retrieval_observation import RetrievalObservation, STAGE_KEYS


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_serial_stages_use_elapsed_milliseconds_and_exclude_unmeasured_gaps():
    clock = Clock()
    observation = RetrievalObservation(scope_video_count=9, clock=clock)
    for name, seconds in zip(STAGE_KEYS, (.125, .030, .015, .010, .005)):
        with observation.measure(name):
            clock.now += seconds
        clock.now += 10  # Other request work must not be assigned to a phase.
    observation.set_count("dense_candidates", 32)
    observation.set_count("scope_records", 144)
    observation.set_count("final_hits", 8)
    observation.set_count("expanded_hits", 6)
    assert observation.to_dict() == {
        "scope_video_count": 9,
        "stages_ms": dict(zip(STAGE_KEYS, (125, 30, 15, 10, 5))),
        "counts": {"dense_candidates": 32, "scope_records": 144,
                   "final_hits": 8, "expanded_hits": 6},
    }
    assert sum(observation.to_dict()["stages_ms"].values()) == 185


def test_repeated_serial_stage_accumulates_and_new_request_is_independent():
    clock = Clock()
    first = RetrievalObservation(scope_video_count=2, clock=clock)
    second = RetrievalObservation(scope_video_count=0, clock=clock)
    with first.measure("query_embedding"):
        clock.now += .2
    with first.measure("query_embedding"):
        clock.now += .3
    first.set_count("dense_candidates", 2)
    assert first.to_dict()["stages_ms"] == {"query_embedding": 500}
    assert second.to_dict() == {"scope_video_count": 0, "stages_ms": {}, "counts": {}}


def test_failure_records_attempt_time_preserves_exception_and_allows_fallback():
    clock = Clock()
    observation = RetrievalObservation(scope_video_count=1, clock=clock)
    original = TimeoutError("PRIVATE_QUERY_OR_PROVIDER_BODY")
    with pytest.raises(TimeoutError) as caught:
        with observation.measure("query_embedding"):
            clock.now = 5
            raise original
    assert caught.value is original
    with observation.measure("scope_documents"):
        clock.now += .01
    observation.set_count("dense_candidates", 0)
    observation.set_count("final_hits", 1)
    exported = observation.to_dict()
    assert exported["stages_ms"] == {"query_embedding": 5000, "scope_documents": 10}
    assert "vector_search" not in exported["stages_ms"]  # Skipped, not measured zero.
    assert "PRIVATE_QUERY" not in json.dumps(exported)


def test_global_and_empty_scope_remain_distinct_and_zero_is_a_real_count():
    global_scope = RetrievalObservation(scope_video_count=None)
    empty_scope = RetrievalObservation(scope_video_count=0)
    empty_scope.set_count("final_hits", 0)
    assert global_scope.to_dict()["scope_video_count"] is None
    assert empty_scope.to_dict()["scope_video_count"] == 0
    assert empty_scope.to_dict()["counts"] == {"final_hits": 0}
    assert global_scope.to_dict()["counts"] == {}


@pytest.mark.parametrize("value", [-1, True, False, 1.5, 2.0, "2", float("nan"), float("inf"), object(), 10**1000])
def test_counter_rejects_non_integer_nonfinite_or_negative_data_without_echo(value):
    observation = RetrievalObservation(scope_video_count=None)
    with pytest.raises(ValueError, match="nonnegative integer"):
        observation.set_count("final_hits", value)
    assert observation.to_dict()["counts"] == {}


@pytest.mark.parametrize("scope", [-1, True, "PRIVATE_SOURCE_TEXT", math.nan, math.inf, 1.0])
def test_scope_rejects_malformed_counts_without_coercing_them_to_global(scope):
    with pytest.raises(ValueError) as caught:
        RetrievalObservation(scope_video_count=scope)
    assert "PRIVATE_SOURCE_TEXT" not in str(caught.value)


@pytest.mark.parametrize("name", ["query", "原文 PRIVATE_SOURCE", "exception", {}, None])
def test_unknown_stage_or_counter_keys_are_not_accepted_or_echoed(name):
    observation = RetrievalObservation(scope_video_count=None)
    with pytest.raises(ValueError) as stage_error:
        with observation.measure(name):
            pytest.fail("An unknown stage must not enter the body")
    with pytest.raises(ValueError) as count_error:
        observation.set_count(name, 1)
    assert "PRIVATE_SOURCE" not in str(stage_error.value) + str(count_error.value)
    assert observation.to_dict() == {"scope_video_count": None, "stages_ms": {}, "counts": {}}


def test_export_filters_accidental_unknown_keys_values_and_returns_detached_data():
    observation = RetrievalObservation(scope_video_count=3)
    observation._stages_ms.update({"query": "PRIVATE_QUERY", "query_embedding": 12.25,
                                  "vector_search": math.nan, "scope_documents": True,
                                  "keyword_fusion": -1, "neighbor_expansion": math.inf})
    observation._counts.update({"exception": "PRIVATE_ERROR", "dense_candidates": 4,
                               "scope_records": "PRIVATE_TEXT", "final_hits": False,
                               "expanded_hits": -1})
    result = observation.to_dict()
    assert result == {"scope_video_count": 3, "stages_ms": {"query_embedding": 12.25},
                      "counts": {"dense_candidates": 4}}
    assert "PRIVATE" not in json.dumps(result, allow_nan=False)
    result["counts"]["dense_candidates"] = 99
    result["stages_ms"]["query_embedding"] = 99
    assert observation.to_dict()["counts"]["dense_candidates"] == 4
    assert observation.to_dict()["stages_ms"]["query_embedding"] == 12.25


def test_invalid_private_scope_is_omitted_rather_than_misrepresented_as_global():
    observation = RetrievalObservation(scope_video_count=3)
    observation._scope_video_count = "PRIVATE_SOURCE"
    assert "scope_video_count" not in observation.to_dict()
    assert "PRIVATE" not in json.dumps(observation.to_dict(), allow_nan=False)


@pytest.mark.parametrize("samples", [(math.nan, 1), (0, math.inf), (0, "PRIVATE_CLOCK"), (0, 1e308)])
def test_invalid_clock_samples_never_export_nonfinite_or_private_values(samples):
    iterator = iter(samples)
    observation = RetrievalObservation(scope_video_count=None, clock=lambda: next(iterator))
    with observation.measure("query_embedding"):
        pass
    assert observation.to_dict()["stages_ms"] == {}
    json.dumps(observation.to_dict(), allow_nan=False)


def test_clock_failure_does_not_mask_the_retrieval_exception():
    def broken_clock():
        raise RuntimeError("PRIVATE_CLOCK_ERROR")
    observation = RetrievalObservation(scope_video_count=None, clock=broken_clock)
    original = ValueError("retrieval-original")
    with pytest.raises(ValueError) as caught:
        with observation.measure("vector_search"):
            raise original
    assert caught.value is original
    assert observation.to_dict()["stages_ms"] == {}


def test_backwards_clock_is_clamped_and_zero_duration_is_not_dropped():
    iterator = iter((3, 2, 4, 4))
    observation = RetrievalObservation(scope_video_count=None, clock=lambda: next(iterator))
    with observation.measure("query_embedding"):
        pass
    with observation.measure("vector_search"):
        pass
    assert observation.to_dict()["stages_ms"] == {"query_embedding": 0, "vector_search": 0}


def test_overlapping_stages_are_rejected_instead_of_double_counted():
    clock = Clock()
    observation = RetrievalObservation(scope_video_count=None, clock=clock)
    with observation.measure("query_embedding"):
        clock.now = 1
        with pytest.raises(RuntimeError, match="must not overlap"):
            with observation.measure("vector_search"):
                pytest.fail("Overlapping measurements must not start")
        clock.now = 2
    with observation.measure("scope_documents"):
        clock.now = 3
    assert observation.to_dict()["stages_ms"] == {"query_embedding": 2000, "scope_documents": 1000}


def test_default_clock_uses_perf_counter_and_snapshot_excludes_active_stage(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("app.services.retrieval_observation.time.perf_counter", clock)
    observation = RetrievalObservation(scope_video_count=1)
    with observation.measure("query_embedding"):
        clock.now = .001
        assert observation.to_dict()["stages_ms"] == {}
    assert observation.to_dict()["stages_ms"] == {"query_embedding": 1}
