"""Display helpers use actual local HTTP routes and budgets with fake providers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.assistant import routes, semantic_intent_model
from app.assistant.queue import AssistantQueue, QueueError, encoded, normalized_result
from app.assistant.usage_budget import UsageBudget
from tests.test_assistant_queue import binding, envelope


class Model:
    def __init__(self, mode, *, fail=False):
        self.mode, self.fail, self.calls = mode, fail, 0
        self.lock = threading.Lock()

    def complete(self, messages, tools):
        with self.lock:
            self.calls += 1
        if self.fail:
            raise RuntimeError('SYNTHETIC_PROVIDER_ERROR')
        if self.mode == 'plan-proposal':
            assert tools[0]['function']['name'] == 'submit_plan'
            message = {'tool_calls': [{'type': 'function', 'function': {'name': 'submit_plan',
                'arguments': json.dumps({'goal': 'save_then_query', 'question': '总结三个要点'})}}]}
            finish = 'tool_calls'
        else:
            assert tools == []
            assert 'FAKE_ORIGINAL_COMMENT' not in messages[-1]['content']
            message, finish = {'content': '原评论定位尚未完成，可先核对线程再决定下一步。'}, 'stop'
        return SimpleNamespace(message=message, finish_reason=finish,
            usage={'total_tokens': 120, 'input_tokens': 80, 'output_tokens': 40})


@pytest.fixture
def system(tmp_path, monkeypatch):
    queue = AssistantQueue(tmp_path / 'queue.sqlite3')
    task = queue.enqueue(binding(), envelope(comment_text='@虚构助手 FAKE_ORIGINAL_COMMENT 保存到测试库再总结'), kind='query')
    claim = queue.claim_next('execute', run_id='audit-fixture')
    task = queue.finish(task['id'], claim['execution_token'], 'execute',
        {'state': 'needs_review', 'reason_code': 'original_notification_not_found',
         'reply_state': 'needs_review', 'details': {'reply_text': '合成未发草稿'}})
    budget = UsageBudget.prepare(tmp_path / 'usage-budget.sqlite3')
    service = routes.AssistantAPI(queue, control=object(), runtime=object())
    app = FastAPI()
    app.dependency_overrides[routes.get_service] = lambda: service
    app.include_router(routes.router, prefix='/api')
    def forbidden(*args, **kwargs):
        pytest.fail('Display helper invoked a task execution or publication effect')
    monkeypatch.setattr(queue, 'enqueue', forbidden)
    monkeypatch.setattr(queue, 'request_action', forbidden)
    monkeypatch.setattr(queue, 'claim_next', forbidden)
    return SimpleNamespace(queue=queue, task=task, budget=budget, app=app, monkeypatch=monkeypatch)


def client_for(system):
    return TestClient(system.app, base_url='http://127.0.0.1', client=('127.0.0.1', 51000))


def stable_task(task):
    return {key: deepcopy(task.get(key)) for key in ['id', 'state', 'reason_code', 'kind',
        'execution_attempts', 'publish_attempts', 'binding', 'envelope', 'execution_token',
        'publication_token', 'agent_task_id', 'reply_state', 'import_state']}


@pytest.mark.parametrize('mode,cache_key', [('plan-proposal', 'semantic_plan_proposal'), ('triage', 'semantic_triage')])
def test_http_success_persists_display_cache_get_detail_reads_it_and_second_call_reuses(system, mode, cache_key):
    model = Model(mode)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    before = stable_task(system.queue.get_task(system.task['id']))
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id'] + '/' + mode
        first, second = client.post(url, json={}), client.post(url, json={})
        detail = client.get('/api/assistant/tasks/' + system.task['id'])
    assert first.status_code == second.status_code == detail.status_code == 200
    assert first.json()['cached'] is False and second.json()['cached'] is True
    assert model.calls == 1
    assert detail.json()['details'][cache_key] == system.queue.get_task(system.task['id'])['details'][cache_key]
    assert stable_task(system.queue.get_task(system.task['id'])) == before
    ledger = system.budget.summary(system.task['id'])['task']
    assert ledger['request_count'] == 1 and ledger['actual_tokens'] == 120
    assert ledger['unsettled_requests'] == 0


@pytest.mark.parametrize('mode', ['plan-proposal', 'triage'])
def test_http_budget_denial_returns_explanation_without_calling_model_or_progressing_task(system, mode):
    model = Model(mode)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    system.budget.set_limits(per_task_tokens=0, daily_tokens=0)
    before = stable_task(system.queue.get_task(system.task['id']))
    with client_for(system) as client:
        result = client.post('/api/assistant/tasks/' + system.task['id'] + '/' + mode, json={})
    payload = result.json()
    fallback = payload['proposal']['fallback'] if mode == 'plan-proposal' else payload['fallback']
    assert result.status_code == 200 and fallback is True
    assert model.calls == 0 and system.budget.summary()['all_time']['request_count'] == 0
    assert stable_task(system.queue.get_task(system.task['id'])) == before


@pytest.mark.parametrize('mode,cache_key', [('plan-proposal', 'semantic_plan_proposal'), ('triage', 'semantic_triage')])
def test_http_provider_error_has_fixed_fallback_preserves_task_and_records_reservation(system, mode, cache_key):
    model = Model(mode, fail=True)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    before = stable_task(system.queue.get_task(system.task['id']))
    with client_for(system) as client:
        result = client.post('/api/assistant/tasks/' + system.task['id'] + '/' + mode, json={})
        detail = client.get('/api/assistant/tasks/' + system.task['id'])
    payload = result.json()
    fallback = payload['proposal']['fallback'] if mode == 'plan-proposal' else payload['fallback']
    assert result.status_code == 200 and fallback is True
    assert 'SYNTHETIC_PROVIDER_ERROR' not in result.text
    assert stable_task(system.queue.get_task(system.task['id'])) == before
    assert model.calls == 1
    assert cache_key not in detail.json()['details']
    ledger = system.budget.summary()['all_time']
    assert ledger['request_count'] == ledger['unsettled_requests'] == 1


@pytest.mark.parametrize('mode', ['plan-proposal', 'triage'])
@pytest.mark.parametrize('problem', ['foreign_origin', 'untrusted_body'])
def test_http_rejects_foreign_origin_and_injected_execution_parameters(system, mode, problem):
    model = Model(mode)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        result = client.post('/api/assistant/tasks/' + system.task['id'] + '/' + mode,
            json={'execute': True} if problem == 'untrusted_body' else {},
            headers={'Origin': 'https://example.invalid'} if problem == 'foreign_origin' else {})
    assert result.status_code == (403 if problem == 'foreign_origin' else 422)
    assert model.calls == 0


@pytest.mark.parametrize('mode', ['plan-proposal', 'triage'])
def test_http_concurrent_first_requests_allow_only_one_provider_call_for_budget_key(system, mode):
    model = Model(mode)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    barrier = threading.Barrier(2)
    original_get = system.queue.get_task
    lock, reads = threading.Lock(), []
    def get_uncached(task_id):
        task = original_get(task_id)
        with lock:
            reads.append(task_id)
            first_pair = len(reads) <= 2
        if first_pair:
            barrier.wait(timeout=10)
        return task
    system.monkeypatch.setattr(system.queue, 'get_task', get_uncached)
    with client_for(system) as client, ThreadPoolExecutor(max_workers=2) as pool:
        url = '/api/assistant/tasks/' + system.task['id'] + '/' + mode
        results = list(pool.map(lambda _: client.post(url, json={}), range(2)))
    assert all(result.status_code == 200 for result in results)
    ledger = system.budget.summary()['all_time']
    # A durable budget key permits exactly one provider request.
    assert model.calls == ledger['request_count'] == 1, {'provider_calls': model.calls, 'ledger': ledger}


def test_http_repeated_failed_plan_does_not_call_provider_again_for_existing_reservation(system):
    model = Model('plan-proposal', fail=True)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id'] + '/plan-proposal'
        first, second = client.post(url, json={}), client.post(url, json={})
    assert first.status_code == second.status_code == 200
    assert first.json()['proposal']['fallback'] and second.json()['proposal']['fallback']
    ledger = system.budget.summary()['all_time']
    assert model.calls == ledger['request_count'] == 1, {'provider_calls': model.calls, 'ledger': ledger}


def test_http_waiting_library_fallback_describes_the_choice_wait(tmp_path, monkeypatch):
    queue = AssistantQueue(tmp_path / 'queue.sqlite3')
    task = queue.enqueue(binding(), envelope(), kind='clarify')
    claim = queue.claim_next('execute', run_id='waiting-fixture')
    task = queue.finish(task['id'], claim['execution_token'], 'execute', {
        'state': 'waiting_library', 'reply_state': 'b_view_visible', 'send_token_present': True,
        'safe_to_publish': False, 'binding_active': True, 'public_reply_allowed': True,
        'details': {'library_choice': {'choice_id': 'c' * 64, 'state': 'waiting_library'}}})
    assert task['state'] == 'waiting_library'
    budget = UsageBudget.prepare(tmp_path / 'usage-budget.sqlite3')
    budget.set_limits(per_task_tokens=0, daily_tokens=0)
    model = Model('triage')
    monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    app = FastAPI()
    app.dependency_overrides[routes.get_service] = lambda: routes.AssistantAPI(queue, control=object())
    app.include_router(routes.router, prefix='/api')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 51000)) as client:
        response = client.post('/api/assistant/tasks/' + task['id'] + '/triage', json={})
        detail = client.get('/api/assistant/tasks/' + task['id'])
    assert response.status_code == detail.status_code == 200
    assert response.json()['fallback'] and model.calls == 0
    assert detail.json()['state'] == 'waiting_library'
    assert '还没有发出' not in response.json()['text'], response.json()


def test_http_triage_cache_follows_task_facts_after_task_is_closed(system):
    model = Model('triage')
    def explain_facts(messages, tools):
        model.calls += 1
        facts = json.loads(messages[-1]['content'])
        return SimpleNamespace(message={'content': '当前状态 ' + facts['state']}, finish_reason='stop',
            usage={'total_tokens': 120, 'input_tokens': 80, 'output_tokens': 40})
    model.complete = explain_facts
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id'] + '/triage'
        first = client.post(url, json={})
        assert first.status_code == 200 and 'needs_review' in first.json()['text']
        closed = system.queue.dismiss_unsent(system.task['id'])
        assert closed['state'] == 'failed_no_submit'
        second = client.post(url, json={})
        detail = client.get('/api/assistant/tasks/' + system.task['id'])
    assert second.status_code == 200 and detail.json()['state'] == 'failed_no_submit'
    assert 'failed_no_submit' in second.json()['text'], {
        'current_state': detail.json()['state'], 'cached_result': second.json(), 'model_calls': model.calls}
    assert second.json()['cached'] is False and model.calls == 2


def test_legacy_unscoped_triage_is_hidden_and_regenerated_for_current_facts(system):
    with system.queue._tx() as db:
        db.execute('UPDATE assistant_tasks SET details=? WHERE id=?',
            (json.dumps({'semantic_triage': 'OLD_UNSCOPED_TEXT'}), system.task['id']))
    model = Model('triage')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        before = client.get(url)
        generated = client.post(url + '/triage', json={})
        after = client.get(url)
    assert 'semantic_triage' not in before.json()['details']
    assert generated.json()['cached'] is False and model.calls == 1
    assert after.json()['details']['semantic_triage'] == generated.json()['text']
    assert 'OLD_UNSCOPED_TEXT' not in after.text


@pytest.mark.parametrize('transition', ['finish', 'reconcile'])
def test_worker_updates_keep_plan_and_scope_old_triage_to_its_facts(system, transition):
    model = Model('plan-proposal')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        plan = client.post(url + '/plan-proposal', json={}).json()['proposal']
        model.mode = 'triage'
        triage = client.post(url + '/triage', json={}).json()['text']
        result = {'state': 'needs_review', 'reason_code': 'preflight_failed',
            'reply_state': 'needs_review', 'details': {'reply_text': 'updated fake draft'}}
        if transition == 'finish':
            system.queue.retry_unsent(system.task['id'])
            claim = AssistantQueue.claim_next(system.queue, 'execute', run_id='next-fixture')
            system.queue.finish(claim['id'], claim['execution_token'], 'execute', result)
        else:
            system.queue.apply_reconciliation(system.task['id'], result)
        detail = client.get(url).json()
        again = client.post(url + '/plan-proposal', json={}).json()
    raw = system.queue.get_task(system.task['id'])
    assert raw['details']['semantic_plan_proposal'] == plan
    assert raw['details']['semantic_triage'] == triage
    assert 'semantic_triage' not in detail['details']
    assert detail['details']['semantic_plan_proposal'] == plan
    assert again['cached'] and model.calls == 2


def test_state_change_during_generation_returns_stale_without_caching_old_explanation(system):
    model = Model('triage')
    original_complete = model.complete
    def complete(messages, tools):
        result = original_complete(messages, tools)
        system.queue.dismiss_unsent(system.task['id'])
        return result
    model.complete = complete
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        result = client.post(url + '/triage', json={}).json()
        detail = client.get(url).json()
    assert result['stale'] and result['fallback'] and not result['cached']
    assert detail['state'] == 'failed_no_submit' and 'semantic_triage' not in detail['details']
    assert model.calls == 1


def test_cache_write_rechecks_facts_inside_the_queue_transaction(system):
    model = Model('triage')
    original_set = system.queue.set_semantic_triage
    def changed_set(task_id, body, **kwargs):
        system.queue.dismiss_unsent(task_id)
        return original_set(task_id, body, **kwargs)
    system.monkeypatch.setattr(system.queue, 'set_semantic_triage', changed_set)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        result = client.post(url + '/triage', json={}).json()
        detail = client.get(url).json()
    assert result['stale'] and detail['state'] == 'failed_no_submit'
    assert 'semantic_triage' not in detail['details'] and model.calls == 1


@pytest.mark.parametrize('mode', ['plan-proposal', 'triage'])
def test_pending_request_has_visible_status_without_publishing_a_display_cache(system, mode):
    model = Model(mode)
    started, release = threading.Event(), threading.Event()
    original_complete = model.complete
    def complete(messages, tools):
        started.set()
        assert release.wait(timeout=10)
        return original_complete(messages, tools)
    model.complete = complete
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client, ThreadPoolExecutor(max_workers=1) as pool:
        url = '/api/assistant/tasks/' + system.task['id']
        future = pool.submit(lambda: client.post(url + '/' + mode, json={}))
        assert started.wait(timeout=10)
        pending = client.post(url + '/' + mode, json={}).json()
        intermediate = client.get(url).json()
        release.set()
        first = future.result(timeout=10).json()
        cached = client.post(url + '/' + mode, json={}).json()
    cache_key = 'semantic_plan_proposal' if mode == 'plan-proposal' else 'semantic_triage'
    assert pending['pending'] and '生成' in (pending['proposal']['summary'] if mode == 'plan-proposal' else pending['text'])
    assert cache_key not in intermediate['details']
    assert first['success'] and cached['cached'] and model.calls == 1


@pytest.mark.parametrize('mode', ['plan-proposal', 'triage'])
def test_duplicate_between_settlement_and_cache_cannot_replace_the_real_result(system, mode):
    model = Model(mode)
    settled, release = threading.Event(), threading.Event()
    original_settle = UsageBudget.settle
    def settle(budget, request_key, usage):
        result = original_settle(budget, request_key, usage)
        settled.set()
        assert release.wait(timeout=10)
        return result
    system.monkeypatch.setattr(UsageBudget, 'settle', settle)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client, ThreadPoolExecutor(max_workers=1) as pool:
        url = '/api/assistant/tasks/' + system.task['id']
        future = pool.submit(lambda: client.post(url + '/' + mode, json={}))
        assert settled.wait(timeout=10)
        duplicate = client.post(url + '/' + mode, json={}).json()
        intermediate = client.get(url).json()
        release.set()
        first = future.result(timeout=10).json()
        cached = client.post(url + '/' + mode, json={}).json()
    key = 'semantic_plan_proposal' if mode == 'plan-proposal' else 'semantic_triage'
    assert not duplicate.get('pending') and (duplicate['proposal']['fallback'] if mode == 'plan-proposal' else duplicate['fallback'])
    assert key not in intermediate['details']
    assert not (first['proposal']['fallback'] if mode == 'plan-proposal' else first['fallback'])
    assert cached['cached'] and model.calls == 1


@pytest.mark.parametrize('mode', ['plan-proposal', 'triage'])
def test_budget_denial_advice_is_not_cached_and_new_allowance_can_generate(system, mode):
    model = Model(mode)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    system.budget.set_limits(per_task_tokens=0, daily_tokens=0)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        denied = client.post(url + '/' + mode, json={}).json()
        detail = client.get(url).json()
        system.budget.set_limits(per_task_tokens=None, daily_tokens=None)
        allowed = client.post(url + '/' + mode, json={}).json()
    key = 'semantic_plan_proposal' if mode == 'plan-proposal' else 'semantic_triage'
    assert (denied['proposal']['fallback'] if mode == 'plan-proposal' else denied['fallback'])
    assert key not in detail['details']
    assert not (allowed['proposal']['fallback'] if mode == 'plan-proposal' else allowed['fallback'])
    assert model.calls == 1


def settled_plan_without_cache(system):
    key = 'plan-proposal:' + system.task['id']
    system.budget.reserve(system.task['id'], key, 1200)
    system.budget.settle(key, {'total_tokens': 120, 'input_tokens': 80, 'output_tokens': 40})


def test_lost_settled_plan_requires_explicit_regeneration_and_then_reuses_cache(system):
    settled_plan_without_cache(system)
    model = Model('plan-proposal')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id'] + '/plan-proposal'
        first = client.post(url, json={}).json()
        again = client.post(url, json={}).json()
        assert first['can_regenerate'] and again['can_regenerate'] and model.calls == 0
        regenerated = client.post(url, json={'regenerate': True}).json()
        cached = client.post(url, json={'regenerate': True}).json()
    assert not regenerated['proposal']['fallback'] and not regenerated['can_regenerate']
    assert cached['cached'] and model.calls == 1
    ledger = system.budget.summary()['all_time']
    assert ledger['request_count'] == 2 and ledger['actual_tokens'] == 240


@pytest.mark.parametrize('state', ['missing', 'reserved', 'unknown'])
def test_unsettled_or_missing_original_plan_never_allows_regeneration(system, state):
    key = 'plan-proposal:' + system.task['id']
    if state != 'missing':
        system.budget.reserve(system.task['id'], key, 1200)
    if state == 'unknown':
        system.budget.mark_unknown(key)
    model = Model('plan-proposal')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        result = client.post('/api/assistant/tasks/' + system.task['id'] + '/plan-proposal',
            json={'regenerate': True}).json()
    assert result['proposal']['fallback'] and not result['can_regenerate'] and model.calls == 0
    assert system.budget.get_request('plan-proposal-regenerate:' + system.task['id']) is None


def test_concurrent_explicit_regeneration_uses_one_additional_budget_key(system):
    settled_plan_without_cache(system)
    model = Model('plan-proposal')
    barrier = threading.Barrier(2)
    original_get = system.queue.get_task
    lock, reads = threading.Lock(), []
    def first_pair(task_id):
        task = original_get(task_id)
        with lock:
            reads.append(task_id)
            pause = len(reads) <= 2
        if pause:
            barrier.wait(timeout=10)
        return task
    system.monkeypatch.setattr(system.queue, 'get_task', first_pair)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client, ThreadPoolExecutor(max_workers=2) as pool:
        url = '/api/assistant/tasks/' + system.task['id'] + '/plan-proposal'
        responses = list(pool.map(lambda _: client.post(url, json={'regenerate': True}), range(2)))
        cached = client.post(url, json={}).json()
    assert all(response.status_code == 200 for response in responses)
    assert cached['cached'] and model.calls == 1
    assert system.budget.summary()['all_time']['request_count'] == 2


def test_failed_regeneration_cannot_create_another_provider_call(system):
    settled_plan_without_cache(system)
    model = Model('plan-proposal', fail=True)
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id'] + '/plan-proposal'
        first = client.post(url, json={'regenerate': True}).json()
        second = client.post(url, json={'regenerate': True}).json()
        normal = client.post(url, json={}).json()
    assert first['proposal']['fallback'] and second['proposal']['fallback']
    assert not first['can_regenerate'] and not normal['can_regenerate'] and model.calls == 1
    assert system.budget.summary()['all_time']['request_count'] == 2


@pytest.mark.parametrize('value', ['true', 1, None])
def test_regeneration_flag_requires_an_explicit_boolean(system, value):
    model = Model('plan-proposal')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        result = client.post('/api/assistant/tasks/' + system.task['id'] + '/plan-proposal',
            json={'regenerate': value})
    assert result.status_code == 422 and model.calls == 0


def largest_worker_result():
    source = {name: 'x' * 2048 for name in
        ['source_id', 'id', 'title', 'url', 'platform_item_id', 'chunk_id', 'source_type']}
    result = {'state': 'needs_review', 'reason_code': 'preflight_failed', 'reply_state': 'needs_review',
        'details': {'sources': [deepcopy(source) for _ in range(4)], 'reply_text': ''}}
    room = 65536 - len(encoded(result))
    assert 0 < room <= 8000
    result['details']['reply_text'] = 'r' * room
    assert len(encoded(result)) == 65536
    normalized_result(result)
    return result


@pytest.mark.parametrize('transition', ['finish', 'reconcile'])
def test_maximum_worker_metadata_keeps_helpers_and_supports_later_helper_writes(system, transition):
    model = Model('plan-proposal')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        plan = client.post(url + '/plan-proposal', json={}).json()['proposal']
        system.queue.set_semantic_triage(system.task['id'], 'before worker')
        result = largest_worker_result()
        if transition == 'finish':
            system.queue.retry_unsent(system.task['id'])
            claim = AssistantQueue.claim_next(system.queue, 'execute', run_id='large-fixture')
            system.queue.finish(claim['id'], claim['execution_token'], 'execute', result)
        else:
            system.queue.apply_reconciliation(system.task['id'], result)
        raw = system.queue.get_task(system.task['id'])
        assert raw['details']['sources'] == result['details']['sources']
        assert raw['details']['reply_text'] == result['details']['reply_text']
        assert raw['details']['semantic_plan_proposal'] == plan
        assert raw['details']['semantic_triage'] == 'before worker'
        system.queue.set_semantic_triage(system.task['id'], 'current worker facts')
        system.queue.set_semantic_plan_proposal(system.task['id'], plan)
        refreshed = client.get(url).json()
        assert refreshed['details']['semantic_triage'] == 'current worker facts'
        assert refreshed['details']['semantic_plan_proposal'] == plan
        assert refreshed['details']['sources'] == result['details']['sources']


def test_worker_and_plan_inputs_keep_original_metadata_limits(system):
    result = largest_worker_result()
    result['details']['reply_text'] += 'x'
    with pytest.raises(QueueError, match='metadata_too_large'):
        normalized_result(result)
    proposal = {'summary': 'display plan', 'steps': [{'args': {'question': 'x' * 65536}}]}
    with pytest.raises(QueueError, match='metadata_too_large'):
        system.queue.set_semantic_plan_proposal(system.task['id'], proposal)


def test_retry_count_changes_invalidate_triage_even_when_state_and_reason_return(system):
    model = Model('triage')
    def explain_facts(messages, tools):
        model.calls += 1
        facts = json.loads(messages[-1]['content'])
        return SimpleNamespace(message={'content': '当前执行尝试 ' + str(facts['execution_attempts'])},
            finish_reason='stop', usage={'total_tokens': 120, 'input_tokens': 80, 'output_tokens': 40})
    model.complete = explain_facts
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        first = client.post(url + '/triage', json={}).json()
        system.queue.retry_unsent(system.task['id'])
        claim = AssistantQueue.claim_next(system.queue, 'execute', run_id='count-fixture')
        system.queue.finish(claim['id'], claim['execution_token'], 'execute', {
            'state': 'needs_review', 'reason_code': system.task['reason_code'],
            'reply_state': system.task['reply_state'], 'details': system.task['details']})
        changed = client.get(url).json()
        second = client.post(url + '/triage', json={}).json()
    assert changed['state'] == system.task['state'] and changed['reason_code'] == system.task['reason_code']
    assert changed['execution_attempts'] == 2 and 'semantic_triage' not in changed['details']
    assert first['context_key'] != second['context_key'] and second['cached'] is False
    assert '尝试 2' in second['text'] and model.calls == 2


@pytest.mark.parametrize('details', [None, [], {'library_choice': None},
    {'library_choice': []}, {'library_choice': 'old'}])
def test_waiting_library_legacy_details_support_normal_get_and_triage(system, details):
    with system.queue._tx() as db:
        db.execute('UPDATE assistant_tasks SET state=?,reply_state=?,details=? WHERE id=?',
            ('waiting_library', 'b_view_visible', json.dumps(details), system.task['id']))
    model = Model('triage')
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        detail = client.get(url)
        listing = client.get('/api/assistant/tasks')
        explanation = client.post(url + '/triage', json={})
        refreshed = client.get(url)
    assert detail.status_code == listing.status_code == explanation.status_code == refreshed.status_code == 200
    assert detail.json()['state'] == refreshed.json()['state'] == 'waiting_library'
    assert isinstance(detail.json()['details'], dict)
    assert explanation.json()['text'] and model.calls == 1


@pytest.mark.parametrize('details', [None, []])
def test_plan_generation_rechecks_legacy_current_details_without_read_error(system, details):
    model = Model('plan-proposal')
    original_complete = model.complete
    def complete(messages, tools):
        result = original_complete(messages, tools)
        with system.queue._tx() as db:
            db.execute('UPDATE assistant_tasks SET details=? WHERE id=?',
                (json.dumps(details), system.task['id']))
        return result
    model.complete = complete
    system.monkeypatch.setattr(semantic_intent_model, 'default_semantic_model', lambda: model)
    with client_for(system) as client:
        url = '/api/assistant/tasks/' + system.task['id']
        plan = client.post(url + '/plan-proposal', json={})
        detail = client.get(url)
    assert plan.status_code == detail.status_code == 200
    assert plan.json()['proposal']['fallback'] is False
    assert detail.json()['details']['semantic_plan_proposal'] == plan.json()['proposal']
    assert model.calls == 1
