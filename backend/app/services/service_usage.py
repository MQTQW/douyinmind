"""Bounded, process-local service observations; never prompts or an invoice.

Only provider-reported token counters are added. Missing usage and uncertain
outcomes remain explicit; no money, zero-cost or delivery claim is inferred.
"""
from collections import deque
from copy import deepcopy
import math
import threading
import time
import uuid


TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens')


def reported_usage(response):
    try:
        value = getattr(response, 'usage', None)
        result = {}
        for key in TOKEN_FIELDS:
            number = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
            if type(number) is int and 0 <= number <= 10 ** 12:
                result[key] = number
        return result
    except Exception:
        return {}


class UsageJournal:
    """Thread-safe snapshots. A failing optional observer cannot affect work."""

    def __init__(self, sink=None, *, limit=100):
        if sink is not None and not callable(sink):
            raise ValueError('invalid_usage_sink')
        self._sink = sink
        self._rows = deque(maxlen=limit)
        self._lock = threading.Lock()

    def append(self, observation):
        snapshot = deepcopy(observation)
        with self._lock:
            self._rows.append(snapshot)
        if self._sink is not None:
            try:
                self._sink(deepcopy(snapshot))
            except Exception:
                pass

    def recent(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_usage_observation_limit')
        with self._lock:
            return deepcopy(list(self._rows)[-limit:])


class CallObservation:
    """One logical call, whose individual SDK requests are counted separately."""

    def __init__(self, operation, journal, *, clock=None, budget_seconds=None, batches=1):
        self.clock = clock or time.perf_counter
        self.started = self.clock()
        self.journal = journal
        self.finished = False
        self.data = {
            'schema_version': 1, 'call_id': uuid.uuid4().hex, 'operation': operation,
            'budget_seconds': budget_seconds, 'total_batches': batches, 'completed_batches': 0,
            'request_attempts': 0, 'requests_without_usage': 0, 'known_usage': {},
            'requests_with_incomplete_usage': 0,
            'unknown_outcome': False, 'attempts': [],
            'scope': 'process_local_observed_tokens_only_not_invoice',
        }

    def attempt(self, *, batch_index, started, outcome, response=None, status=None):
        usage = reported_usage(response) if response is not None else {}
        elapsed = max(0.0, self.clock() - started)
        row = {'ordinal': self.data['request_attempts'] + 1, 'batch_index': batch_index,
               'elapsed_seconds': round(elapsed, 6), 'outcome': outcome, 'known_usage': usage}
        if type(status) is int and 100 <= status <= 599:
            row['http_status'] = status
        self.data['request_attempts'] += 1
        self.data['requests_without_usage'] += not bool(usage)
        required = {'prompt_tokens', 'total_tokens'} if self.data['operation'].startswith('embed_') else set(TOKEN_FIELDS)
        self.data['requests_with_incomplete_usage'] += not required.issubset(usage)
        self.data['unknown_outcome'] |= outcome in {'unknown', 'invalid_response', 'interrupted'}
        for key, value in usage.items():
            self.data['known_usage'][key] = self.data['known_usage'].get(key, 0) + value
        # Keep aggregate counters exact while bounding per-request detail.
        if len(self.data['attempts']) < 128:
            self.data['attempts'].append(row)

    def complete_batch(self):
        self.data['completed_batches'] += 1

    def finish(self, outcome, reason):
        if self.finished:
            return
        self.finished = True
        elapsed = self.clock() - self.started
        self.data.update(outcome=outcome, stop_reason=reason,
                         elapsed_seconds=round(max(0.0, elapsed), 6) if math.isfinite(elapsed) else None,
                         usage_complete=(self.data['request_attempts'] > 0
                                         and self.data['requests_with_incomplete_usage'] == 0
                                         and not self.data['unknown_outcome']))
        self.journal.append(self.data)
