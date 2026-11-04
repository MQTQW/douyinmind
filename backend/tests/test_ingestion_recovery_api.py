"""Temporary business/worker ledgers; no worker thread, model or real media."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import threading
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.routes import knowledge as routes
from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.services import knowledge_service as module
from app.services.worker import Worker, WorkerBusyError


REQUEST = '12345678-1234-4234-8234-123456789abc'


@pytest.fixture
def scope(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'business.sqlite3'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    local = Worker(tmp_path / 'jobs.sqlite3')
    checks = []
    monkeypatch.setattr(module, 'worker', local)
    monkeypatch.setattr(routes, 'worker', local)
    monkeypatch.setattr(module.omni_service, 'check_configuration', lambda: checks.append('configuration_only'))
    for name in ('analyze_audio', 'analyze_video'):
        monkeypatch.setattr(module.omni_service, name, lambda *args, **kwargs: pytest.fail('recognition must not run'))
    monkeypatch.setattr(module, 'download_audio', lambda *args, **kwargs: pytest.fail('media must not run'))
    monkeypatch.setattr(module, 'download_video', lambda *args, **kwargs: pytest.fail('media must not run'))
    monkeypatch.setattr(module.settings, 'audio_cache_dir', str(tmp_path / 'audio'))
    monkeypatch.setattr(module.settings, 'video_cache_dir', str(tmp_path / 'video'))
    (tmp_path / 'audio').mkdir(); (tmp_path / 'video').mkdir()
    with Session(engine, expire_on_commit=False) as db:
        base = KnowledgeBase(name='合成恢复库', name_key='synthetic-recovery')
        db.add(base); db.flush()
        for identity, status in [('video-done', 'done'), ('video-failed', 'failed'), ('video-interrupted', 'transcribing'),
                                 ('video-other', 'downloading'), ('video-pending', 'pending'), ('video-deleting', 'deleting')]:
            db.add(VideoCache(platform_item_id=identity, title='合成资料 ' + identity, status=status,
                transcript_text='preserved-' + identity, analysis_json='{"preserved":true}'))
        db.flush()
        targets = ['video-done', 'video-failed', 'video-interrupted']
        db.add_all(KnowledgeBaseVideo(knowledge_base_id=base.id, platform_item_id=identity) for identity in targets)
        db.commit()
        app = FastAPI(); app.include_router(routes.router, prefix='/api')
        def dependency():
            with Session(engine, expire_on_commit=False) as connection: yield connection
        app.dependency_overrides[routes.get_db] = dependency
        with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 51000),
                headers={'origin': 'http://127.0.0.1:5173', 'content-type': 'application/json'}) as client:
            c = SimpleNamespace(db=db, engine=engine, worker=local, base_id=base.id, checks=checks,
                service=module.KnowledgeService(), client=client, targets=targets)
            yield c
    assert not local._thread.is_alive()
    assert local.stop(timeout=0)['stopped'] is True
    engine.dispose()


def old_job(c, *, status='failed', metadata=None):
    metadata = metadata if metadata is not None else {'platform_item_ids': c.targets,
        'knowledge_base_id': c.base_id, 'analysis_mode': 'audio'}
    ledger = c.worker._get_ledger(create=True, owner=True)
    snapshot = ledger.register('old-batch', c.worker._owner_token, metadata)
    if status not in {'queued', 'running'}:
        snapshot = ledger.finish('old-batch', c.worker._owner_token, status,
            message='PRIVATE_PROVIDER_DETAIL_MUST_NOT_ESCAPE')
    elif status == 'running': snapshot = ledger.start_job('old-batch', c.worker._owner_token)
    c.worker._cache('old-batch', snapshot)
    return deepcopy(snapshot)


def statuses(c):
    return dict(c.db.execute(select(VideoCache.platform_item_id, VideoCache.status)).all())


def retry(c, request_id=REQUEST):
    return c.service.retry_sync(c.db, 'old-batch', request_id)


def test_new_submission_records_full_uuid_and_only_actual_pending_scope(scope):
    c = scope
    result = c.service.start_sync(c.db, ['video-done', 'video-pending'], c.base_id)
    assert str(uuid.UUID(result['task_id'])) == result['task_id'] and len(result['task_id']) == 36
    task = c.worker.get_progress(result['task_id'])
    assert task['metadata'] == {'platform_item_ids': ['video-pending'], 'knowledge_base_id': c.base_id,
        'analysis_mode': module.settings.knowledge_analysis_mode}
    assert task['status'] == 'queued' and task['usage_state'] == 'not_recorded'
    assert not c.worker._thread.is_alive() and c.checks == ['configuration_only']


def test_explicit_retry_only_resets_its_interrupted_targets_and_preserves_old_results(scope):
    c = scope; original = old_job(c)
    before = {row.platform_item_id: (row.transcript_text, row.analysis_json) for row in c.db.scalars(select(VideoCache))}
    result = retry(c)
    assert result['reset_count'] == 1 and result['duplicate'] is False
    assert statuses(c)['video-interrupted'] == 'pending' and statuses(c)['video-other'] == 'downloading'
    new = c.worker.get_progress(result['task_id'])
    assert new['metadata']['platform_item_ids'] == ['video-failed', 'video-interrupted']
    assert new['metadata']['knowledge_base_id'] == c.base_id and new['metadata']['recovery_of'] == 'old-batch'
    assert c.worker.get_progress('old-batch') == original
    assert all((row.transcript_text, row.analysis_json) == before[row.platform_item_id] for row in c.db.scalars(select(VideoCache)))
    assert c.worker._queue.qsize() == 1 and not c.worker._thread.is_alive()


def test_same_request_returns_the_original_registered_job_even_after_it_fails(scope):
    c = scope; original = old_job(c)
    first = retry(c)
    count = c.worker.count_tasks()
    assert retry(c)['task_id'] == first['task_id'] and retry(c)['duplicate'] is True
    c.worker._finish(first['task_id'], 'failed')
    again = retry(c)
    assert again['duplicate'] is True and again['task']['status'] == 'failed'
    assert c.worker.count_tasks() == count and c.worker._queue.qsize() == 1
    assert c.worker.get_progress('old-batch') == original and c.checks == ['configuration_only']


def test_all_completed_retry_gets_a_zero_work_idempotency_receipt_without_pipeline(scope):
    c = scope; old_job(c, metadata={'platform_item_ids': ['video-done'], 'knowledge_base_id': c.base_id, 'analysis_mode': 'audio'})
    result = retry(c)
    assert result['pending_count'] == 0 and c.checks == []
    assert c.worker.get_progress(result['task_id'])['metadata']['platform_item_ids'] == []
    # No session, index or model may be opened for this receipt callback.
    c.service._run_sync([])
    assert retry(c)['task_id'] == result['task_id'] and c.worker.count_tasks() == 2


@pytest.mark.parametrize('status', ['done', 'queued', 'running'])
def test_nonreviewable_or_active_originals_do_not_reset_or_publish(scope, status):
    c = scope; old_job(c, status=status); before = statuses(c)
    with pytest.raises((module.KnowledgeRecoveryError, WorkerBusyError)): retry(c)
    assert statuses(c) == before and c.worker.count_tasks() == 1 and c.worker._queue.empty() and c.checks == []


def test_missing_metadata_never_guesses_a_video_scope(scope):
    c = scope; old_job(c, metadata={}); before = statuses(c)
    with pytest.raises(module.KnowledgeRecoveryError, match='不是入库任务'): retry(c)
    assert statuses(c) == before and c.worker._queue.empty()


def test_notes_and_generic_history_do_not_enter_ingestion_pages_or_retry_lookup(scope):
    c = scope; old_job(c); before = statuses(c)
    ledger = c.worker._get_ledger(create=True, owner=True)
    note = ledger.register('notes-job', c.worker._owner_token,
        {'platform_item_ids': ['video-done'], 'knowledge_base_id': None, 'analysis_mode': 'audio'}, kind='notes')
    note = ledger.finish('notes-job', c.worker._owner_token, 'failed')
    c.worker._cache('notes-job', note)
    generic = ledger.register('generic-job', c.worker._owner_token)
    c.worker._cache('generic-job', generic)
    history = c.client.get('/api/knowledge/tasks?limit=1&offset=0').json()
    assert history['success'] and history['total'] == 1
    assert [r['task_id'] for r in history['items']] == ['old-batch']
    for method, url, options in (
        ('get', '/api/knowledge/tasks/notes-job/retry-result', {'params': {'request_id': REQUEST}}),
        ('post', '/api/knowledge/tasks/notes-job/retry', {'json': {'confirm_reprocess': True, 'request_id': REQUEST}}),
        ('get', '/api/knowledge/sync/notes-job', {}),
    ):
        result = getattr(c.client, method)(url, **options).json()
        assert result['success'] is False and result['code'] == 'INGESTION_TASK_KIND_MISMATCH'
    assert c.worker.get_progress('notes-job')['kind'] == 'notes'
    assert statuses(c) == before and c.worker._queue.empty() and c.checks == []


def test_ingestion_result_cannot_adopt_a_notes_job_with_the_same_request_id(scope):
    c = scope; old_job(c); before = statuses(c)
    identity = module.recovery_task_id('old-batch', REQUEST)
    ledger = c.worker._get_ledger(create=True, owner=True)
    note = ledger.register(identity, c.worker._owner_token,
        {'platform_item_ids': ['video-done'], 'recovery_of': 'old-batch'}, kind='ingestion')
    # Typed registration normally rejects cross-kind predecessors. Simulate a
    # conflicting existing deterministic ID while keeping a fully owned schema.
    with ledger._db(write=True) as db:
        db.execute("UPDATE ingestion_jobs SET kind='notes' WHERE task_id=?", (identity,))
    c.worker._cache(identity, ledger.get(identity))
    with pytest.raises(module.KnowledgeRecoveryError) as refused:
        c.service.recovery_result('old-batch', REQUEST)
    assert refused.value.code == 'INGESTION_RETRY_ID_CONFLICT'
    assert statuses(c) == before and c.worker._queue.empty() and c.checks == []


@pytest.mark.parametrize('running', [False, True])
def test_notes_activity_is_busy_but_never_reported_as_ingestion_progress_or_video_counts(scope, monkeypatch, running):
    c = scope; before = statuses(c)
    ledger = c.worker._get_ledger(create=True, owner=True)
    row = ledger.register('notes-active', c.worker._owner_token,
        {'platform_item_ids': ['video-done']}, kind='notes')
    if running:
        row = ledger.start_job('notes-active', c.worker._owner_token)
        c.worker._current_task_id = 'notes-active'
    c.worker._cache('notes-active', row)
    monkeypatch.setattr(module, 'get_chroma_service', lambda: SimpleNamespace(count=lambda: 12, get_video_count=lambda: 3))
    stats = c.service.get_stats(c.db)
    assert stats['current_task_id'] is None and stats['maintenance_busy'] is True and stats['activity_known'] is True
    assert stats['active_task_id'] == ('notes-active' if running else None)
    assert stats['active_task_kind'] == ('notes' if running else None)
    assert stats['video_cache']['done'] == 1 and statuses(c) == before
    assert stats['chromadb'] == {'total_chunks': 12, 'total_videos': 3} and c.checks == []


@pytest.mark.parametrize('change,code', [('missing', 'INGESTION_TARGET_MISSING'),
    ('deleting', 'INGESTION_TARGET_DELETING'), ('deleted_library', 'INGESTION_LIBRARY_UNAVAILABLE')])
def test_retry_revalidates_current_video_and_original_library_before_any_reset(scope, change, code):
    c = scope; original = old_job(c)
    if change == 'missing': c.db.delete(c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == 'video-failed')))
    elif change == 'deleting': c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == 'video-failed')).status = 'deleting'
    else: c.db.get(KnowledgeBase, c.base_id).deleted_at = datetime(2026, 11, 4)
    c.db.commit(); before = statuses(c)
    with pytest.raises(module.KnowledgeRecoveryError) as refused: retry(c)
    assert refused.value.code == code and statuses(c) == before
    assert c.worker.get_progress('old-batch') == original and c.worker._queue.empty() and c.checks == []


def test_deleting_material_is_not_reclassified_or_submitted_by_ordinary_import(scope):
    c = scope
    before = list(c.db.scalars(select(KnowledgeBaseVideo.platform_item_id).where(KnowledgeBaseVideo.knowledge_base_id == c.base_id)))
    with pytest.raises(ValueError): c.service.start_sync(c.db, ['video-deleting'], c.base_id)
    assert list(c.db.scalars(select(KnowledgeBaseVideo.platform_item_id).where(KnowledgeBaseVideo.knowledge_base_id == c.base_id))) == before
    assert c.worker._queue.empty() and c.checks == []


@pytest.mark.parametrize('entry', ['sync', 'retry'])
def test_a_reused_session_cannot_hide_a_committed_deletion_marker(scope, entry):
    c = scope
    if entry == 'retry': old_job(c)
    cached = c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == 'video-failed'))
    assert cached.status == 'failed'
    with Session(c.engine) as other:
        other.scalar(select(VideoCache).where(VideoCache.platform_item_id == 'video-failed')).status = 'deleting'
        other.commit()
    assert cached.status == 'failed'  # Deliberately stale caller identity map.
    with pytest.raises(ValueError):
        if entry == 'sync': c.service.start_sync(c.db, ['video-failed'])
        else: retry(c)
    assert c.worker._queue.empty() and c.checks == []


def test_publication_failure_reports_committed_reset_without_claiming_a_rollback(scope, monkeypatch):
    c = scope; original = old_job(c)
    monkeypatch.setattr(c.worker, 'submit', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('PRIVATE_PROVIDER_TOKEN')))
    with pytest.raises(module.KnowledgeRecoveryError) as failed: retry(c)
    assert failed.value.code == 'INGESTION_RETRY_PARTIAL_RESET' and failed.value.reset_count == 1 and failed.value.needs_review
    assert 'PRIVATE' not in failed.value.message
    assert statuses(c)['video-interrupted'] == 'pending' and statuses(c)['video-other'] == 'downloading'
    assert c.worker.get_progress('old-batch') == original and c.worker.count_tasks() == 1


def test_registration_ack_failure_is_reconciled_to_that_exact_record_without_a_second_submit(scope, monkeypatch):
    c = scope; old_job(c); original_submit = c.worker.submit
    def uncertain(*args, **kwargs):
        original_submit(*args, **kwargs)
        raise OSError('PRIVATE_ACK_FAILURE')
    monkeypatch.setattr(c.worker, 'submit', uncertain)
    result = retry(c)
    assert result['duplicate'] is True and result['found'] is True
    assert c.worker.count_tasks() == 2 and c.worker._queue.qsize() == 1
    assert retry(c)['task_id'] == result['task_id']


def test_simultaneous_same_request_uses_one_worker_registration(scope, monkeypatch):
    c = scope; old_job(c); entered, release = threading.Event(), threading.Event()
    original_submit = c.worker.submit; calls = []
    def delayed(*args, **kwargs):
        calls.append(args[0]); entered.set()
        assert release.wait(timeout=10)
        return original_submit(*args, **kwargs)
    monkeypatch.setattr(c.worker, 'submit', delayed)
    def one():
        with Session(c.engine, expire_on_commit=False) as db:
            return c.service.retry_sync(db, 'old-batch', REQUEST)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(one)
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(WorkerBusyError): retry(c)
            assert c.worker.count_tasks() == 1 and c.worker._queue.empty()
        finally: release.set()
        result = first.result(timeout=10)
    assert retry(c)['task_id'] == result['task_id'] and calls == [result['task_id']]
    assert c.worker.count_tasks() == 2 and c.worker._queue.qsize() == 1
    assert not c.worker._thread.is_alive()


def test_same_deterministic_id_has_one_registration_under_concurrent_submit(scope):
    c = scope; old_job(c); gate = threading.Barrier(2)
    identity = module.recovery_task_id('old-batch', REQUEST)
    def submit():
        gate.wait(timeout=10)
        try:
            c.worker.submit(identity, lambda **kwargs: pytest.fail('this callable must remain inert'),
                job_metadata={'platform_item_ids': ['video-failed', 'video-interrupted'],
                    'knowledge_base_id': c.base_id, 'analysis_mode': 'audio', 'recovery_of': 'old-batch'})
            return 'registered'
        except ValueError:
            return 'rejected_duplicate'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda unused: submit(), range(2)))
    assert sorted(results) == ['registered', 'rejected_duplicate']
    assert c.worker.count_tasks() == 2 and c.worker._queue.qsize() == 1 and not c.worker._thread.is_alive()


def test_reset_commit_failure_rolls_back_once_before_maintenance_exclusion_ends(scope, monkeypatch):
    c = scope; old_job(c); before = statuses(c); calls = []
    original_rollback = c.db.rollback
    def commit():
        raise OSError('synthetic uncertain SQL commit')
    def rollback():
        with pytest.raises(WorkerBusyError):
            with c.worker.submission_guard(): pytest.fail('rollback must remain inside maintenance')
        calls.append('rollback')
        return original_rollback()
    with monkeypatch.context() as patch:
        patch.setattr(c.db, 'commit', commit); patch.setattr(c.db, 'rollback', rollback)
        with pytest.raises(module.KnowledgeRecoveryError) as failed: retry(c)
        assert failed.value.code == 'INGESTION_RETRY_RESET_UNCONFIRMED' and failed.value.needs_review
        assert calls == ['rollback']
    assert statuses(c) == before and c.worker._queue.empty()


def test_history_and_same_request_lookup_are_readonly_and_do_not_expose_provider_details(scope):
    c = scope; old_job(c); before = statuses(c)
    history = c.client.get('/api/knowledge/tasks?limit=1&offset=0').json()
    assert history['success'] and history['total'] == 1 and history['items'][0]['can_retry'] is True
    assert history['items'][0]['usage_state'] == 'not_recorded' and 'PRIVATE' not in str(history)
    result = c.client.get('/api/knowledge/tasks/old-batch/retry-result', params={'request_id': REQUEST}).json()
    assert result['success'] and result['found'] is False
    assert statuses(c) == before and c.worker.count_tasks() == 1 and c.checks == []


@pytest.mark.parametrize('body', [{'confirm_reprocess': False, 'request_id': REQUEST},
    {'confirm_reprocess': 1, 'request_id': REQUEST}, {'confirm_reprocess': True},
    {'confirm_reprocess': True, 'request_id': REQUEST, 'selected_ids': ['video-other']}])
def test_api_requires_explicit_confirmation_id_and_never_accepts_caller_scope(scope, body):
    c = scope; old_job(c); before = statuses(c)
    response = c.client.post('/api/knowledge/tasks/old-batch/retry', json=body)
    assert response.status_code == 422 or response.json()['success'] is False
    assert statuses(c) == before and c.worker.count_tasks() == 1 and c.worker._queue.empty()


def test_cross_site_retry_is_rejected_before_any_recovery_work(scope):
    c = scope; old_job(c); before = statuses(c)
    response = c.client.post('/api/knowledge/tasks/old-batch/retry',
        headers={'origin': 'https://outside.invalid'}, json={'confirm_reprocess': True, 'request_id': REQUEST})
    assert response.status_code == 403 and statuses(c) == before
    assert c.worker._queue.empty() and c.checks == []


def test_delete_failure_and_same_operation_resume_preserve_newer_reimport(scope, monkeypatch):
    c = scope
    class Index:
        fail = True
        calls = []
        def delete_by_video(self, identity):
            self.calls.append(identity)
            if self.fail: raise OSError('PRIVATE_INDEX_PROVIDER')
    index = Index()
    monkeypatch.setattr(module, 'get_chroma_service', lambda: index)
    result = c.client.delete('/api/knowledge/videos/video-done').json()
    assert result['success'] is False and result['code'] == 'DELETION_INCOMPLETE' and result['status'] == 'deleting'
    assert 'PRIVATE' not in str(result)
    identity = result['operation_id']; assert str(uuid.UUID(identity)) == identity
    c.db.expire_all()
    cache = c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == 'video-done'))
    assert cache.status == 'deleting' and cache.transcript_text == 'preserved-video-done'
    history = c.client.get('/api/knowledge/deletions?active_only=true').json()
    assert history['total'] == 1 and history['items'][0]['operation_id'] == identity
    assert c.client.post(f'/api/knowledge/deletions/{identity}/resume', json={'confirm': False}).json()['success'] is False
    assert index.calls == ['video-done']
    index.fail = False
    completed = c.client.post(f'/api/knowledge/deletions/{identity}/resume', json={'confirm': True}).json()
    assert completed['success'] and completed['operation_id'] == identity and completed['deletion_state'] == 'done'
    c.db.expire_all(); cache = c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == 'video-done'))
    cache.status, cache.transcript_text = 'done', 'newer-reimport-original'; c.db.commit()
    count = len(index.calls)
    monkeypatch.setattr(module, 'get_chroma_service', lambda: pytest.fail('old completed operation must not touch the index'))
    again = c.client.post(f'/api/knowledge/deletions/{identity}/resume', json={'confirm': True}).json()
    assert again['success'] and again['already_completed'] is True
    c.db.expire_all()
    assert c.db.scalar(select(VideoCache.transcript_text).where(VideoCache.platform_item_id == 'video-done')) == 'newer-reimport-original'
    assert len(index.calls) == count


def test_pending_deletion_blocks_AI_export_before_any_model_but_keeps_original_readable(scope, monkeypatch):
    from app.services import markdown_export
    c = scope; calls = []
    monkeypatch.setattr(markdown_export.llm_client, 'chat', lambda **kwargs: calls.append(kwargs) or 'must not run')
    blocked = c.client.get('/api/knowledge/export/video-deleting?mode=ai')
    assert blocked.status_code == 409 and blocked.json()['code'] == 'DELETION_PENDING'
    assert calls == []
    original = c.client.get('/api/knowledge/export/video-deleting?mode=original')
    assert original.status_code == 200 and 'preserved-video-deleting' in original.text
    assert calls == []
