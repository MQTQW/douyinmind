"""Fake SDK/clock only: bounded retries, completed batches and honest usage."""
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import httpx2
import openai
import pytest


PRIVATE = 'SYNTHETIC_INPUT_KEY_AND_PROVIDER_DETAIL_NEVER_OBSERVED'


@pytest.fixture
def module(monkeypatch):
    config = ModuleType('app.core.config')
    config.settings = SimpleNamespace(
        deepseek_api_key='synthetic-key', llm_base_url='https://unit.invalid/v1',
        llm_model='fake-chat', llm_thinking_enabled=False, dashscope_api_key='synthetic-key',
        embedding_base_url='https://unit.invalid/v1', embedding_model='fake-embedding',
        query_embedding_timeout_seconds=5.0,
    )
    monkeypatch.setitem(sys.modules, config.__name__, config)
    path = Path(__file__).resolve().parents[1] / 'app/services/llm_service.py'
    spec = importlib.util.spec_from_file_location('retry_accounting_under_test', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def wall(self):
        return 1_800_000_000 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def status_error(status, retry_after=None):
    request = httpx2.Request('POST', 'https://unit.invalid/v1')
    headers = {'retry-after': retry_after} if retry_after is not None else {}
    response = httpx2.Response(status, request=request, headers=headers)
    return openai.APIStatusError(PRIVATE, response=response, body={'message': PRIVATE})


def usage(tokens=5):
    return SimpleNamespace(prompt_tokens=tokens, completion_tokens=0, total_tokens=tokens)


def batch_response(kwargs):
    return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[float(value)])
                                 for i, value in reversed(list(enumerate(kwargs['input'])))],
                           usage=usage(len(kwargs['input'])))


def answer(text='合成回答', *, tokens=5):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=usage(tokens))


class Provider:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls, self.options = [], []
        self.embeddings = self
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        if not self.actions:
            raise AssertionError('Unexpected additional paid request')
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action(kwargs) if callable(action) else action


def client(module, monkeypatch, provider, *, embedding=False, clock=None, **options):
    clock = clock or Clock()
    constructors = []
    def build(**kwargs):
        constructors.append(kwargs)
        return provider
    monkeypatch.setattr(module, 'OpenAI', build)
    kind = module.EmbeddingClient if embedding else module.LLMClient
    service = kind(clock=clock.monotonic, sleep=clock.sleep, wall_clock=clock.wall, **options)
    return service, clock, constructors


def test_later_embedding_batch_retries_only_itself_and_never_replays_completed_batches(module, monkeypatch):
    provider = Provider([batch_response, status_error(429, '2'), batch_response, batch_response])
    service, clock, constructors = client(module, monkeypatch, provider, embedding=True)
    vectors = service.embed_texts([str(i) for i in range(23)])
    assert vectors == [[float(i)] for i in range(23)]
    assert [call['input'] for call in provider.calls] == [
        [str(i) for i in range(10)], [str(i) for i in range(10, 20)],
        [str(i) for i in range(10, 20)], [str(i) for i in range(20, 23)]]
    assert constructors[0]['max_retries'] == 0 and all(row['max_retries'] == 0 for row in provider.options)
    assert clock.sleeps == [2]
    report = service.last_observation
    assert report['request_attempts'] == 4 and report['completed_batches'] == report['total_batches'] == 3
    assert report['known_usage']['total_tokens'] == 23
    assert report['requests_without_usage'] == 1 and report['usage_complete'] is False
    assert report['unknown_outcome'] is False and report['outcome'] == 'succeeded'


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 408, 500, 503])
def test_rejected_or_uncertain_embedding_batch_never_restarts_prior_batches(module, monkeypatch, status):
    provider = Provider([batch_response, status_error(status), batch_response])
    service, clock, _ = client(module, monkeypatch, provider, embedding=True)
    with pytest.raises(RuntimeError) as error:
        service.embed_texts([str(i) for i in range(23)])
    assert PRIVATE not in str(error.value) and len(provider.calls) == 2 and clock.sleeps == []
    report = service.last_observation
    assert report['completed_batches'] == 1 and report['request_attempts'] == 2
    assert report['known_usage']['total_tokens'] == 10
    assert report['unknown_outcome'] is (status == 408 or status >= 500)


def test_malformed_embedding_response_is_not_retried_or_returned_as_partial_vectors(module, monkeypatch):
    provider = Provider([batch_response, SimpleNamespace(data=[], usage=usage(10)), batch_response])
    service, _, _ = client(module, monkeypatch, provider, embedding=True)
    with pytest.raises(RuntimeError, match='返回不完整'):
        service.embed_texts([str(i) for i in range(23)])
    assert len(provider.calls) == 2
    assert service.last_observation['completed_batches'] == 1
    assert service.last_observation['known_usage']['total_tokens'] == 20
    assert service.last_observation['unknown_outcome'] is True


