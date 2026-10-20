"""Query-only transport limits and failure classification; no real HTTP calls."""
import ast
import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx2
import openai
import pytest

from app.services.retrieval_errors import EmbeddingUnavailable


@pytest.fixture
def llm_module(monkeypatch):
    config = ModuleType("app.core.config")
    config.settings = SimpleNamespace(
        dashscope_api_key="synthetic-unit-key", embedding_base_url="https://unit.invalid/v1",
        embedding_model="synthetic-embedding", query_embedding_timeout_seconds=5.0,
    )
    monkeypatch.setitem(sys.modules, config.__name__, config)
    path = Path(__file__).resolve().parents[1] / "app/services/llm_service.py"
    spec = importlib.util.spec_from_file_location("query_embedding_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def response(vector=None, index=0):
    return SimpleNamespace(data=[SimpleNamespace(index=index, embedding=vector if vector is not None else [.25, -.5])])


class Provider:
    def __init__(self, value=None, error=None):
        self.value = value if value is not None else response()
        self.error = error
        self.calls = []
        self.embeddings = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.value


def status_error(status):
    request = httpx2.Request("POST", "https://unit.invalid/embeddings")
    reply = httpx2.Response(status, request=request)
    return openai.APIStatusError("EXTERNAL_SENTINEL_NEVER_EXPOSE", response=reply,
                                 body={"message": "EXTERNAL_SENTINEL_NEVER_EXPOSE"})


def test_query_uses_short_timeout_zero_sdk_retries_and_its_own_client(llm_module, monkeypatch):
    service = llm_module.EmbeddingClient()
    provider = Provider()
    constructors = []

    def create_client(**kwargs):
        constructors.append(kwargs)
        return provider

    monkeypatch.setattr(llm_module, "OpenAI", create_client)
    monkeypatch.setattr(service, "embed_texts", lambda texts: pytest.fail("Query must not enter ingestion retries"))
    assert service.embed_query("synthetic question") == [.25, -.5]
    assert service.embed_text("second question") == [.25, -.5]
    assert len(constructors) == 1
    assert constructors[0]["max_retries"] == 0
    assert constructors[0]["timeout"] == 5.0
    assert len(provider.calls) == 2
    assert [call["input"] for call in provider.calls] == [["synthetic question"], ["second question"]]
    assert service._client is None


@pytest.mark.parametrize("kind, expected_reason", [
    ("timeout", "timeout"), ("connection", "connection"),
    (408, "timeout"), (429, "rate_limit"), (500, "upstream"), (503, "upstream"), (599, "upstream"),
])
def test_only_explicit_service_failures_are_typed_and_never_retried(llm_module, kind, expected_reason):
    request = httpx2.Request("POST", "https://unit.invalid/embeddings")
    error = (openai.APITimeoutError(request=request) if kind == "timeout" else
             openai.APIConnectionError(message="EXTERNAL_SENTINEL_NEVER_EXPOSE", request=request)
             if kind == "connection" else status_error(kind))
    provider = Provider(error=error)
    service = llm_module.EmbeddingClient()
    service._query_client = provider
    with pytest.raises(EmbeddingUnavailable) as captured:
        service.embed_query("synthetic question")
    assert captured.value.reason == expected_reason
    assert "EXTERNAL_SENTINEL" not in str(captured.value)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_request_configuration_and_permission_errors_are_not_downgradable(llm_module, status):
    provider = Provider(error=status_error(status))
    service = llm_module.EmbeddingClient()
    service._query_client = provider
    with pytest.raises(RuntimeError) as captured:
        service.embed_query("synthetic question")
    assert not isinstance(captured.value, EmbeddingUnavailable)
    assert "EXTERNAL_SENTINEL" not in str(captured.value)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("payload", [
    SimpleNamespace(), SimpleNamespace(data=[]),
    SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[.1]), SimpleNamespace(index=1, embedding=[.2])]),
    response(index=1), response(index=False), response(index=0.0),
    SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[]) ]),
    response([float("nan")]), response([float("inf")]), response([True]), response(["invalid"]),
    SimpleNamespace(data=[SimpleNamespace(index=0, embedding=None)]),
])
def test_invalid_payload_is_a_clear_error_and_never_a_lexical_outage(llm_module, payload):
    service = llm_module.EmbeddingClient()
    service._query_client = Provider(value=payload)
    with pytest.raises(RuntimeError) as captured:
        service.embed_query("synthetic question")
    assert not isinstance(captured.value, EmbeddingUnavailable)


