"""Call-local captures on fake SDKs; never infer an invoice from recent rows."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

if __package__:
    from .test_llm_retry_accounting import module, client, Provider, answer, status_error, PRIVATE
else:
    from test_llm_retry_accounting import module, client, Provider, answer, status_error, PRIVATE


def test_each_concurrent_call_gets_its_own_detached_capture(module, monkeypatch):
    barrier = threading.Barrier(2)
    class ConcurrentProvider(Provider):
        def create(self, **kwargs):
            prompt = kwargs['messages'][1]['content']
            barrier.wait(timeout=3)
            return answer(prompt, tokens={'alpha': 11, 'beta': 22}[prompt])
    service, _, _ = client(module, monkeypatch, ConcurrentProvider([]))
    rows = {'alpha': [], 'beta': []}
    def call(prompt):
        return service.chat('system', prompt, observation_sink=rows[prompt].append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(call, rows)) == ['alpha', 'beta']
    assert [rows[k][0]['known_usage']['total_tokens'] for k in rows] == [11, 22]
    assert len({rows[k][0]['call_id'] for k in rows}) == 2
    assert all(len(value) == 1 for value in rows.values())
    rows['alpha'][0]['known_usage']['total_tokens'] = 999
    assert sorted(row['known_usage']['total_tokens'] for row in service.get_usage_observations()) == [11, 22]


def test_required_sink_failure_is_fixed_and_does_not_repeat_provider_or_sink(module, monkeypatch):
    provider = Provider([answer(tokens=13), answer('must never be requested')])
    service, _, _ = client(module, monkeypatch, provider)
    captures = []
    def fail(row):
        captures.append(row)
        raise OSError(PRIVATE)
    with pytest.raises(RuntimeError, match='^capture_unconfirmed$') as exc:
        service.chat(PRIVATE, PRIVATE, observation_sink=fail)
    assert exc.value.reason == 'capture_unconfirmed'
    assert len(provider.calls) == len(captures) == 1
    assert service.last_observation['known_usage']['total_tokens'] == 13
    assert PRIVATE not in json.dumps(captures)


def test_failure_and_local_preflight_each_capture_once_without_guessing_zero_usage(module, monkeypatch):
    provider = Provider([KeyboardInterrupt()])
    service, _, _ = client(module, monkeypatch, provider)
    rows = []
    with pytest.raises(KeyboardInterrupt):
        service.chat(PRIVATE, PRIVATE, observation_sink=rows.append)
    assert len(rows) == 1 and rows[0]['unknown_outcome'] and rows[0]['request_attempts'] == 1
    for bad_budget in (0, float('nan'), PRIVATE, object()):
        rows.clear()
        with pytest.raises(ValueError, match='invalid_service_budget'):
            service.chat(PRIVATE, PRIVATE, timeout=bad_budget, observation_sink=rows.append)
        assert len(rows) == 1 and rows[0]['outcome'] == 'not_started'
        assert rows[0]['request_attempts'] == 0 and rows[0]['known_usage'] == {}
        assert PRIVATE not in json.dumps(rows)
    assert len(provider.calls) == 1


def test_configuration_identity_is_lazy_actual_and_does_not_return_sensitive_values(module, monkeypatch):
    provider = Provider([])
    service, _, constructors = client(module, monkeypatch, provider)
    first = service.configuration_identity()
    assert constructors == []
    provider.base_url = module.settings.llm_base_url + '/'
    provider.api_key = module.settings.deepseek_api_key
    service._client = provider
    assert service.configuration_identity() == first
    provider.base_url = 'https://user:password@different.invalid/v1?secret=private'
    provider.api_key = 'DIFFERENT_PRIVATE_CREDENTIAL'
    actual = service.configuration_identity()
    assert actual['endpoint_hash'] != first['endpoint_hash'] and actual['credential_hash'] != first['credential_hash']
    text = json.dumps(actual)
    assert all(value not in text for value in ('password', 'different.invalid', 'private', provider.api_key))
    monkeypatch.setattr(module.settings, 'llm_model', 'next-model')
    assert service.configuration_identity()['model'] == 'next-model'
    monkeypatch.setattr(module.settings, 'llm_base_url', 'https://api.deepseek.com')
    before = service.configuration_identity()['generation_parameters_hash']
    monkeypatch.setattr(module.settings, 'llm_thinking_enabled', True)
    assert service.configuration_identity()['generation_parameters_hash'] != before


def test_configuration_identity_never_executes_an_arbitrary_key_provider(module, monkeypatch):
    provider = Provider([])
    service, _, _ = client(module, monkeypatch, provider)
    service._client = provider
    provider.base_url = 'https://unit.invalid/v1'
    provider.api_key = lambda: pytest.fail('identity must never execute credential providers')
    with pytest.raises(RuntimeError, match='identity_unavailable'):
        service.configuration_identity()


def test_one_logical_call_keeps_model_and_thinking_payload_across_safe_429_retry(module, monkeypatch):
    def rejected(_):
        monkeypatch.setattr(module.settings, 'llm_model', 'changed-during-call')
        monkeypatch.setattr(module.settings, 'llm_base_url', 'https://api.deepseek.com')
        monkeypatch.setattr(module.settings, 'llm_thinking_enabled', True)
        raise status_error(429, '0')
    provider = Provider([rejected, answer()])
    service, _, _ = client(module, monkeypatch, provider)
    rows = []
    assert service.chat('system', 'source', observation_sink=rows.append) == '合成回答'
    assert [call['model'] for call in provider.calls] == ['fake-chat', 'fake-chat']
    assert [call['extra_body'] for call in provider.calls] == [None, None]
    assert len(rows) == 1 and rows[0]['request_attempts'] == 2