@pytest.mark.parametrize('retry_after,timeout,reason', [
    ('31', 60, 'retry_after_limit'), ('nan', 60, 'retry_after_limit'),
    ('-1', 60, 'retry_after_limit'), ('not-a-date', 60, 'retry_after_limit'),
    ('3', 3, 'budget_exhausted'),
])
def test_retry_after_cannot_exceed_the_total_budget_or_wait_bound(module, monkeypatch, retry_after, timeout, reason):
    provider = Provider([status_error(429, retry_after), answer()])
    service, clock, _ = client(module, monkeypatch, provider)
    with pytest.raises(module.ServiceCallFailure) as error:
        service.chat(PRIVATE, PRIVATE, timeout=timeout)
    assert error.value.reason == reason and len(provider.calls) == 1 and clock.sleeps == []
    assert service.last_observation['unknown_outcome'] is False


def test_http_date_retry_after_and_remaining_timeout_share_one_deadline(module, monkeypatch):
    clock = Clock()
    retry_date = format_datetime(datetime.fromtimestamp(clock.wall() + 3, timezone.utc), usegmt=True)
    def first(_):
        clock.now += 1
        raise status_error(429, retry_date)
    provider = Provider([first, answer()])
    service, _, constructors = client(module, monkeypatch, provider, clock=clock)
    assert service.chat(PRIVATE, PRIVATE, timeout=8) == '合成回答'
    assert clock.sleeps == [2] and [call['timeout'] for call in provider.calls] == [8, 5]
    assert constructors[0]['max_retries'] == 0
    assert [row['max_retries'] for row in provider.options] == [0, 0]


def test_429_has_a_single_three_attempt_limit_without_hidden_sdk_retries(module, monkeypatch):
    provider = Provider([status_error(429, '0') for _ in range(4)])
    service, clock, _ = client(module, monkeypatch, provider)
    with pytest.raises(module.ServiceCallFailure) as error:
        service.chat(PRIVATE, PRIVATE)
    assert error.value.reason == 'retry_limit' and len(provider.calls) == 3 and clock.sleeps == []
    assert service.last_observation['request_attempts'] == 3


@pytest.mark.parametrize('kind', ['timeout', 'connection', 408, 500, 503])
def test_uncertain_paid_chat_call_is_never_automatically_repeated(module, monkeypatch, kind):
    request = httpx2.Request('POST', 'https://unit.invalid/v1')
    error = (openai.APITimeoutError(request=request) if kind == 'timeout' else
             openai.APIConnectionError(message=PRIVATE, request=request) if kind == 'connection' else status_error(kind))
    provider = Provider([error, answer()])
    service, clock, _ = client(module, monkeypatch, provider)
    with pytest.raises(module.ServiceCallFailure) as captured:
        service.chat(PRIVATE, PRIVATE)
    assert PRIVATE not in str(captured.value) and len(provider.calls) == 1 and clock.sleeps == []
    assert service.last_observation['unknown_outcome'] is True
    assert service.last_observation['known_usage'] == {} and service.last_observation['usage_complete'] is False


def test_budget_exhausted_after_a_response_records_its_usage_without_another_call(module, monkeypatch):
    clock = Clock()
    def late(_):
        clock.now += 6
        return answer(tokens=13)
    provider = Provider([late, answer()])
    service, _, _ = client(module, monkeypatch, provider, clock=clock)
    with pytest.raises(module.ServiceCallFailure) as error:
        service.chat(PRIVATE, PRIVATE, timeout=5)
    assert error.value.reason == 'budget_exhausted_after_response'
    assert len(provider.calls) == 1 and service.last_observation['known_usage']['total_tokens'] == 13
    assert service.last_observation['unknown_outcome'] is False


class Stream:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.closed = False
    def __iter__(self):
        return self
    def __next__(self):
        value = next(self.actions)
        if isinstance(value, BaseException):
            raise value
        return value
    def close(self):
        self.closed = True


def delta(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)


def test_partial_stream_is_closed_and_recorded_unknown_without_restarting(module, monkeypatch):
    request = httpx2.Request('POST', 'https://unit.invalid/v1')
    stream = Stream([delta('已显示片段'), openai.APIConnectionError(message=PRIVATE, request=request)])
    provider = Provider([stream, Stream([delta('禁止重发')])])
    service, clock, _ = client(module, monkeypatch, provider)
    result = service.stream_chat(PRIVATE, PRIVATE)
    assert next(result) == '已显示片段'
    with pytest.raises(module.ServiceCallFailure):
        next(result)
    assert stream.closed and len(provider.calls) == 1 and clock.sleeps == []
    assert service.last_observation['unknown_outcome'] is True


