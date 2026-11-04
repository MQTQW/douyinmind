"""Typed TEMP job histories, no notes/model service or production database."""
import json
import sqlite3
import threading
import time

import pytest

from app.services.ingestion_jobs import COMPONENT, IngestionJobLedger, IngestionJobError, _SCHEMA_V1
from app.services.worker import Worker, WorkerBusyError


def v1_history(path):
    raw = ' { "platform_item_ids" : ["旧合成资料"], "knowledge_base_id" : 7, "analysis_mode" : "audio" } '
    with sqlite3.connect(path) as db:
        for sql in _SCHEMA_V1.values():
            db.execute(sql)
        db.executemany('INSERT INTO ingestion_job_meta VALUES(?,?)', [('component', COMPONENT), ('version', '1')])
        for identity, metadata in [('legacy-ingest', raw), ('legacy-generic', ' {} ')]:
            db.execute("""INSERT INTO ingestion_jobs(task_id,owner_token,status,stage,progress,total,message,metadata,
                       created_at,updated_at,revision) VALUES(?,?,'done','finished',1,1,'preserved',?,1,2,5)""",
                       (identity, 'old-owner', metadata))
    return raw


def test_v1_read_projection_never_migrates_and_owned_upgrade_preserves_metadata_bytes(tmp_path):
    path = tmp_path / 'old.sqlite3'
    raw = v1_history(path)
    reader = Worker(path)
    assert reader.get_progress('legacy-ingest')['kind'] == 'ingestion'
    assert reader.get_progress('legacy-generic')['kind'] == 'generic'
    assert reader.count_tasks(kind='notes') == 0
    assert [x['task_id'] for x in reader.list_tasks(kind='ingestion', limit=1)] == ['legacy-ingest']
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM ingestion_job_meta WHERE key='version'").fetchone()[0] == '1'
        assert 'kind' not in [row[1] for row in db.execute('PRAGMA table_info(ingestion_jobs)')]
    owner = Worker(path)
    try:
        owner.start()
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT value FROM ingestion_job_meta WHERE key='version'").fetchone()[0] == '2'
            assert db.execute("SELECT metadata FROM ingestion_jobs WHERE task_id='legacy-ingest'").fetchone()[0] == raw
            assert db.execute("SELECT metadata FROM ingestion_jobs WHERE task_id='legacy-generic'").fetchone()[0] == ' {} '
            assert list(db.execute('SELECT created_at,updated_at,revision FROM ingestion_jobs')) == [(1., 2., 5), (1., 2., 5)]
            assert db.execute('SELECT count(*) FROM ingestion_job_events').fetchone()[0] == 0
        assert owner.get_progress('legacy-generic')['kind'] == 'generic'
    finally:
        owner.stop(timeout=2)


@pytest.mark.parametrize('persistent', [False, True])
def test_kind_filter_precedes_pagination_and_count_and_survives_reopen(tmp_path, persistent):
    local = Worker(tmp_path / 'jobs.sqlite3' if persistent else None)
    try:
        for identity, kind in [('notes-one', 'notes'), ('ingest-one', 'ingestion'),
                               ('notes-two', 'notes'), ('generic-one', 'generic')]:
            metadata = None if kind == 'generic' else {'platform_item_ids': [identity], 'knowledge_base_id': None}
            local.submit(identity, lambda **_: pytest.fail('cold queue'), job_metadata=metadata, job_kind=kind)
        for reader in ([local, Worker(tmp_path / 'jobs.sqlite3')] if persistent else [local]):
            assert reader.count_tasks() == 4 and reader.count_tasks(kind='notes') == 2
            assert reader.count_tasks(kind='generic') == 1 and reader.count_tasks(kind='ingestion') == 1
            assert [r['task_id'] for r in reader.list_tasks(limit=1, offset=1, kind='notes')] == ['notes-one']
            assert [r['kind'] for r in reader.list_tasks(limit=1, kind='ingestion')] == ['ingestion']
    finally:
        local.stop(timeout=2)


