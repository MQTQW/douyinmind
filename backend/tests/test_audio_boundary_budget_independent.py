"""Independent offline checks for review admission, SDK scope, and paid evidence."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def legacy_single_window_budget(monkeypatch):
    monkeypatch.setattr(settings, 'audio_boundary_batch_enabled', False)
from app.services import omni_service as module
from app.services.audio_media import AudioSegment
from app.services.omni_service import AudioAnalysis, OmniService, OmniServiceError


def part(text, start=0, end=16):
    return {"text": text, "start_seconds": start, "end_seconds": end}


@pytest.fixture
def review_case(tmp_path, monkeypatch):
    """Fake only media/checkpoint I/O; leave review control flow intact."""
    source = tmp_path / "offline-source.wav"
    source.write_bytes(b"offline PCM window; no ffmpeg or provider is used")
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 4)
    monkeypatch.setattr(settings, "audio_boundary_review_timeout_seconds", 20)
    monkeypatch.setattr(settings, "audio_boundary_review_budget_seconds", 60)
    roots = [{"start_seconds": i * 20, "parts": [
        part("一段完整原稿必须保留", i * 20, i * 20 + 19),
        part("当", i * 20 + 19, (i + 1) * 20),
    ]} for i in range(5)]
    cache = SimpleNamespace(get=lambda segment: None,
        record_success=lambda *args: None, record_failure=lambda *args: None)
    monkeypatch.setattr(module, "AudioCheckpoint", lambda *args, **kwargs: cache)
    prepared = []

    @contextmanager
    def prepare(path, start, end):
        prepared.append((start, end))
        yield AudioSegment(source, start, end - start)

    monkeypatch.setattr(module, "prepare_audio_window", prepare)
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: pytest.fail("Unmocked provider access"))
    result = AudioAnalysis(transcript_segments=[part("独立补听原始响应")], warnings=["不确定"])
    return SimpleNamespace(service=service, source=source, clock=clock, cache=cache,
        roots=roots, original=deepcopy([p for root in roots for p in root["parts"]]),
        result=result, raw=result.model_dump_json(), prepared=prepared)


@pytest.mark.parametrize("slow_stage", ["window_prepare", "cache_read"])
def test_pre_request_work_cannot_admit_a_request_at_exact_stage_deadline(review_case, monkeypatch, slow_stage):
    case = review_case
    if slow_stage == "cache_read":
        def get(segment):
            case.clock[0] = 60
        case.cache.get = get
    else:
        @contextmanager
        def prepare(path, start, end):
            case.clock[0] = 60
            yield AudioSegment(case.source, start, end - start)
        monkeypatch.setattr(module, "prepare_audio_window", prepare)
    monkeypatch.setattr(case.service, "_analyze_audio_segment", lambda segment: pytest.fail("Budget exhausted before request"))
    speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
    assert speech == case.original
    assert audit["recognition_attempts"] == 0
    assert len(audit["reviews"]) == 4
    assert all(entry["reason"] == "stage_budget_exhausted" for entry in audit["reviews"])
    assert not hasattr(case.service._request_context, "boundary_deadline")


def test_cache_commit_time_counts_toward_the_next_request_admission(review_case, monkeypatch):
    case = review_case
    attempts = []
    def recognize(segment):
        attempts.append(case.service._request_context.boundary_deadline)
        return case.result, case.raw
    def commit(*args):
        case.clock[0] = 60
    case.cache.record_success = commit
    monkeypatch.setattr(case.service, "_analyze_audio_segment", recognize)
    speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
    assert speech == case.original
    assert attempts == [20]
    assert audit["recognition_attempts"] == 1
    assert audit["reviews"][0]["raw_response"] == case.raw
    assert all(entry["reason"] == "stage_budget_exhausted" for entry in audit["reviews"][1:])


def test_cache_read_failure_does_not_start_a_paid_request_or_change_original(review_case, monkeypatch):
    case = review_case
    def unreadable(segment):
        raise OSError("offline cache unavailable")
    case.cache.get = unreadable
    monkeypatch.setattr(case.service, "_analyze_audio_segment", lambda segment: pytest.fail("Unavailable cache must not discard completed roots"))
    speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
    assert speech == case.original
    assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 0
    assert all(entry["reason"] == "review_unavailable" for entry in audit["reviews"])
    assert not hasattr(case.service._request_context, "boundary_deadline")


@pytest.mark.parametrize("corrupt_saved", [
    {"analysis": {"transcript_segments": []}, "raw_response": "   "},
    {"analysis": {"transcript_segments": []}},
    {"analysis": {"transcript_segments": [part("缓存时间越界", 0, 1000)]}, "raw_response": "old"},
])
def test_invalid_cached_evidence_is_replaced_by_one_paid_result_per_window(review_case, monkeypatch, corrupt_saved):
    case = review_case
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    case.cache.get = lambda segment: deepcopy(corrupt_saved)
    calls = []
    def recognize(segment):
        calls.append(segment)
        return case.result, case.raw
    monkeypatch.setattr(case.service, "_analyze_audio_segment", recognize)
    speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
    assert speech == case.original
    assert len(calls) == audit["recognition_attempts"] == 1
    assert audit["reused_windows"] == 0
    assert audit["reviews"][0]["raw_response"] == case.raw


@pytest.mark.parametrize("analyzer_error", [OSError("offline media read"), ValueError("offline invalid data"), RuntimeError("unexpected local failure")])
def test_thread_local_deadline_is_cleared_for_all_analyzer_exit_paths(review_case, monkeypatch, analyzer_error):
    case = review_case
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    seen = []
    def recognize(segment):
        seen.append(case.service._request_context.boundary_deadline)
        raise analyzer_error
    monkeypatch.setattr(case.service, "_analyze_audio_segment", recognize)
    if isinstance(analyzer_error, RuntimeError):
        with pytest.raises(RuntimeError, match="unexpected local failure"):
            case.service._review_audio_boundaries(case.source, 100, case.roots)
    else:
        speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
        assert speech == case.original
        assert audit["reviews"][0]["reason"] == "review_unavailable"
    assert seen == [20]
    assert not hasattr(case.service._request_context, "boundary_deadline")


def test_partial_paid_response_survives_failure_checkpoint_write_error(review_case, monkeypatch):
    case = review_case
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    partial = '{"transcript_segments":[{"text":"已付费半段证据'
    def recognize(segment):
        raise OmniServiceError("offline incomplete response", code="output_incomplete", raw_response=partial)
    def unwritable(*args):
        raise OSError("offline failure checkpoint unavailable")
    case.cache.record_failure = unwritable
    monkeypatch.setattr(case.service, "_analyze_audio_segment", recognize)
    speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
    assert speech == case.original
    assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 1
    assert audit["reviews"][0].get("raw_response") == partial
    assert not hasattr(case.service._request_context, "boundary_deadline")


class SSEBytes(httpx.SyncByteStream):
    def __init__(self, text="离线响应"):
        self.closed = False
        self.payload = ("data: " + json.dumps({"id": "offline", "object": "chat.completion.chunk",
            "created": 0, "model": "offline", "choices": [{"index": 0,
                "delta": {"content": text}, "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n").encode()

    def __iter__(self):
        yield self.payload

    def close(self):
        self.closed = True


@pytest.mark.parametrize("remaining", [7.5, 20, 45])
def test_real_sdk_receives_bounded_timeout_for_each_http_phase_and_preserves_root_client(monkeypatch, remaining):
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(settings, "audio_boundary_review_timeout_seconds", 20)
    observed, streams = [], []
    def transport(request):
        observed.append(request.extensions["timeout"].copy())
        stream = SSEBytes()
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    http = httpx.Client(transport=httpx.MockTransport(transport))
    client = OpenAI(api_key="offline-test-only", base_url="https://offline.invalid/v1", http_client=http,
        timeout=90, max_retries=1)
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: client)
    try:
        service._request_context.boundary_deadline = 100 + remaining
        assert service._request([]) == "离线响应"
        del service._request_context.boundary_deadline
        assert not client.is_closed() and not http.is_closed
        assert client.max_retries == 1
        assert service._request([]) == "离线响应"
        limit = min(20, remaining)
        assert observed == [dict(connect=limit, read=limit, write=limit, pool=limit),
                            dict(connect=90, read=90, write=90, pool=90)]
        assert all(stream.closed for stream in streams)
    finally:
        client.close()


def test_real_sdk_does_not_retry_review_http_500_despite_root_retry_policy(monkeypatch):
    monkeypatch.setattr(module.time, "monotonic", lambda: 0)
    calls = []
    def transport(request):
        calls.append(request)
        return httpx.Response(500, json={"error": {"message": "offline provider unavailable"}})
    client = OpenAI(api_key="offline-test-only", http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        max_retries=1)
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: client)
    service._request_context.boundary_deadline = 20
    try:
        with pytest.raises(OmniServiceError) as error:
            service._request([])
        assert error.value.code == "api_error"
        assert len(calls) == 1
        assert not client.is_closed()
    finally:
        client.close()


def test_boundary_scope_in_another_thread_does_not_change_root_request_policy(monkeypatch):
    options, closed = [], []
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        def with_options(self, **kwargs):
            options.append(kwargs)
            return self
        def create(self, **kwargs):
            class Stream:
                def __iter__(self):
                    yield SimpleNamespace(choices=[SimpleNamespace(
                        delta=SimpleNamespace(content="offline root"), finish_reason="stop")])
                def close(self):
                    closed.append(True)
            return Stream()
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: Client())
    service._request_context.boundary_deadline = -1
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(service._request, []).result(timeout=5) == "offline root"
    assert options == [] and closed == [True]
    assert service._request_context.boundary_deadline == -1


def test_stream_read_timeout_is_optional_and_retains_paid_partial_evidence(review_case, monkeypatch):
    case = review_case
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    closed = []
    partial = '{"transcript_segments":[{"text":"网络中断前的已付费原文'
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield ("data: " + json.dumps({"id": "offline", "object": "chat.completion.chunk",
                "created": 0, "model": "offline", "choices": [{"index": 0,
                    "delta": {"content": partial}, "finish_reason": None}]}) + "\n\n").encode()
            raise httpx.ReadTimeout("offline stream read timeout")
        def close(self):
            closed.append(True)
    def transport(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    client = OpenAI(api_key="offline-test-only", http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(case.service, "_get_client", lambda: client)
    try:
        speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
        assert speech == case.original
        assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 1
        assert audit["reviews"][0]["reason"] == "boundary_review_timeout"
        assert audit["reviews"][0]["raw_response"] == partial
        assert closed == [True]
        assert not client.is_closed()
        assert not hasattr(case.service._request_context, "boundary_deadline")
    finally:
        client.close()


@pytest.mark.parametrize("network_timeout", [httpx.ConnectTimeout, httpx.ReadTimeout])
def test_sdk_wrapped_timeout_is_classified_as_boundary_timeout_without_retries(review_case, monkeypatch, network_timeout):
    case = review_case
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    calls = []
    def transport(request):
        calls.append(request)
        raise network_timeout("offline request setup timeout", request=request)
    client = OpenAI(api_key="offline-test-only", http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        max_retries=1)
    monkeypatch.setattr(case.service, "_get_client", lambda: client)
    try:
        speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
        assert speech == case.original
        assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 1
        assert audit["reviews"][0]["reason"] == "boundary_review_timeout"
        assert audit["reviews"][0]["raw_response"] is None
        assert len(calls) == 1
        assert not client.is_closed()
        assert not hasattr(case.service._request_context, "boundary_deadline")
    finally:
        client.close()


@pytest.mark.parametrize(("close_error", "expected_code"), [
    (httpx.ReadError("offline close failure"), "api_error"),
    (httpx.ReadTimeout("offline close timeout"), "boundary_review_timeout"),
])
def test_stream_close_http_error_preserves_received_evidence_and_original(review_case, monkeypatch, close_error, expected_code):
    case = review_case
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    closed = []
    class Stream:
        def __iter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=case.raw), finish_reason="stop")])
        def close(self):
            closed.append(True)
            raise close_error
    class Client:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: Stream()))
        def with_options(self, **kwargs):
            return self
        def close(self):
            pytest.fail("Shared SDK client must stay open")
    monkeypatch.setattr(case.service, "_get_client", lambda: Client())
    speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
    assert speech == case.original
    assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 1
    assert audit["reviews"][0]["reason"] == expected_code
    assert audit["reviews"][0]["raw_response"] == case.raw
    assert closed == [True]
    assert not hasattr(case.service._request_context, "boundary_deadline")


@pytest.fixture(params=["sdk_default", "DefaultHttpx2Client"])
def httpx2_sdk(request, monkeypatch):
    """Exercise the SDK default constructor as well as its explicit new client."""
    httpx2 = pytest.importorskip("httpx2")
    import openai
    if not hasattr(openai, "DefaultHttpx2Client"):
        pytest.skip("This OpenAI SDK predates its httpx2 default transport")

    def create(handler):
        transport = httpx2.MockTransport(handler)
        if request.param == "sdk_default":
            # Preserve the SDK's constructor and wrapper. Substitute only the
            # transport at httpx2's constructor so no socket can be opened.
            original_init = httpx2.Client.__init__
            def offline_init(self, *args, **kwargs):
                kwargs.update(transport=transport, trust_env=False)
                original_init(self, *args, **kwargs)
            monkeypatch.setattr(httpx2.Client, "__init__", offline_init)
            client = OpenAI(api_key="offline-test-only", timeout=90, max_retries=1)
        else:
            client = OpenAI(api_key="offline-test-only", timeout=90, max_retries=1,
                http_client=openai.DefaultHttpx2Client(transport=transport, trust_env=False))
        assert isinstance(client._client, httpx2.Client)
        return client

    return httpx2, create


@pytest.mark.parametrize("failure_stage", ["sse_read_timeout", "stream_close_read_error", "connect_timeout", "response_read_timeout"])
def test_httpx2_default_sdk_failures_preserve_optional_review_and_raw(review_case, monkeypatch, httpx2_sdk, failure_stage):
    case = review_case
    httpx2, create_client = httpx2_sdk
    monkeypatch.setattr(settings, "audio_boundary_review_max_windows", 1)
    calls, closed = [], []
    partial = '{"transcript_segments":[{"text":"默认传输保留的已付费原文'
    stream_stage = failure_stage in {"sse_read_timeout", "stream_close_read_error"}
    class Stream(httpx2.SyncByteStream):
        def __iter__(self):
            yield ("data: " + json.dumps({"id": "offline-httpx2", "object": "chat.completion.chunk",
                "created": 0, "model": "offline", "choices": [{"index": 0,
                    "delta": {"content": partial},
                    "finish_reason": "stop" if failure_stage == "stream_close_read_error" else None}]}) + "\n\n").encode()
            if failure_stage == "sse_read_timeout":
                raise httpx2.ReadTimeout("offline native SSE read timeout")
        def close(self):
            closed.append(True)
            if failure_stage == "stream_close_read_error":
                raise httpx2.ReadError("offline native stream close error")
    def transport(request):
        calls.append(request)
        if not stream_stage:
            error = httpx2.ConnectTimeout if failure_stage == "connect_timeout" else httpx2.ReadTimeout
            raise error("offline native setup timeout", request=request)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    client = create_client(transport)
    monkeypatch.setattr(case.service, "_get_client", lambda: client)
    try:
        speech, audit = case.service._review_audio_boundaries(case.source, 100, case.roots)
        assert speech == case.original
        assert audit["recognition_attempts"] == audit["failed_recognition_attempts"] == 1
        expected_code = "api_error" if failure_stage == "stream_close_read_error" else "boundary_review_timeout"
        assert audit["reviews"][0]["reason"] == expected_code
        assert audit["reviews"][0]["raw_response"] == (partial if stream_stage else None)
        assert len(calls) == 1
        assert calls[0].extensions["timeout"] == dict(connect=20, read=20, write=20, pool=20)
        assert closed == ([True] if stream_stage else [])
        assert not client.is_closed()
        assert client.max_retries == 1
        assert not hasattr(case.service._request_context, "boundary_deadline")
    finally:
        client.close()


def test_httpx2_default_sdk_request_scope_preserves_root_phase_timeouts(httpx2_sdk, monkeypatch):
    httpx2, create_client = httpx2_sdk
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(settings, "audio_boundary_review_timeout_seconds", 20)
    observed, closed = [], []
    class Stream(httpx2.SyncByteStream):
        def __iter__(self):
            yield ("data: " + json.dumps({"id": "offline-httpx2", "object": "chat.completion.chunk",
                "created": 0, "model": "offline", "choices": [{"index": 0,
                    "delta": {"content": "offline native success"}, "finish_reason": "stop"}]}) + "\n\ndata: [DONE]\n\n").encode()
        def close(self):
            closed.append(True)
    def transport(request):
        observed.append(request.extensions["timeout"].copy())
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
    client = create_client(transport)
    service = OmniService()
    monkeypatch.setattr(service, "_get_client", lambda: client)
    try:
        service._request_context.boundary_deadline = 107.5
        assert service._request([]) == "offline native success"
        del service._request_context.boundary_deadline
        assert not client.is_closed()
        assert service._request([]) == "offline native success"
        assert observed == [dict(connect=7.5, read=7.5, write=7.5, pool=7.5),
                            dict(connect=90, read=90, write=90, pool=90)]
        assert closed == [True, True]
        assert client.max_retries == 1
    finally:
        client.close()
