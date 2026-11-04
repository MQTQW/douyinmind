"""Real TEMP SQLite contention and inert callbacks, never real service shutdown."""
from contextlib import contextmanager
import sqlite3
import threading
import time

import pytest

from app.services.worker import Worker, WorkerBusyError


@pytest.mark.parametrize('mode', ['EXCLUSIVE', 'IMMEDIATE'])
def test_sqlite_lock_wait_for_many_queued_jobs_shares_one_total_shutdown_deadline(tmp_path, mode):
    path = tmp_path / 'jobs.sqlite3'
    local = Worker(path)
    calls = []
    for number in range(4):
        local.submit('queued-' + str(number), lambda **_: calls.append('forbidden'),
                     job_metadata={'platform_item_ids': [str(number)]}, job_kind='notes')
    blocker = sqlite3.connect(path, timeout=0)
    try:
        blocker.execute('BEGIN ' + mode)
        start = time.monotonic()
        result = local.stop(timeout=.03)
        elapsed = time.monotonic() - start
        assert elapsed < .5, 'shutdown must not wait per-row 5s SQLite budgets'
        assert result['stopped'] is True and result['timed_out'] is True
        assert result['state_recorded'] is False and result['pending_count'] == 4
        assert calls == []
    finally:
        blocker.rollback(); blocker.close()
    assert local.activity_snapshot()['maintenance_busy'] is True
    with pytest.raises(WorkerBusyError):
        local.start()
    reopened = Worker(path)
    try:
        reopened.start()
        assert {r['status'] for r in reopened.list_tasks(kind='notes')} == {'needs_review'}
        assert calls == []
    finally:
        reopened.stop(timeout=2)


@pytest.mark.parametrize('lock_name', ['_transition_lock', '_lifecycle_lock', '_lock', '_stop_state_lock'])
def test_shutdown_lock_contention_uses_remaining_budget_and_sets_stop_fence(tmp_path, lock_name):
    local = Worker(tmp_path / 'jobs.sqlite3')
    calls = []
    local.submit('queued', lambda **_: calls.append('forbidden'))
    entered, release = threading.Event(), threading.Event()
    def hold():
        with getattr(local, lock_name):
            entered.set(); assert release.wait(3)
    thread = threading.Thread(target=hold)
    thread.start(); assert entered.wait(1)
    try:
        started = time.monotonic()
        result = local.stop(timeout=.02)
        assert time.monotonic() - started < .5
        assert result['stopped'] and result['timed_out'] and result['state_recorded'] is False
        assert local._stop_requested.is_set() and calls == []
    finally:
        release.set(); thread.join(2)
        local.stop(timeout=2)


def test_zero_budget_does_not_run_or_claim_unrecorded_queue_completed(tmp_path):
    path = tmp_path / 'jobs.sqlite3'
    local = Worker(path)
    calls = []
    local.submit('queued', lambda **_: calls.append('forbidden'), job_metadata={'platform_item_ids': ['v']})
    result = local.stop(timeout=0)
    assert result['stopped'] is True and result['timed_out'] is True and result['state_recorded'] is False
    assert Worker(path).get_progress('queued')['status'] == 'queued'
    assert calls == []
    reopened = Worker(path)
    try:
        reopened.start()
        assert reopened.get_progress('queued')['status'] == 'needs_review' and calls == []
    finally:
        reopened.stop(timeout=2)


def test_readonly_stopping_signal_does_not_initialize_configuration_or_storage():
    local = Worker(ledger_path_factory=lambda: pytest.fail('signal must not resolve storage'))
    assert local.is_stopping() is False
    assert local.stop(timeout=0)['stopped'] is True
    assert local.is_stopping() is True and local._ledger is None


def test_late_confirmed_native_cleanup_does_not_report_the_deadline_was_met(monkeypatch):
    local = Worker()
    def late_release(*, deadline=None, **_expected):
        time.sleep(.03)  # A native close/fsync cannot be force-cancelled by Python.
    monkeypatch.setattr(local, '_release_owner', late_release)
    result = local.stop(timeout=.005)
    assert result['stopped'] is True and result['timed_out'] is True


