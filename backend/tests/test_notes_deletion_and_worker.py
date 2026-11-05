"""Actual TEMP Worker/SQL lifecycle, fake LLM and no platform/index clients."""
import threading

import pytest
from sqlalchemy import select

from app.models.entities import KnowledgeNoteGeneration as Generation, KnowledgeNoteStep as Step, VideoCache
from app.services.material_deletion import delete_material, DeletionIncomplete
from app.services.notes_store import NotesStore
from app.services.worker import Worker, WorkerBusyError
from app.services.notes_service import NotesService
from tests.test_notes_persistence import context, submit, request, long_source, VIDEO, PRIVATE


class Index:
    def __init__(self): self.calls = []
    def delete_by_video(self, video): self.calls.append(video)


def test_global_delete_erases_all_note_copies_in_final_sql_and_old_done_cannot_erase_new_note(context, monkeypatch):
    c = context
    first, _ = submit(c); c.queue.execute()
    c.model.identity['model'] = 'another-synthetic-model'; c.model.fail_at = 2
    unknown, old_body = submit(c)
    with pytest.raises(RuntimeError): c.queue.execute()
    assert len(c.model.calls) == 2
    audio, video = c.tmp / 'audio', c.tmp / 'video'; audio.mkdir(); video.mkdir()
    index, commits, original = Index(), [], c.db.commit
    def commit():
        commits.append(True)
        if len(commits) == 4: raise OSError('SYNTHETIC_FINAL_COMMIT_ERROR')
        original()
    monkeypatch.setattr(c.db, 'commit', commit)
    with pytest.raises(DeletionIncomplete) as error:
        delete_material(c.db, VIDEO, chroma=index, audio_dir=audio, video_dir=video)
    assert error.value.reason == 'final_commit_failed'
    # Cleanup attempted, but rolled-back SQL never claims notes were wiped.
    generations = list(c.db.scalars(select(Generation)))
    assert all(item.source_snapshot for item in generations)
    assert NotesStore(c.db).generation(first['item']['note_id']).markdown
    result = delete_material(c.db, VIDEO, chroma=index, audio_dir=audio, video_dir=video,
                             expected_operation_id=error.value.operation_id)
    assert result['deletion_state'] == 'done' and index.calls == [VIDEO] and len(c.model.calls) == 2
    c.db.expire_all()
    assert all(item.state == 'deleted' and not item.source_snapshot and not item.markdown
               for item in c.db.scalars(select(Generation)))
    assert all(not item.output for item in c.db.scalars(select(Step)))
    assert request(c, old_body['request_id'])['usage']['state'] == 'unknown'
    assert request(c, old_body['request_id'])['state'] == 'deleted'
    cache = c.db.scalar(select(VideoCache)); cache.status = 'done'; cache.transcript_text = '后来重新入库的源文'; c.db.commit()
    c.model.fail_at = None
    newer, _ = submit(c); c.queue.execute()
    newer_bytes = c.service.artifact(c.db, newer['item']['note_id'])
    repeated = delete_material(c.db, VIDEO, chroma=index, audio_dir=audio, video_dir=video,
                               expected_operation_id=result['operation_id'])
    assert repeated['already_completed'] and c.service.artifact(c.db, newer['item']['note_id']) == newer_bytes
    assert index.calls == [VIDEO] and len(c.model.calls) == 3


