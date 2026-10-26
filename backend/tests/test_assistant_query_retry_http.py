"""HTTP dispatch/public projection only; retry state-machine tests use real temp stores elsewhere."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.assistant import routes
from app.assistant.queue import QueueError


TASK_ID = 'a' * 32
PRIVATE = 'SYNTHETIC_PRIVATE_EXECUTION_PROOF'


class Queue:
    def __init__(self):
        self.requests = []
        self.task = {'id': TASK_ID, 'kind': 'query', 'state': 'needs_review',
            'reason_code': 'knowledge_query_failed', 'authorization': {'private': PRIVATE},
            'execution_token': PRIVATE}

    def get_task(self, task_id):
        return self.task if task_id == TASK_ID else None

    def request_action(self, task_id, action):
        self.requests.append((task_id, action))
        return {'id': 'intent', 'action': action, 'state': 'queued'}

    def public_task(self, task, *, detail=False):
        return {key: task[key] for key in ['id', 'kind', 'state', 'reason_code']}


class Runtime:
    def __init__(self, queue, error=None):
        self.queue, self.error, self.calls = queue, error, []

    def retry_query_task(self, task_id):
        self.calls.append(task_id)
        if self.error:
            raise self.error
        return self.queue.get_task(task_id)


def client_for(queue, runtime=None):
    service = routes.AssistantAPI(queue, control=object(), runtime=runtime)
    app = FastAPI()
    app.dependency_overrides[routes.get_service] = lambda: service
    app.include_router(routes.router, prefix='/api')
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))


def test_retry_http_dispatches_explicit_read_retry_and_never_exposes_private_proofs():
    queue = Queue()
    runtime = Runtime(queue)
    with client_for(queue, runtime) as client:
        response = client.post(f'/api/assistant/tasks/{TASK_ID}/retry-query', json={})
    assert response.status_code == 200
    assert runtime.calls == [TASK_ID]
    assert response.json()['task']['id'] == TASK_ID
    assert PRIVATE not in response.text
    assert 'execution_token' not in response.text
    assert 'authorization' not in response.text


def test_retry_http_queues_only_retry_action_when_runtime_is_separate_process():
    queue = Queue()
    with client_for(queue) as client:
        response = client.post(f'/api/assistant/tasks/{TASK_ID}/retry-query', json={})
    assert response.status_code == 200
    assert queue.requests == [(TASK_ID, 'retry_query')]
    assert response.json()['accepted'] is True
    assert PRIVATE not in response.text


@pytest.mark.parametrize('origin', ['https://example.invalid', 'null'])
def test_foreign_origin_cannot_trigger_a_model_retry(origin):
    queue = Queue()
    runtime = Runtime(queue)
    with client_for(queue, runtime) as client:
        response = client.post(f'/api/assistant/tasks/{TASK_ID}/retry-query', json={}, headers={'Origin': origin})
    assert response.status_code == 403
    assert runtime.calls == []


def test_http_cannot_supply_its_own_retry_or_authorization_proofs():
    queue = Queue()
    runtime = Runtime(queue)
    with client_for(queue, runtime) as client:
        response = client.post(f'/api/assistant/tasks/{TASK_ID}/retry-query',
            json={'can_retry_query': True, 'execution_token': PRIVATE})
    assert response.status_code == 422
    assert runtime.calls == []


def test_retry_http_failure_preserves_fixed_message_and_does_not_return_exception_text():
    queue = Queue()
    runtime = Runtime(queue, QueueError(PRIVATE))
    with client_for(queue, runtime) as client:
        response = client.post(f'/api/assistant/tasks/{TASK_ID}/retry-query', json={})
    assert response.status_code != 200
    assert runtime.calls == [TASK_ID]
    assert PRIVATE not in response.text


def test_retry_action_status_is_not_mislabeled_as_generic_resume_or_completion():
    value = routes.public_action({'id': 'intent', 'action': 'retry_query', 'state': 'done',
        'reason_code': PRIVATE, 'metadata': {'private': PRIVATE}})
    assert value['action'] == 'retry_query'
    assert value['state'] == 'done'
    assert '问答重试请求' in value['message']
    assert PRIVATE not in str(value)