def test_explicit_notes_are_not_inferred_and_recovery_cannot_cross_kinds(tmp_path):
    ledger = IngestionJobLedger(tmp_path / 'jobs.sqlite3').prepare()
    metadata = {'platform_item_ids': ['v'], 'knowledge_base_id': None}
    note = ledger.register('notes-old', 'owner', metadata, kind='notes')
    assert note['kind'] == 'notes' and note['metadata'] == metadata
    ledger.finish('notes-old', 'owner', 'failed')
    with pytest.raises(IngestionJobError, match='recovery_not_reviewable'):
        ledger.register('wrong-ingest', 'owner', {**metadata, 'recovery_of': 'notes-old'})
    assert ledger.get('wrong-ingest') is None
    assert ledger.register('notes-new', 'owner', {**metadata, 'recovery_of': 'notes-old'}, kind='notes')['kind'] == 'notes'
    assert ledger.register('legacy-empty-batch', 'owner', {'platform_item_ids': []})['kind'] == 'ingestion'
    assert ledger.register('old-no-metadata', 'owner')['kind'] == 'generic'


@pytest.mark.parametrize('persistent', [False, True])
def test_all_kinds_share_video_reservations_maintenance_and_one_shutdown(tmp_path, persistent):
    local = Worker(tmp_path / 'jobs.sqlite3' if persistent else None)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def note(*, task_id):
        calls.append(task_id); entered.set(); assert release.wait(3)
    try:
        local.submit('notes', note, job_metadata={'platform_item_ids': ['video']}, job_kind='notes')
        assert local.activity_snapshot()['maintenance_busy'] is True
        with pytest.raises(WorkerBusyError):
            local.submit('ingest', lambda **_: None, job_metadata={'platform_item_ids': ['video']})
        with pytest.raises(WorkerBusyError):
            with local.maintenance_guard():
                pytest.fail('notes queued is busy')
        local.start(); assert entered.wait(2)
        assert local.activity_snapshot() == {'active_task_id': 'notes', 'active_task_kind': 'notes',
                                             'maintenance_busy': True, 'activity_known': True}
        assert local.active_video_tasks() == {'video': 'notes'}
        with pytest.raises(IngestionJobError, match='progress_scope_changed'):
            local.update_progress('notes', 0, 2, 'cannot reinterpret videos as note steps')
        with pytest.raises(IngestionJobError, match='progress_scope_changed'):
            local.update_progress('notes', 0, 1, item_id='foreign-video')
        local.update_progress('notes', 0, 1, stage='notes', item_id='video')
        with pytest.raises(WorkerBusyError):
            local.submit('ingest', lambda **_: None, job_metadata={'platform_item_ids': ['video']})
        release.set()
        end = time.monotonic() + 3
        while local.get_progress('notes')['status'] != 'done' and time.monotonic() < end:
            time.sleep(.005)
        assert local.get_progress('notes')['kind'] == 'notes' and local.active_video_tasks() == {}
        assert calls == ['notes']
    finally:
        release.set(); local.stop(timeout=3)


def test_bad_kind_fails_before_claim_or_database_creation(tmp_path):
    local = Worker(tmp_path / 'jobs.sqlite3')
    with pytest.raises(IngestionJobError, match='invalid_job_kind'):
        local.submit('bad', lambda **_: None, job_kind='other')
    assert not (tmp_path / 'jobs.sqlite3').exists() and local.active_video_tasks() == {}


def test_foreign_or_augmented_v1_schema_is_never_migrated(tmp_path):
    path = tmp_path / 'old.sqlite3'
    v1_history(path)
    with sqlite3.connect(path) as db:
        db.execute('CREATE INDEX foreign_index ON ingestion_jobs(status)')
    before = path.read_bytes()
    with pytest.raises(Exception):
        Worker(path).start()
    assert path.read_bytes() == before