def test_missing_credentials_fail_without_a_request_or_downgrade(llm_module, monkeypatch):
    monkeypatch.setattr(llm_module.settings, "dashscope_api_key", " ")
    monkeypatch.setattr(llm_module, "OpenAI", lambda **kwargs: pytest.fail("No client without credentials"))
    with pytest.raises(RuntimeError) as captured:
        llm_module.EmbeddingClient().embed_query("synthetic question")
    assert not isinstance(captured.value, EmbeddingUnavailable)


def test_program_error_is_not_mistaken_for_a_provider_outage(llm_module):
    service = llm_module.EmbeddingClient()
    service._query_client = Provider(error=ValueError("synthetic-program-bug"))
    with pytest.raises(ValueError, match="synthetic-program-bug"):
        service.embed_query("synthetic question")


def test_sdk_schema_validation_error_is_not_a_degradable_network_error(llm_module):
    request = httpx2.Request("POST", "https://unit.invalid/embeddings")
    reply = httpx2.Response(200, request=request)
    provider = Provider(error=openai.APIResponseValidationError(reply, {"EXTERNAL_SENTINEL": "bad schema"}))
    service = llm_module.EmbeddingClient()
    service._query_client = provider
    with pytest.raises(RuntimeError) as captured:
        service.embed_query("synthetic question")
    assert not isinstance(captured.value, EmbeddingUnavailable)
    assert "EXTERNAL_SENTINEL" not in str(captured.value)


@pytest.mark.parametrize("timeout, valid", [(.5, True), (5.0, True), (30.0, True), (.49, False), (30.01, False)])
def test_production_query_timeout_configuration_has_finite_small_bounds(timeout, valid):
    from pydantic import ValidationError

    path = Path(__file__).resolve().parents[1] / "app/core/config.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Load the real Settings definition without executing settings=Settings(),
    # and explicitly disable .env reads for the isolated instances below.
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "settings"
                                            for target in node.targets))]
    namespace = {"__file__": str(path), "__name__": "query_settings_under_test"}
    exec(compile(tree, str(path), "exec"), namespace)
    kwargs = {"_env_file": None, "query_embedding_timeout_seconds": timeout,
              "deepseek_api_key": "", "dashscope_api_key": "", "qwen_omni_api_key": ""}
    if valid:
        assert namespace["Settings"](**kwargs).query_embedding_timeout_seconds == timeout
    else:
        with pytest.raises(ValidationError):
            namespace["Settings"](**kwargs)


def test_concurrent_query_initialization_creates_only_one_client(llm_module, monkeypatch):
    service = llm_module.EmbeddingClient()
    created = []
    gate = threading.Barrier(9)
    provider = Provider()

    def create_client(**kwargs):
        created.append(kwargs)
        # Give all waiting callers a chance to contend for initialization.
        time.sleep(.025)
        return provider

    def get_client():
        gate.wait(timeout=3)
        return service._get_query_client()

    monkeypatch.setattr(llm_module, "OpenAI", create_client)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(get_client) for _ in range(8)]
        gate.wait(timeout=3)
        assert all(future.result(timeout=3) is provider for future in futures)
    assert len(created) == 1


def test_ingestion_batch_order_and_ten_text_limit_stay_unchanged(llm_module):
    batches = []

    class BatchProvider:
        def __init__(self):
            self.embeddings = self

        def create(self, *, model, input):
            batches.append(input)
            return SimpleNamespace(data=[SimpleNamespace(index=index, embedding=[float(value)])
                                         for index, value in reversed(list(enumerate(input)))])

    service = llm_module.EmbeddingClient()
    batch_client = BatchProvider()
    service._client = batch_client
    service._query_client = Provider()
    result = llm_module.EmbeddingClient.embed_texts.__wrapped__(service, [str(i) for i in range(23)])
    assert [len(batch) for batch in batches] == [10, 10, 3]
    assert [vector[0] for vector in result] == list(range(23))
    assert service._client is batch_client
