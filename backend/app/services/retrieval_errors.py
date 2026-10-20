"""Safe typed boundary for recoverable query-embedding service outages."""

from copy import deepcopy


class EmbeddingUnavailable(RuntimeError):
    REASONS = frozenset({"timeout", "connection", "rate_limit", "upstream"})

    def __init__(self, reason: str = "upstream") -> None:
        if reason not in self.REASONS:
            raise ValueError("Unsupported query-embedding outage reason")
        self.reason = reason
        super().__init__("语义检索服务暂时不可用，请稍后重试。")


class RetrievalHits(list):
    """List-compatible hits with per-request diagnostics, never global state."""

    def __init__(self, hits=(), *, mode: str, fallback_reason: str | None = None,
                 diagnostics: dict | None = None):
        super().__init__(hits)
        self.mode = mode
        self.fallback_reason = fallback_reason
        self.diagnostics = deepcopy(diagnostics) if diagnostics is not None else None
