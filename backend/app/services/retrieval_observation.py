"""Per-request numeric diagnostics for serial retrieval work, without source text.

Usage inside the existing retrieval path::

    observation = RetrievalObservation(
        scope_video_count=None if scope_ids is None else len(scope_ids)
    )
    with observation.measure("query_embedding"):
        query_vector = embedding_client.embed_text(query)
    with observation.measure("vector_search"):
        hits = chroma.search(query_vector, allowed_video_ids=scope_ids)
    observation.set_count("dense_candidates", len(hits))
    diagnostics = observation.to_dict()

Create an instance for each request. Missing stages/counters are unmeasured, not
zero; a failed measured stage still contributes elapsed time. Timings exclude
gaps between blocks, answer generation and all other unmeasured work. Repeated
nonoverlapping uses of the same stage accumulate. No total request duration or
performance improvement is inferred here.

Counter meanings are set by the integration: dense_candidates are actual scoped
vector hits, scope_records are original records eligible for local matching,
final_hits are retrieved hits returned before the answer-context budget, and
expanded_hits counts those hits whose text gained neighboring source records.
No counter accepts documents or queries, and this module never logs or persists.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import math
import time


STAGE_KEYS = (
    "query_embedding",
    "vector_search",
    "scope_documents",
    "keyword_fusion",
    "neighbor_expansion",
)
COUNTER_KEYS = (
    "dense_candidates",
    "scope_records",
    "final_hits",
    "expanded_hits",
)


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _count(value: object) -> bool:
    return type(value) is int and value >= 0 and _finite_number(value)


class RetrievalObservation:
    """A small allowlisted observation, owned by one serial retrieval request."""

    __slots__ = ("_scope_video_count", "_clock", "_stages_ms", "_counts", "_active")

    def __init__(
        self,
        *,
        scope_video_count: int | None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if scope_video_count is not None and not _count(scope_video_count):
            raise ValueError("Scope count must be a nonnegative integer or None")
        self._scope_video_count = scope_video_count
        self._clock = time.perf_counter if clock is None else clock
        self._stages_ms: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._active = False

    def _sample(self) -> int | float | None:
        # Diagnostic clock failure must not hide an original retrieval error.
        try:
            value = self._clock()
        except Exception:
            return None
        return value if _finite_number(value) else None

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        if type(stage) is not str or stage not in STAGE_KEYS:
            raise ValueError("Unsupported retrieval observation stage")
        if self._active:
            raise RuntimeError("Retrieval observation stages must not overlap")
        self._active = True
        started = self._sample()
        try:
            yield
        finally:
            ended = self._sample()
            self._active = False
            if started is not None and ended is not None:
                duration = max(0.0, ended - started) * 1000
                accumulated = self._stages_ms.get(stage, 0.0) + duration
                if _finite_number(accumulated) and accumulated >= 0:
                    self._stages_ms[stage] = accumulated

    def set_count(self, counter: str, value: int) -> None:
        if type(counter) is not str or counter not in COUNTER_KEYS:
            raise ValueError("Unsupported retrieval observation counter")
        if not _count(value):
            raise ValueError("Retrieval observation count must be a nonnegative integer")
        self._counts[counter] = value

    def to_dict(self) -> dict:
        """Return a detached, JSON-safe allowlist, including only numeric data.

        Validate at serialization too: accidental mutation must not expose raw
        text, unexpected keys, booleans or non-finite values through a trace.
        None is used only for the explicitly global scope, not to replace a bad
        scope value with a misleading global marker.
        """
        timings = self._stages_ms if isinstance(self._stages_ms, dict) else {}
        counts = self._counts if isinstance(self._counts, dict) else {}
        result = {
            "stages_ms": {
                key: round(float(timings[key]), 3)
                for key in STAGE_KEYS
                if key in timings and _finite_number(timings[key]) and timings[key] >= 0
            },
            "counts": {
                key: counts[key]
                for key in COUNTER_KEYS
                if key in counts and _count(counts[key])
            },
        }
        if self._scope_video_count is None or _count(self._scope_video_count):
            result["scope_video_count"] = self._scope_video_count
        return result