@pytest.mark.parametrize('long_form', [False, True])
def test_actual_typed_worker_owns_notes_and_maintenance_exclusion_and_restart_never_replays(context, long_form):
    c = context; local = Worker(c.tmp / 'typed-jobs.sqlite3')
    if long_form: long_source(c)
    service = NotesService(session_factory=c.factory, worker=local, llm=c.model, ready=lambda: True)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    run_notes = service._run
    def observed_run(*args, **kwargs):
        try:
            return run_notes(*args, **kwargs)
        finally:
            completed.set()
    service._run = observed_run
    def held_model_call(index):
        entered.set()
        assert release.wait(10), 'the test did not release its fake model call'
    c.model.on_call = held_model_call
    try:
        local.start()
        result = service.submit(c.db, VIDEO, request_id=__import__('uuid').uuid4(), confirm=True,
            expected_source_fingerprint=service.source_view(c.db, VIDEO)['fingerprint'])
        task_id = result['item']['task_id']
        assert entered.wait(10), 'the actual worker did not enter its fake model call'
        assert local.get_progress(task_id)['kind'] == 'notes'
        assert local.list_tasks(kind='ingestion') == [] and local.count_tasks(kind='notes') == 1
        with pytest.raises(WorkerBusyError):
            with local.maintenance_guard(): pass
        with pytest.raises(WorkerBusyError):
            local.submit('other-ingestion', lambda *, task_id: None,
                job_metadata={'platform_item_ids': [VIDEO]}, job_kind='ingestion')
        release.set()
        # All actual note steps must return before stopping: an earlier stop
        # correctly fences the next paid segment. Wait on the callback instead
        # of repeatedly opening the ledger while it is persisting those steps.
        assert completed.wait(10), 'the actual NotesService callback did not finish'
        assert len(c.model.calls) == (3 if long_form else 1)
        stopped = local.stop(timeout=10)
        assert stopped['stopped'] and not stopped['timed_out']
        assert stopped.get('state_recorded') is not False
        assert local.get_progress(task_id)['status'] == 'done'
        assert local.get_progress(task_id)['progress'] == local.get_progress(task_id)['total'] == 1
        assert service.lookup(c.db, result['item']['request_id'])['item']['state'] == 'done'
        reopened = Worker(c.tmp / 'typed-jobs.sqlite3'); reopened.start()
        try:
            assert reopened.get_progress(task_id)['kind'] == 'notes'
            assert reopened.get_progress(task_id)['status'] == 'done'
            assert len(c.model.calls) == (3 if long_form else 1)
        finally:
            assert reopened.stop(timeout=3)['stopped']
    finally:
        release.set(); local.stop(timeout=3)


@pytest.mark.parametrize('long_form', [False, True])
def test_stop_saves_current_result_but_never_starts_the_next_paid_segment(context, long_form):
    c = context
    if long_form: long_source(c)
    local = Worker(c.tmp / 'stopping-jobs.sqlite3')
    service = NotesService(session_factory=c.factory, worker=local, llm=c.model, ready=lambda: True)
    entered, release = threading.Event(), threading.Event()
    def held_model_call(index):
        entered.set()
        assert release.wait(10), 'the test did not release its fake model call'
    c.model.on_call = held_model_call
    try:
        local.start()
        result = service.submit(c.db, VIDEO, request_id=__import__('uuid').uuid4(), confirm=True,
            expected_source_fingerprint=service.source_view(c.db, VIDEO)['fingerprint'])
        task_id = result['item']['task_id']
        assert entered.wait(10), 'the actual worker did not enter its fake model call'
        assert local.stop(timeout=.01)['stopped'] is False
        assert local.is_stopping() is True
        release.set()
        # stop() joins the actual thread and its final state writes. A fixed
        # polling window is neither a completion barrier nor a latency spec.
        stopped = local.stop(timeout=10)
        assert stopped['stopped'] and not stopped['timed_out']
        assert stopped.get('state_recorded') is not False
        item = service.lookup(c.db, result['item']['request_id'])['item']
        assert len(c.model.calls) == 1
        assert item['state'] == ('needs_review' if long_form else 'done')
        if long_form:
            assert item['reason_code'] == 'worker_stopping' and item['progress'] == 1
            assert [step.state for step in NotesStore(c.db).steps(item['note_id'])] == ['succeeded', 'pending', 'pending']
        else:
            assert service.artifact(c.db, item['note_id'])
    finally:
        release.set(); local.stop(timeout=3)
