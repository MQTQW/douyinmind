"""Budget admission around the existing model, without storing model inputs."""
from datetime import datetime, timezone
import json
import secrets
import threading

from app.mention_agent.contracts import canonical_json
from .usage_budget import UsageBudgetExceeded


class ModelBudgetError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in {'model_budget_exceeded', 'model_budget_unknown'} else 'model_budget_unknown'
        super().__init__(self.code)


class BudgetedModel:
    def __init__(self, model, budget, task_id):
        self.model, self.budget, self.task_id = model, budget, task_id
        self._prepared = None
        self._prepared_payload = None
        self._inflight = False
        self._poisoned = False
        self._lock = threading.Lock()
        self.request_key = None

    def preflight(self, messages, tools):
        with self._lock:
            return self._preflight(messages, tools)

    def _preflight(self, messages, tools):
        if self._prepared is not None or self._inflight or self._poisoned:
            raise ModelBudgetError('model_budget_unknown')
        # Deliberately conservative admission estimate. It is not a tokenizer,
        # provider guarantee or currency price; real usage replaces it later.
        payload = canonical_json({'messages': messages, 'tools': tools}).encode('utf-8')
        estimate = len(payload) + 1500 + 2048
        key = 'live-' + secrets.token_hex(24)
        try:
            row = self.budget.reserve(self.task_id, key, estimate)
        except UsageBudgetExceeded as error:
            code = 'model_budget_unknown' if error.code.endswith('usage_unknown') else 'model_budget_exceeded'
            raise ModelBudgetError(code) from None
        except Exception:
            raise ModelBudgetError('model_budget_unknown') from None
        if row.get('may_request') is not True:
            raise ModelBudgetError('model_budget_unknown')
        self._prepared = key
        self._prepared_payload = payload
        self.request_key = key

    def complete(self, messages, tools):
        with self._lock:
            key, payload = self._prepared, self._prepared_payload
            if key is None or self._inflight or self._poisoned:
                raise ModelBudgetError('model_budget_unknown')
            # Consume admission before touching provider code. Two callers or
            # an exception cannot reuse this reservation for a second request.
            self._prepared = self._prepared_payload = None
            self._inflight = True
        try:
            if canonical_json({'messages': messages, 'tools': tools}).encode('utf-8') != payload:
                raise ModelBudgetError('model_budget_unknown')
            frozen = json.loads(payload)
            # Only this frozen copy is sent, even if caller containers change
            # after validation. Prompts and payload hashes are not persisted.
            turn = self.model.complete(frozen['messages'], frozen['tools'])
            try:
                settled = self.budget.settle(key, turn.usage)
            except Exception:
                raise ModelBudgetError('model_budget_unknown') from None
            if settled['state'] != 'settled':
                with self._lock: self._poisoned = True
            return turn
        except Exception:
            with self._lock: self._poisoned = True
            try: self.budget.mark_unknown(key, reason='usage_invalid')
            except Exception: pass
            raise
        finally:
            with self._lock: self._inflight = False


def sync_existing_usage(budget, store, *, trusted_scope_for_task=None):
    """Import old totals once; never release unconfirmed reservations on restart."""
    if trusted_scope_for_task is not None and not callable(trusted_scope_for_task):
        raise ModelBudgetError('model_budget_unknown')

    def scope_matches(row, task):
        if row is None:
            return False
        if row['task_id'] == task['task_id']:
            return True  # Old tasks retain their original strict identity.
        if trusted_scope_for_task is None:
            return False
        try:
            from app.mention_agent.contracts import Binding
            raw_binding = store.get_binding(task['binding_id'])
            binding = Binding.from_dict(raw_binding) if raw_binding is not None else None
            # This checks an already-recorded reservation, never authorizes a
            # new provider request. Only the issuer may validate the narrow
            # historical active=True -> current inactive transition.
            scope = trusted_scope_for_task(task['task_id'], envelope=task['envelope'], binding=binding,
                                           for_accounting=True)
            return type(scope) is str and scope == row['task_id']
        except Exception:
            return False
    response_count = 0
    for task in store.list_tasks():
        task_id = task['task_id']
        pending = []
        for step in store.list_steps(task_id):
            if step['kind'] == 'model_request':
                pending.append(step)
                continue
            if step['kind'] != 'model_response': continue
            request = pending.pop(0) if pending else None
            metadata = step.get('metadata', {})
            request_metadata = (request or {}).get('metadata', {})
            request_key = request_metadata.get('budget_request_id')
            response_key = metadata.get('budget_request_id')
            if request_key and response_key and request_key != response_key:
                raise ModelBudgetError('model_budget_unknown')
            if request is not None and ('round' in request_metadata or 'round' in metadata):
                if (type(request_metadata.get('round')) is not int
                        or request_metadata['round'] < 1
                        or request_metadata.get('round') != metadata.get('round')):
                    raise ModelBudgetError('model_budget_unknown')
            budget_key = request_key or response_key
            if budget_key:
                if request is None: raise ModelBudgetError('model_budget_unknown')
                row = budget.get_request(budget_key)
                if not scope_matches(row, task):
                    raise ModelBudgetError('model_budget_unknown')
                budget.settle(budget_key, metadata.get('usage'))
            else:
                identity = f'request-{request["id"]}' if request else f'orphan-response-{step["id"]}'
                key = f'historical-{task_id}-{identity}'
                happened = datetime.fromtimestamp((request or step)['created_at'], timezone.utc)
                budget.import_historical_usage(task_id, key, metadata.get('usage'), happened, outcome='completed')
            response_count += 1
        for request in pending:
            key = request.get('metadata', {}).get('budget_request_id')
            if key:
                row = budget.get_request(key)
                if not scope_matches(row, task): raise ModelBudgetError('model_budget_unknown')
                budget.mark_unknown(key, reason='request_interrupted')
            else:
                budget.import_historical_usage(task_id, f'historical-{task_id}-request-{request["id"]}', None,
                    datetime.fromtimestamp(request['created_at'], timezone.utc), outcome='unknown')
    return {'historical_responses_checked': response_count}
