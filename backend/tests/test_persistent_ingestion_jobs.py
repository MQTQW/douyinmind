"""Temporary ledgers and inert callbacks only; no provider, media, UI or user DB."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from app.services.ingestion_jobs import IngestionJobError, IngestionJobLedger
from app.services.worker import Worker, WorkerBusyError


META = {"platform_item_ids": ["synthetic-video-1", "synthetic-video-2"],
        "knowledge_base_id": 7, "analysis_mode": "audio"}


def wait_status(worker, task_id, status, *, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        progress = worker.get_progress(task_id)
        if progress is not None and progress["status"] == status:
            return progress
        time.sleep(.01)
    pytest.fail("temporary ingestion task did not reach the expected status")


def test_construction_does_not_resolve_global_configuration_or_create_files(tmp_path):
    def forbidden_configuration():
        pytest.fail("constructor cannot resolve or initialize production state")
    local = Worker(ledger_path_factory=forbidden_configuration)
    explicit = Worker(tmp_path / "new-directory" / "jobs.sqlite3")
    assert not local._thread.is_alive() and not explicit._thread.is_alive()
    assert explicit.get_progress("missing") is None
    assert explicit.list_tasks() == []
    assert list(tmp_path.iterdir()) == []


def test_progress_and_metadata_survive_reopening_without_touching_the_live_owner(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    local = Worker(path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def inert_job(*, task_id):
        calls.append(task_id)
        local.update_progress(task_id, 1, 2, "合成第一项已完成", stage="embedding", item_id=META["platform_item_ids"][0])
        entered.set()
        assert release.wait(4)
        local.update_progress(task_id, 2, 2, "合成第二项已完成", stage="saving", item_id=META["platform_item_ids"][1])

    try:
        local.start()
        local.submit("batch", inert_job, job_metadata=META)
        assert entered.wait(3)
        reader = Worker(path)
        snapshot = reader.get_progress("batch")
        assert snapshot["status"] == "running" and snapshot["progress"] == 1
        assert snapshot["stage"] == "embedding" and snapshot["current_item_id"] == META["platform_item_ids"][0]
        assert snapshot["metadata"] == META and snapshot["usage_state"] == "not_recorded"
        snapshot["metadata"]["platform_item_ids"].append("not-in-the-batch")
        assert local.get_progress("batch")["metadata"] == META
        release.set()
        assert wait_status(local, "batch", "done")["progress"] == 2
        assert local.stop(timeout=3)["stopped"] is True
        reopened = Worker(path)
        assert reopened.get_progress("batch")["status"] == "done"
        reopened.start()
        assert calls == ["batch"]
        assert reopened.list_tasks()[0]["metadata"] == META
        assert reopened.stop(timeout=3)["stopped"] is True
        with IngestionJobLedger(path)._db() as db:
            stages = [row[0] for row in db.execute(
                "SELECT stage FROM ingestion_job_events WHERE task_id='batch' AND event='progress' ORDER BY event_id")]
        assert stages == ["embedding", "saving"]
    finally:
        release.set()
        local.stop(timeout=4)


def test_restart_quarantines_running_and_queued_records_without_replaying(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    ledger = IngestionJobLedger(path, clock=lambda: 1000.0).prepare()
    ledger.register("old-running", "previous-owner", META)
    ledger.start_job("old-running", "previous-owner")
    ledger.progress("old-running", "previous-owner", 1, 2, "旧阶段", stage="transcribing")
    ledger.register("old-queued", "previous-owner", META)
    local = Worker(path, clock=lambda: 2000.0)
    calls = []
    try:
        local.start()
        running = local.get_progress("old-running")
        queued = local.get_progress("old-queued")
        assert running["status"] == "interrupted" and running["stage"] == "transcribing"
        assert running["progress"] == 1 and running["total"] == 2
        assert queued["status"] == "needs_review" and queued["reason_code"] == "worker_restart_queued"
        assert running["needs_review"] is True and queued["needs_review"] is True
        assert running["usage_state"] == queued["usage_state"] == "not_recorded"
        assert local._queue.empty() and calls == []
        old = dict(running)
        local.submit("explicit-new-batch", lambda *, task_id: calls.append(task_id),
                     job_metadata={**META, "recovery_of": "old-running"})
        wait_status(local, "explicit-new-batch", "done")
        assert calls == ["explicit-new-batch"]
        assert local.get_progress("old-running") == old
    finally:
        local.stop(timeout=3)


def test_second_writer_is_refused_but_readonly_history_remains_available(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first, second = Worker(path), Worker(path)
    try:
        first.start()
        with pytest.raises(IngestionJobError, match="owner_busy"):
            second.start()
        assert second.list_tasks() == []
        assert first.stop(timeout=3)["stopped"] is True
        second.start()
        assert second.stop(timeout=3)["stopped"] is True
    finally:
        first.stop(timeout=3)
        second.stop(timeout=3)


def test_zero_budget_reports_unconfirmed_marker_and_retains_owner_until_current_call_finishes(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    local, other = Worker(path), Worker(path)
    entered, release = threading.Event(), threading.Event()

    def inert_job(*, task_id):
        local.update_progress(task_id, 0, 2, "合成调用等待", stage="transcribing")
        entered.set()
        assert release.wait(4)

    try:
        local.start()
        local.submit("slow", inert_job, job_metadata=META)
        assert entered.wait(3)
        outcome = local.stop(timeout=0)
        assert outcome == {"stopped": False, "timed_out": True, "current_task_id": "slow", "pending_count": 0,
                           "state_recorded": False}
        snapshot = Worker(path).get_progress("slow")
        assert snapshot["status"] == "running" and snapshot["stage"] == "transcribing"
        # No budget was available for a durable marker; only this owner can
        # report the unconfirmed write. A read-only reopen must not invent it.
        assert snapshot["shutdown_timed_out"] is False
        assert local.get_progress("slow")["persistence_error"] is True
        assert local.get_progress("slow")["needs_review"] is True
        assert snapshot["progress"] == 0 and snapshot["usage_state"] == "not_recorded"
        with pytest.raises(IngestionJobError, match="owner_busy"):
            other.start()
        release.set()
        done = wait_status(local, "slow", "done")
        assert done["shutdown_timed_out"] is False and done["needs_review"] is False
        assert local.stop(timeout=3)["stopped"] is True
        other.start()
        assert other.get_progress("slow")["status"] == "done"
    finally:
        release.set()
        local.stop(timeout=4)
        other.stop(timeout=3)


def test_stop_wait_does_not_hold_worker_lock_or_block_current_progress(tmp_path):
    local = Worker(tmp_path / "jobs.sqlite3")
    entered, advance, advanced, release, stopping = [threading.Event() for _ in range(5)]

    def inert_job(*, task_id):
        entered.set()
        assert advance.wait(4)
        local.update_progress(task_id, 1, 2, "停机等待期间仍可更新", stage="saving")
        advanced.set()
        assert release.wait(4)

    def stop():
        stopping.set()
        return local.stop(timeout=3)

    try:
        local.start()
        local.submit("waiting-stop", inert_job, job_metadata=META)
        assert entered.wait(3)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stop)
            assert stopping.wait(1)
            advance.set()
            try:
                assert advanced.wait(1), "waiting for shutdown must not hold the progress mutex"
                assert local.get_progress("waiting-stop")["progress"] == 1
                assert local.get_current_task() == "waiting-stop"
            finally:
                release.set()
            assert future.result(timeout=4)["stopped"] is True
        assert local.get_progress("waiting-stop")["status"] == "done"
    finally:
        advance.set(); release.set()
        local.stop(timeout=4)


def test_stop_quarantines_queued_jobs_instead_of_running_them(tmp_path):
    local = Worker(tmp_path / "jobs.sqlite3")
    calls = []
    local.submit("not-started", lambda *, task_id: calls.append(task_id), job_metadata=META)
    assert local.stop(timeout=2)["pending_count"] == 1
    assert local.get_progress("not-started")["status"] == "needs_review"
    assert local._queue.unfinished_tasks == 0
    try:
        local.start()
        assert calls == [] and local._queue.empty()
    finally:
        local.stop(timeout=3)


def test_ambiguous_queue_publication_never_executes_the_delivered_orphan(tmp_path, monkeypatch):
    local = Worker(tmp_path / "jobs.sqlite3")
    calls = []
    original = local._queue.put_nowait

    def delivered_then_failed(value):
        original(value)
        raise RuntimeError("synthetic publication response lost")

    monkeypatch.setattr(local._queue, "put_nowait", delivered_then_failed)
    try:
        with pytest.raises(RuntimeError):
            local.submit("ambiguous", lambda *, task_id: calls.append(task_id), job_metadata=META)
        assert local.get_progress("ambiguous")["status"] == "needs_review"
        assert local.get_progress("ambiguous")["reason_code"] == "queue_publication_unconfirmed"
        local.start()
        assert local.stop(timeout=3)["stopped"] is True
        assert calls == []
    finally:
        local.stop(timeout=3)


def test_unknown_registration_commit_is_retained_for_review(tmp_path, monkeypatch):
    local = Worker(tmp_path / "jobs.sqlite3")
    ledger = local._get_ledger(create=True, owner=True)
    original = ledger.register

    def committed_then_failed(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic registration response lost")

    monkeypatch.setattr(ledger, "register", committed_then_failed)
    try:
        with pytest.raises(RuntimeError):
            local.submit("commit-unknown", lambda **_: pytest.fail("never execute"), job_metadata=META)
        assert local.get_progress("commit-unknown")["status"] == "needs_review"
        assert local._queue.empty() and local.active_video_tasks() == {}
    finally:
        local.stop(timeout=3)


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_callback_failure_keeps_partial_progress_and_never_replays(tmp_path, failure):
    local = Worker(tmp_path / "jobs.sqlite3")
    def inert_job(*, task_id):
        local.update_progress(task_id, 1, 2, "已保存一项合成结果", stage="transcribing")
        raise failure("do not persist provider exception bodies")
    try:
        local.start()
        local.submit("failed-batch", inert_job, job_metadata=META)
        record = wait_status(local, "failed-batch", "interrupted" if failure is KeyboardInterrupt else "failed")
        assert record["progress"] == 1 and record["total"] == 2 and record["stage"] == "transcribing"
        assert "provider exception" not in record["message"]
    finally:
        local.stop(timeout=3)


def test_progress_cannot_change_frozen_batch_membership(tmp_path):
    ledger = IngestionJobLedger(tmp_path / "jobs.sqlite3").prepare()
    ledger.register("batch", "synthetic-owner", META)
    ledger.start_job("batch", "synthetic-owner")
    before = ledger.get("batch")
    with pytest.raises(IngestionJobError, match="scope_changed"):
        ledger.progress("batch", "synthetic-owner", 0, 3)
    with pytest.raises(IngestionJobError, match="scope_changed"):
        ledger.progress("batch", "synthetic-owner", 0, 2, item_id="outside-batch")
    assert ledger.get("batch") == before


def test_foreign_or_deleted_history_is_not_adopted_as_an_empty_queue(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE foreign_data(value TEXT)")
        db.commit()
    finally:
        db.close()
    before = path.read_bytes()
    with pytest.raises(Exception):
        Worker(path).start()
    assert path.read_bytes() == before
    owned = tmp_path / "owned.sqlite3"
    local = Worker(owned)
    local.start(); local.stop(timeout=3)
    owned.unlink()
    with pytest.raises(IngestionJobError, match="ledger_missing"):
        Worker(owned).get_progress("past-task")
    with pytest.raises(IngestionJobError, match="ledger_missing"):
        Worker(owned).list_tasks()
    with pytest.raises(IngestionJobError, match="ledger_missing"):
        Worker(owned).start()


def test_submission_and_maintenance_guards_keep_old_mutex_contract(tmp_path):
    local = Worker(tmp_path / "jobs.sqlite3")
    with local.submission_guard():
        assert local._lock.acquire(blocking=False)
        local._lock.release()
        with pytest.raises(WorkerBusyError):
            with local.maintenance_guard():
                pytest.fail("must not acquire maintenance during admission")
    with local.maintenance_guard():
        assert local._lock.acquire(blocking=False)
        local._lock.release()
        with pytest.raises(WorkerBusyError):
            local.submit("blocked", lambda **_: pytest.fail("never execute"))
    assert not (tmp_path / "jobs.sqlite3").exists()


def test_overlapping_queued_videos_are_atomically_rejected_before_registration(tmp_path):
    local = Worker(tmp_path / "jobs.sqlite3")
    try:
        local.submit("original", lambda **_: pytest.fail("queued only"), job_metadata=META)
        before = local.get_progress("original")
        with pytest.raises(WorkerBusyError, match="所选视频已有"):
            local.submit("overlap", lambda **_: pytest.fail("must not publish"),
                         job_metadata={**META, "platform_item_ids": [META["platform_item_ids"][1]]})
        assert local.get_progress("overlap") is None and local._queue.qsize() == 1
        assert local.get_progress("original") == before and len(local.list_tasks()) == 1
        snapshot = local.active_video_tasks()
        assert snapshot == {video: "original" for video in META["platform_item_ids"]}
        snapshot.clear()
        assert len(local.active_video_tasks()) == 2
    finally:
        local.stop(timeout=0)


def test_running_scope_blocks_overlap_and_finished_scope_allows_an_explicit_new_job(tmp_path):
    local = Worker(tmp_path / "jobs.sqlite3")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def first(*, task_id):
        calls.append(task_id)
        entered.set()
        assert release.wait(4)
    try:
        local.start()
        local.submit("running", first, job_metadata=META)
        assert entered.wait(3)
        with pytest.raises(WorkerBusyError):
            local.submit("blocked", lambda **_: pytest.fail("must not execute"), job_metadata=META)
        assert local.get_progress("blocked") is None
        release.set()
        wait_status(local, "running", "done")
        assert local.active_video_tasks() == {}
        local.submit("explicit-later", lambda *, task_id: calls.append(task_id), job_metadata=META)
        wait_status(local, "explicit-later", "done")
        assert calls == ["running", "explicit-later"]
        assert local.get_progress("running")["status"] == "done"
    finally:
        release.set()
        local.stop(timeout=4)


def test_submissions_without_metadata_preserve_the_original_compatibility_contract():
    local = Worker()
    for task_id in ("first", "second"):
        local.submit(task_id, lambda **_: pytest.fail("cold worker must not execute"))
    assert local._queue.qsize() == 2 and local.active_video_tasks() == {}
    local.stop(timeout=0)


def test_failed_queue_publication_releases_the_video_reservation(tmp_path, monkeypatch):
    local = Worker(tmp_path / "jobs.sqlite3")
    original = local._queue.put_nowait
    def fail(_):
        raise RuntimeError("synthetic queue publication failure")
    try:
        monkeypatch.setattr(local._queue, "put_nowait", fail)
        with pytest.raises(RuntimeError):
            local.submit("unconfirmed", lambda **_: pytest.fail("must not execute"), job_metadata=META)
        assert local.active_video_tasks() == {}
        monkeypatch.setattr(local._queue, "put_nowait", original)
        local.submit("explicit-new", lambda **_: None, job_metadata=META)
        assert local.active_video_tasks() == {video: "explicit-new" for video in META["platform_item_ids"]}
        assert local.get_progress("unconfirmed")["status"] == "needs_review"
    finally:
        local.stop(timeout=0)


def test_two_concurrent_submissions_cannot_reserve_the_same_video_twice(tmp_path):
    local = Worker(tmp_path / "jobs.sqlite3")
    barrier = threading.Barrier(2)
    def submit(task_id):
        barrier.wait(timeout=2)
        try:
            local.submit(task_id, lambda **_: pytest.fail("cold worker only"), job_metadata=META)
        except WorkerBusyError:
            return "blocked"
        return "accepted"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, ("concurrent-a", "concurrent-b")))
        assert sorted(outcomes) == ["accepted", "blocked"]
        assert local._queue.qsize() == 1 and len(local.list_tasks()) == 1
        assert len(set(local.active_video_tasks().values())) == 1
    finally:
        local.stop(timeout=0)


def test_stop_reports_unconfirmed_status_writes_instead_of_silently_claiming_success(tmp_path, monkeypatch):
    local = Worker(tmp_path / "jobs.sqlite3")
    local.submit("queued", lambda **_: pytest.fail("never execute"), job_metadata=META)
    ledger = local._get_ledger()
    original = ledger.finish
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic durable status write failure")
    monkeypatch.setattr(ledger, "finish", fail)
    outcome = local.stop(timeout=0)
    assert outcome["stopped"] is True and outcome["state_recorded"] is False
    assert local.get_progress("queued")["persistence_error"] is True
    assert local.list_tasks()[0]["persistence_error"] is True
    with pytest.raises(WorkerBusyError):
        local.start()
    monkeypatch.setattr(ledger, "finish", original)
    # A new owner quarantines the prior durable queued record; it never runs it.
    reopened = Worker(tmp_path / "jobs.sqlite3")
    try:
        reopened.start()
        assert reopened.get_progress("queued")["status"] == "needs_review"
    finally:
        reopened.stop(timeout=3)


@pytest.mark.parametrize("persistent", [False, True])
def test_history_count_covers_all_pages_and_survives_a_readonly_reopen(tmp_path, persistent):
    path = tmp_path / "count" / "jobs.sqlite3"
    local = Worker(path if persistent else None)
    assert local.count_tasks() == 0
    assert not path.exists()
    try:
        for number in range(3):
            local.submit(f"count-{number}", lambda **_: pytest.fail("counting cannot execute work"))
        assert local.count_tasks() == 3
        assert isinstance(local.list_tasks(limit=1), list) and len(local.list_tasks(limit=1)) == 1
        assert local.count_tasks() == 3
        if persistent:
            reader = Worker(path)
            assert reader.count_tasks() == 3 and len(reader.list_tasks(limit=1, offset=1)) == 1
            assert local.get_progress("count-0")["status"] == "queued"
    finally:
        local.stop(timeout=0)
    if persistent:
        assert Worker(path).count_tasks() == 3