def test_budget_spent_after_first_record_does_not_start_more_queued_sql_or_callbacks(tmp_path, monkeypatch):
    path = tmp_path / 'jobs.sqlite3'
    local = Worker(path)
    calls, writes = [], []
    for number in range(3):
        local.submit('queued-' + str(number), lambda **_: calls.append('forbidden'))
    ledger = local._get_ledger()
    finish = ledger.finish
    def first_slow(*args, **kwargs):
        value = finish(*args, **kwargs)
        writes.append(args[0])
        time.sleep(.3)
        return value
    monkeypatch.setattr(ledger, 'finish', first_slow)
    result = local.stop(timeout=.25)
    assert result['stopped'] and result['timed_out'] and result['state_recorded'] is False
    assert writes == ['queued-0'] and calls == []
    records = Worker(path).list_tasks()
    assert {r['task_id']: r['status'] for r in records} == {
        'queued-0': 'needs_review', 'queued-1': 'queued', 'queued-2': 'queued'}


def test_job_dequeued_and_ledger_claimed_before_stop_cannot_enter_its_callback(tmp_path, monkeypatch):
    local = Worker(tmp_path / 'jobs.sqlite3')
    ledger = local._get_ledger(create=True, owner=True)
    original = ledger.start_job
    entered, release = threading.Event(), threading.Event()
    calls = []
    def paused(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set(); assert release.wait(3)
        return value
    monkeypatch.setattr(ledger, 'start_job', paused)
    local.submit('claimed', lambda **_: calls.append('forbidden'), job_metadata={'platform_item_ids': ['v']})
    local.start(); assert entered.wait(1)
    result = local.stop(timeout=.02)
    assert result['timed_out'] and result['state_recorded'] is False and calls == []
    release.set(); local._thread.join(2)
    assert not local._thread.is_alive() and calls == []
    assert Worker(tmp_path / 'jobs.sqlite3').get_progress('claimed')['status'] == 'needs_review'


def test_old_stop_cannot_release_a_restarted_owner_or_report_active_new_thread_stopped(tmp_path, monkeypatch):
    path = tmp_path / 'jobs.sqlite3'
    local, other = Worker(path), Worker(path)
    paused, allow, entered, release = [threading.Event() for _ in range(4)]
    original = local._release_owner
    outcome, errors = {}, []
    def release_owner(*args, **kwargs):
        if threading.current_thread().name == 'synthetic-old-stop':
            paused.set(); assert allow.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(local, '_release_owner', release_owner)
    def stop():
        try:
            outcome.update(local.stop(timeout=2))
        except BaseException as error:
            errors.append(type(error).__name__)
    stopping = threading.Thread(target=stop, name='synthetic-old-stop')
    local.start()
    try:
        stopping.start(); assert paused.wait(2)
        refused = False
        try:
            local.start()
        except WorkerBusyError:
            refused = True
        if refused:
            allow.set(); stopping.join(2)
            assert not stopping.is_alive() and not errors and outcome['stopped'] is True
            local.start()
        local.submit('new-live-notes', lambda **_: (entered.set(), release.wait(3)),
                     job_kind='notes', job_metadata={'platform_item_ids': ['synthetic-video']})
        assert entered.wait(1)
        allow.set(); stopping.join(2)
        assert not stopping.is_alive() and not errors
        if not refused:
            assert not (outcome['stopped'] and not outcome['timed_out'])
        with pytest.raises(Exception, match='owner_busy'):
            other.start()
        assert other.get_progress('new-live-notes')['status'] == 'running'
        release.set(); local._thread.join(.01)
        end = time.monotonic() + 2
        while local.get_progress('new-live-notes')['status'] != 'done' and time.monotonic() < end:
            time.sleep(.005)
        assert local.get_progress('new-live-notes')['status'] == 'done'
        assert local.stop(timeout=2)['stopped'] is True
        other.start()
        assert other.get_progress('new-live-notes')['status'] == 'done'
    finally:
        allow.set(); release.set(); stopping.join(2)
        local.stop(timeout=2); other.stop(timeout=2)


def test_start_aborted_after_owner_acquisition_returns_only_its_unstarted_owner(tmp_path, monkeypatch):
    path = tmp_path / 'jobs.sqlite3'
    local, other = Worker(path), Worker(path)
    paused, allow = threading.Event(), threading.Event()
    original, errors = local._get_ledger, []
    def get_ledger(*args, **kwargs):
        value = original(*args, **kwargs)
        if threading.current_thread().name == 'synthetic-start' and kwargs.get('owner'):
            paused.set(); assert allow.wait(3)
        return value
    monkeypatch.setattr(local, '_get_ledger', get_ledger)
    def start():
        try:
            local.start()
        except BaseException as error:
            errors.append(type(error).__name__)
    starting = threading.Thread(target=start, name='synthetic-start')
    try:
        starting.start(); assert paused.wait(2)
        result = local.stop(timeout=.01)
        assert result['timed_out'] and result['state_recorded'] is False
        allow.set(); starting.join(2)
        assert not starting.is_alive() and errors == ['WorkerBusyError']
        assert not local._thread.is_alive() and local._owner_lock is None
        other.start()
        assert other._thread.is_alive() and other.count_tasks() == 0
    finally:
        allow.set(); starting.join(2)
        local.stop(timeout=2); other.stop(timeout=2)


def test_cold_admission_paused_before_ledger_cannot_acquire_owner_after_stop_returns(tmp_path, monkeypatch):
    path = tmp_path / 'jobs.sqlite3'
    local, other = Worker(path), Worker(path)
    paused, allow = threading.Event(), threading.Event()
    original, errors, calls = local._get_ledger, [], []
    def get_ledger(*args, **kwargs):
        if threading.current_thread().name == 'cold-submitter' and kwargs.get('owner'):
            paused.set(); assert allow.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(local, '_get_ledger', get_ledger)
    def submit():
        try:
            local.submit('cold-old', lambda **_: calls.append('forbidden'), job_kind='notes',
                         job_metadata={'platform_item_ids': ['synthetic-video']})
        except BaseException as error:
            errors.append(type(error).__name__)
    submitting = threading.Thread(target=submit, name='cold-submitter')
    try:
        submitting.start(); assert paused.wait(1)
        outcome = local.stop(timeout=.01)
        assert outcome['stopped'] and outcome['timed_out'] and outcome['state_recorded'] is False
        allow.set(); submitting.join(2)
        assert not submitting.is_alive() and errors == ['WorkerBusyError']
        assert local._owner_lock is None and not local._thread.is_alive() and local._queue.empty()
        assert local.get_progress('cold-old') is None and calls == []
        other.start()
        assert other.count_tasks() == 0 and other.get_progress('cold-old') is None
    finally:
        allow.set(); submitting.join(2)
        local.stop(timeout=2); other.stop(timeout=2)


def test_an_old_admission_cannot_revive_after_stop_then_a_legitimate_new_start(tmp_path):
    local = Worker(tmp_path / 'jobs.sqlite3')
    paused, allow, entered, release = [threading.Event() for _ in range(4)]
    real = local._transition_lock
    errors, old_calls, new_calls = [], [], []
    class PauseOldAdmission:
        def __enter__(self):
            if threading.current_thread().name == 'old-admission':
                paused.set(); assert allow.wait(3)
            real.acquire(); return self
        def __exit__(self, *_):
            real.release()
        def acquire(self, *args, **kwargs):
            return real.acquire(*args, **kwargs)
        def release(self):
            return real.release()
    local._transition_lock = PauseOldAdmission()
    def old_submit():
        try:
            local.submit('old-epoch', lambda **_: old_calls.append('forbidden'),
                         job_metadata={'platform_item_ids': ['old-video']}, job_kind='notes')
        except BaseException as error:
            errors.append(type(error).__name__)
    old = threading.Thread(target=old_submit, name='old-admission')
    try:
        old.start(); assert paused.wait(1)
        outcome = local.stop(timeout=1)
        assert outcome['stopped'] and not outcome['timed_out']
        local.start()
        def current(*, task_id):
            new_calls.append(task_id); entered.set(); assert release.wait(3)
        local.submit('new-epoch', current, job_metadata={'platform_item_ids': ['new-video']}, job_kind='notes')
        assert entered.wait(1)
        allow.set(); old.join(2)
        assert not old.is_alive() and errors == ['WorkerBusyError']
        assert local.get_progress('old-epoch') is None and old_calls == []
        assert local.get_progress('new-epoch')['status'] == 'running' and new_calls == ['new-epoch']
    finally:
        allow.set(); release.set(); old.join(2); local.stop(timeout=2)


@pytest.mark.parametrize('kind,timed_out', [('lock_timeout', True), ('sqlite_busy', True), ('other_failure', False)])
def test_budget_wait_failure_is_explicit_even_if_clock_rounding_returns_before_deadline(monkeypatch, kind, timed_out):
    local = Worker()
    if kind == 'lock_timeout':
        error = TimeoutError('early-rounded-lock-timeout')
    elif kind == 'sqlite_busy':
        error = sqlite3.OperationalError('database is locked')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    else:
        error = OSError('synthetic non-deadline persistence failure')
    def fail(**_):
        raise error
    monkeypatch.setattr(local, '_quarantine_queued', fail)
    start = time.monotonic()
    outcome = local.stop(timeout=2)
    assert time.monotonic() - start < .5
    assert outcome['stopped'] and outcome['state_recorded'] is False
    assert outcome['timed_out'] is timed_out
