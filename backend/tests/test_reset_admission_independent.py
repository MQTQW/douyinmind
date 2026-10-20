"""Independent admission/reset protocol checks; cold local Worker, fake DB only.

No worker thread is started, no task callable executes, and no database is opened.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import inspect
import queue
import threading
from types import SimpleNamespace

import pytest

from app.api.routes import knowledge as routes
from app.services import knowledge_service as service_module
from app.services.knowledge_service import KnowledgeService
from app.services.worker import Worker

BUSY = (ValueError, RuntimeError)


@pytest.fixture
def cold(monkeypatch):
    local = Worker()
    assert not local._thread.is_alive()
    monkeypatch.setattr(service_module, 'worker', local)
    monkeypatch.setattr(routes, 'worker', local)
    yield local
    assert not local._thread.is_alive(), 'These tests must never start a real worker'


def unlocked(worker):
    assert worker._lock.acquire(blocking=False), 'DB/guard body must not hold worker lock'
    worker._lock.release()


def submission_active(worker):
    unlocked(worker)
    with pytest.raises(BUSY):
        with worker.maintenance_guard():
            pytest.fail('Submission reservation must block maintenance until DB is finished')


def maintenance_active(worker):
    unlocked(worker)
    with pytest.raises(BUSY):
        with worker.submission_guard():
            pytest.fail('Maintenance must reject new admission before DB work')


class FakeDB:
    def __init__(self, worker, rows=(), *, failure=None):
        self.worker, self.rows, self.failure = worker, list(rows), failure
        self.events = []
    def scalars(self, statement):
        self.events.append('read')
        submission_active(self.worker)
        if self.failure == 'read':
            raise RuntimeError('synthetic read failure')
        return SimpleNamespace(all=lambda: self.rows)
    def commit(self):
        self.events.append('commit')
        submission_active(self.worker)
        if self.failure == 'commit':
            raise RuntimeError('synthetic commit failure')
    def rollback(self):
        self.events.append('rollback')
        submission_active(self.worker)


def call_reset(db):
    result = routes.reset_failed_videos(db=db)
    return asyncio.run(result) if inspect.isawaitable(result) else result


def test_maintenance_is_fail_fast_for_both_admission_and_direct_submit(cold):
    with cold.maintenance_guard():
        unlocked(cold)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def attempt():
                with pytest.raises(BUSY):
                    with cold.submission_guard():
                        pytest.fail('must not enter admission')
                with pytest.raises(BUSY):
                    cold.submit('blocked', lambda **kwargs: pytest.fail('never execute'))
            pool.submit(attempt).result(timeout=1)
    assert cold.get_progress('blocked') is None and cold._queue.empty()
    with cold.submission_guard():
        unlocked(cold)


def test_queued_registry_blocks_reset_after_pop_before_current_is_set(cold):
    cold.submit('queued', lambda **kwargs: pytest.fail('No tasks execute in this test'))
    item = cold._queue.get_nowait()
    assert item[0] == 'queued' and cold.get_current_task() is None
    assert cold._queue.empty(), 'qsize/current check alone would miss this transition'
    with pytest.raises(BUSY):
        with cold.maintenance_guard():
            pytest.fail('queued registry must close pop-to-current window')
    cold._queue.task_done()


def test_duplicate_task_id_is_rejected_without_second_publication(cold):
    cold.submit('unique', lambda **kwargs: None)
    first = dict(cold.get_progress('unique'))
    with pytest.raises(BUSY):
        cold.submit('unique', lambda **kwargs: None)
    assert cold._queue.qsize() == 1 and cold.get_progress('unique') == first


def test_queue_publication_failure_does_not_leave_phantom_queued_state(cold, monkeypatch):
    def fail(item):
        assert cold._tasks.get('publish-failed', {}).get('status') == 'queued', 'register before publication'
        raise queue.Full('synthetic queue refusal before publication')
    monkeypatch.setattr(cold._queue, 'put_nowait', fail)
    with pytest.raises(Exception):
        cold.submit('publish-failed', lambda **kwargs: None)
    progress = cold.get_progress('publish-failed')
    assert progress is None or progress['status'] not in ('queued', 'running')
    with cold.maintenance_guard():
        unlocked(cold)


@pytest.mark.parametrize('guard_name', ['submission_guard', 'maintenance_guard'])
@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_guards_release_flags_on_normal_and_base_exception_exits(cold, guard_name, error):
    with pytest.raises(error):
        with getattr(cold, guard_name)():
            unlocked(cold)
            raise error('synthetic body failure')
    with cold.maintenance_guard():
        unlocked(cold)
    with cold.submission_guard():
        unlocked(cold)


def test_no_pending_no_base_branch_finishes_db_transaction_before_release(cold):
    db = FakeDB(cold)
    result = KnowledgeService().start_sync(db)
    assert result['task_id'] is None
    assert db.events[0] == 'read' and db.events[-1] in ('commit', 'rollback')
    with cold.maintenance_guard():
        unlocked(cold)


def test_existing_base_done_video_assignment_is_reserved_until_all_commits(cold, monkeypatch):
    video = SimpleNamespace(platform_item_id='synthetic-video', status='done')
    db = FakeDB(cold, [video])
    def require(db, base):
        db.events.append('require-base');submission_active(cold)
    def assign(db, base, selected):
        db.events.append('assign');submission_active(cold)
        return {'added_count': 1, 'existing_count': 0}
    monkeypatch.setattr(service_module.knowledge_base_service, 'require_base', require)
    monkeypatch.setattr(service_module.knowledge_base_service, 'assign_videos', assign)
    result = KnowledgeService().start_sync(db, ['synthetic-video'], 1)
    assert result['task_id'] is None and result['assigned_count'] == 1
    assert db.events[-1] == 'commit'
    with cold.maintenance_guard():
        unlocked(cold)


@pytest.mark.parametrize('failure', ['read', 'commit'])
def test_submission_db_errors_roll_back_inside_reservation_and_release(cold, failure):
    db = FakeDB(cold, failure=failure)
    with pytest.raises(RuntimeError):
        KnowledgeService().start_sync(db)
    assert db.events[-1] == 'rollback'
    with cold.maintenance_guard():
        unlocked(cold)


@pytest.mark.parametrize('failure', ['configuration', 'submit'])
def test_configuration_or_submit_failure_releases_admission_after_rollback(cold, monkeypatch, failure):
    video = SimpleNamespace(platform_item_id='synthetic-pending', status='pending')
    db = FakeDB(cold, [video])
    def fail(*args, **kwargs):
        submission_active(cold)
        raise RuntimeError('synthetic controlled refusal')
    monkeypatch.setattr(service_module.omni_service, 'check_configuration',
                        fail if failure == 'configuration' else lambda: None)
    if failure == 'submit':
        monkeypatch.setattr(cold, 'submit', fail)
    with pytest.raises(RuntimeError):
        KnowledgeService().start_sync(db)
    assert db.events[-1] == 'rollback'
    assert cold._queue.empty()
    with cold.maintenance_guard():
        unlocked(cold)


def test_reservation_is_visible_while_first_db_read_is_paused(cold):
    started, release = threading.Event(), threading.Event()
    db = FakeDB(cold)
    original = db.scalars
    def paused(statement):
        result = original(statement)
        started.set()
        assert release.wait(timeout=3)
        return result
    db.scalars = paused
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(KnowledgeService().start_sync, db)
        try:
            assert started.wait(timeout=1)
            with pytest.raises(BUSY):
                with cold.maintenance_guard():
                    pytest.fail('reset must not enter between query and publication')
            unlocked(cold)
        finally:
            release.set()
        assert future.result(timeout=2)['task_id'] is None
    with cold.maintenance_guard():
        pass


def test_reset_guard_covers_commit_and_rejects_submission_before_any_db_access(cold):
    entered_commit, release = threading.Event(), threading.Event()
    class ResetDB:
        def execute(self, statement):
            maintenance_active(cold)
            return SimpleNamespace(rowcount=2)
        def commit(self):
            maintenance_active(cold)
            entered_commit.set()
            assert release.wait(timeout=3)
        def rollback(self):
            maintenance_active(cold)
    forbidden_db = SimpleNamespace(scalars=lambda *args: pytest.fail('maintenance must reject before DB'))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(call_reset, ResetDB())
        try:
            assert entered_commit.wait(timeout=1)
            with pytest.raises(BUSY):
                KnowledgeService().start_sync(forbidden_db)
            with pytest.raises(BUSY):
                cold.submit('blocked-during-commit', lambda **kwargs: None)
            assert cold.get_current_task() is None
            unlocked(cold)
        finally:
            release.set()
        result = future.result(timeout=2)
    assert result['success'] is True and result['reset_count'] == 2
    with cold.submission_guard():
        pass


@pytest.mark.parametrize('failure', ['execute', 'commit'])
def test_reset_sql_errors_roll_back_before_maintenance_flag_is_cleared(cold, failure):
    events = []
    class ResetDB:
        def execute(self, statement):
            events.append('execute');maintenance_active(cold)
            if failure == 'execute':
                raise RuntimeError('synthetic reset execute failure')
            return SimpleNamespace(rowcount=1)
        def commit(self):
            events.append('commit');maintenance_active(cold)
            raise RuntimeError('synthetic reset commit failure')
        def rollback(self):
            events.append('rollback');maintenance_active(cold)
    try:
        result = call_reset(ResetDB())
    except RuntimeError:
        pass
    else:
        assert result['success'] is False, 'SQL error must not claim reset succeeded'
    assert events[-1] == 'rollback'
    with cold.submission_guard():
        pass


def test_submission_rollback_failure_still_releases_reservation(cold):
    db = FakeDB(cold, failure='read')
    def rollback():
        submission_active(cold)
        raise RuntimeError('synthetic rollback itself failed')
    db.rollback = rollback
    with pytest.raises(RuntimeError, match='rollback itself failed'):
        KnowledgeService().start_sync(db)
    with cold.maintenance_guard():
        unlocked(cold)


def test_reset_rollback_failure_still_releases_maintenance(cold):
    class ResetDB:
        def execute(self, statement):
            maintenance_active(cold)
            raise RuntimeError('synthetic SQL failure')
        def rollback(self):
            maintenance_active(cold)
            raise RuntimeError('synthetic reset rollback itself failed')
    with pytest.raises(RuntimeError, match='reset rollback itself failed'):
        call_reset(ResetDB())
    with cold.submission_guard():
        unlocked(cold)