def test_caller_closing_stream_does_not_replay_it_or_claim_zero_usage(module, monkeypatch):
    stream = Stream([delta('片段'), delta('尚未读取')])
    provider = Provider([stream])
    service, _, _ = client(module, monkeypatch, provider)
    result = service.stream_chat(PRIVATE, PRIVATE)
    assert next(result) == '片段'
    result.close()
    assert stream.closed and len(provider.calls) == 1
    assert service.last_observation['outcome'] == 'interrupted'
    assert service.last_observation['unknown_outcome'] and service.last_observation['known_usage'] == {}


def test_stream_uses_last_reported_usage_once_and_returns_original_deltas(module, monkeypatch):
    stream = Stream([delta('甲'), delta('乙'), SimpleNamespace(choices=[], usage=usage(17))])
    provider = Provider([stream])
    service, _, _ = client(module, monkeypatch, provider)
    assert list(service.stream_chat(PRIVATE, PRIVATE)) == ['甲', '乙']
    report = service.last_observation
    assert report['request_attempts'] == 1 and report['known_usage']['total_tokens'] == 17
    assert report['outcome'] == 'succeeded' and report['usage_complete'] is True and stream.closed


def test_stream_start_rejection_is_never_retried(module, monkeypatch):
    provider = Provider([status_error(429, '1'), Stream([delta('禁止重发')])])
    service, clock, _ = client(module, monkeypatch, provider)
    with pytest.raises(module.ServiceCallFailure):
        list(service.stream_chat(PRIVATE, PRIVATE))
    assert len(provider.calls) == 1 and clock.sleeps == []


def test_usage_snapshots_are_detached_bounded_and_never_include_inputs_or_provider_diagnostics(module, monkeypatch):
    seen = []
    def broken_sink(row):
        seen.append(row)
        row['known_usage']['total_tokens'] = 999
        raise RuntimeError(PRIVATE)
    provider = Provider([answer(tokens=7), answer(tokens=9)])
    service, _, _ = client(module, monkeypatch, provider, usage_sink=broken_sink)
    assert service.chat(PRIVATE, PRIVATE) == '合成回答'
    snapshot = service.last_observation
    snapshot['known_usage']['total_tokens'] = 999
    assert service.last_observation['known_usage']['total_tokens'] == 7
    assert service.chat(PRIVATE, PRIVATE) == '合成回答'
    assert [row['known_usage']['total_tokens'] for row in service.get_usage_observations()] == [7, 9]
    assert PRIVATE not in json.dumps(service.get_usage_observations(), ensure_ascii=False)
    assert service.last_observation['scope'] == 'process_local_observed_tokens_only_not_invoice'


def test_query_embedding_keeps_its_single_short_request_and_typed_failure(module, monkeypatch):
    provider = Provider([status_error(503), batch_response])
    service, clock, constructors = client(module, monkeypatch, provider, embedding=True)
    with pytest.raises(module.EmbeddingUnavailable) as error:
        service.embed_query(PRIVATE)
    assert error.value.reason == 'upstream' and len(provider.calls) == 1 and clock.sleeps == []
    assert constructors[0]['timeout'] == 5 and constructors[0]['max_retries'] == 0
    assert service.last_observation['request_attempts'] == 1 and service.last_observation['unknown_outcome']


@pytest.mark.parametrize('embedding', [False, True])
def test_process_interruption_preserves_unknown_attempt_and_never_replays_completed_work(module, monkeypatch, embedding):
    actions = [batch_response, KeyboardInterrupt()] if embedding else [KeyboardInterrupt()]
    provider = Provider(actions)
    service, _, _ = client(module, monkeypatch, provider, embedding=embedding)
    with pytest.raises(KeyboardInterrupt):
        if embedding:
            service.embed_texts([str(i) for i in range(12)])
        else:
            service.chat(PRIVATE, PRIVATE)
    report = service.last_observation
    assert report['request_attempts'] == (2 if embedding else 1)
    assert report['completed_batches'] == (1 if embedding else 0)
    assert report['unknown_outcome'] is True and report['outcome'] == 'unknown'


def test_interrupted_stream_open_is_one_unknown_attempt_not_a_free_local_failure(module, monkeypatch):
    provider = Provider([KeyboardInterrupt()])
    service, _, _ = client(module, monkeypatch, provider)
    with pytest.raises(KeyboardInterrupt):
        list(service.stream_chat(PRIVATE, PRIVATE))
    assert len(provider.calls) == 1
    assert service.last_observation['unknown_outcome'] is True
    assert service.last_observation['request_attempts'] == 1
