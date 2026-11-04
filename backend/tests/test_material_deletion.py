"""Temporary SQL/cache files and fake Chroma only; no import or model calls."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, KnowledgeDeletionOperation, VideoCache
from app.services import material_deletion as module
from app.services.chroma_service import ChromaService


VIDEO = 'synthetic-video'
ORIGINAL = '完整合成原文，不应在外部清理尚未完成时清空。'


class Index:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.on_delete = lambda: None
    def delete_by_video(self, video):
        self.calls.append(video)
        self.on_delete()
        if self.fail:
            raise OSError('SYNTHETIC_PRIVATE_ERROR')


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "business.sqlite3"}')
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    db.add_all([KnowledgeBase(id=1, name='甲库', name_key='甲库'),
                KnowledgeBase(id=2, name='乙库', name_key='乙库'),
                VideoCache(platform_item_id=VIDEO, title='合成标题', status='done',
                           transcript_text=ORIGINAL, visual_analysis_text='合成画面', summary='旧摘要',
                           analysis_json='{"synthetic":true}', analysis_model='synthetic-model',
                           analysis_duration_seconds=12, processed_at=datetime.now(timezone.utc))])
    db.flush()
    db.add_all([KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id=VIDEO),
                KnowledgeBaseVideo(knowledge_base_id=2, platform_item_id=VIDEO)])
    db.commit()
    audio, video = tmp_path / 'audio', tmp_path / 'video'
    audio.mkdir(); video.mkdir()
    source_media = audio / 'source_media'; source_media.mkdir()
    owned = [audio / f'{VIDEO}.wav', audio / f'{VIDEO}.wav.analysis.json', video / f'{VIDEO}.mp4']
    owned += [audio / f'{VIDEO}.wav.boundary-analysis.json', audio / f'{VIDEO}.wav.boundary-packs.json',
              source_media / f'{VIDEO}.mp4']
    for path in owned:
        path.write_text('synthetic private cached media', encoding='utf-8')
    unrelated = audio / 'unrelated.wav'
    unrelated.write_bytes(b'never delete another video')
    other_copies = [source_media / f'{VIDEO}-other.mp4', audio / f'{VIDEO}-other.wav.boundary-packs.json']
    for path in other_copies: path.write_bytes(b'other video source copy')
    value = SimpleNamespace(db=db, engine=engine, audio=audio, video=video, owned=owned,
                            unrelated=unrelated, other_copies=other_copies, index=Index())
    try:
        yield value
    finally:
        db.close(); engine.dispose()


def run(c, operation_id=None):
    return module.delete_material(c.db, VIDEO, chroma=c.index, audio_dir=c.audio, video_dir=c.video,
                                  expected_operation_id=operation_id)


def material(c):
    c.db.expire_all()
    return c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))


def operations(c):
    c.db.expire_all()
    return list(c.db.scalars(select(KnowledgeDeletionOperation)))


def assert_original_retained(c):
    cache = material(c)
    assert cache.status == 'deleting' and cache.transcript_text == ORIGINAL
    assert cache.visual_analysis_text == '合成画面' and cache.analysis_json == '{"synthetic":true}'
    assert cache.summary == '旧摘要'
    assert len(list(c.db.scalars(select(KnowledgeBaseVideo)))) == 2


def test_intent_and_deleting_are_committed_before_external_delete_and_final_clear(context):
    c = context
    def observe_committed_intent():
        with Session(c.engine) as other:
            cache = other.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))
            active = other.scalar(select(KnowledgeDeletionOperation).where(KnowledgeDeletionOperation.active_key == 1))
            assert cache.status == 'deleting' and cache.transcript_text == ORIGINAL
            assert active.state == 'pending' and active.operation_id
    c.index.on_delete = observe_committed_intent
    result = run(c)
    assert result['status'] == 'pending' and result['deletion_state'] == 'done'
    assert c.index.calls == [VIDEO] and all(not path.exists() for path in c.owned)
    assert c.unrelated.read_bytes() == b'never delete another video'
    assert all(path.read_bytes() == b'other video source copy' for path in c.other_copies)
    cache = material(c)
    assert cache.status == 'pending' and cache.transcript_text == cache.visual_analysis_text == cache.summary == ''
    assert cache.analysis_json == '{}' and cache.analysis_model == '' and cache.processed_at is None
    assert list(c.db.scalars(select(KnowledgeBaseVideo))) == []
    op = operations(c)[0]
    assert op.state == 'done' and op.active_key is None and op.completed_at is not None


def test_intent_commit_failure_cannot_touch_index_media_or_original(context, monkeypatch):
    c = context
    def fail_commit():
        raise OSError('SYNTHETIC_PRIVATE_ERROR')
    monkeypatch.setattr(c.db, 'commit', fail_commit)
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    assert error.value.reason == 'intent_commit_failed' and str(uuid.UUID(error.value.operation_id))
    assert 'PRIVATE' not in str(error.value) and c.index.calls == []
    assert all(path.exists() for path in c.owned)
    cache = material(c)
    assert cache.status == 'done' and cache.transcript_text == ORIGINAL and operations(c) == []


def test_failed_rollback_discards_dirty_intent_instead_of_leaving_it_for_later_autocommit(context, monkeypatch):
    c = context
    def broken():
        raise OSError('SYNTHETIC_PRIVATE_CONNECTION_ERROR')
    monkeypatch.setattr(c.db, 'commit', broken)
    monkeypatch.setattr(c.db, 'rollback', broken)
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    assert error.value.reason == 'intent_commit_failed' and 'PRIVATE' not in str(error.value)
    with Session(c.engine) as other:
        assert other.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO)).status == 'done'
        assert list(other.scalars(select(KnowledgeDeletionOperation))) == []
    assert not c.db.new and not c.db.dirty and not c.index.calls
    assert all(path.exists() for path in c.owned)


def test_sql_lookup_failure_has_a_fixed_reason_and_never_starts_external_deletion(context, monkeypatch):
    c = context; original_scalar = c.db.scalar
    def broken(*args, **kwargs):
        raise OperationalError('synthetic lookup', {}, RuntimeError('SYNTHETIC_PRIVATE_ERROR'))
    monkeypatch.setattr(c.db, 'scalar', broken)
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    assert error.value.reason == 'operation_lookup_failed' and 'PRIVATE' not in str(error.value)
    assert c.index.calls == [] and all(path.exists() for path in c.owned)
    monkeypatch.setattr(c.db, 'scalar', original_scalar)
    assert operations(c) == [] and material(c).transcript_text == ORIGINAL


def test_index_failure_keeps_active_intent_original_and_resumes_same_operation(context):
    c = context; c.index.fail = True
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    op_id = error.value.operation_id
    assert error.value.reason == 'index_delete_failed'
    assert_original_retained(c)
    assert operations(c)[0].state == 'pending' and all(path.exists() for path in c.owned)
    c.index.fail = False; result = run(c, op_id)
    assert result['operation_id'] == op_id and len(operations(c)) == 1 and c.index.calls == [VIDEO, VIDEO]


def test_partial_media_failure_does_not_restore_index_or_clear_original_and_retry_is_idempotent(context, monkeypatch):
    c = context; original_unlink = Path.unlink
    failed = c.owned[1]
    def unlink(path, *args, **kwargs):
        if path == failed:
            raise PermissionError('SYNTHETIC_PRIVATE_ERROR')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    op_id = error.value.operation_id
    assert error.value.reason == 'files_delete_failed' and c.index.calls == [VIDEO]
    assert not c.owned[0].exists() and failed.exists()
    assert operations(c)[0].state == 'index_deleted'
    assert_original_retained(c)
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    assert run(c, op_id)['operation_id'] == op_id and c.index.calls == [VIDEO]
    assert all(not path.exists() for path in c.owned)


@pytest.mark.parametrize('extension', ['.mp3', '.wav', '.m4a', '.webm', '.opus'])
def test_every_existing_audio_checkpoint_namespace_is_cleared_without_touching_other_videos(context, extension):
    c = context
    extra = [c.audio / f'{VIDEO}{extension}{suffix}' for suffix in
             ('.analysis.json', '.boundary-analysis.json', '.boundary-packs.json')]
    for path in extra: path.write_text('synthetic recognized source copy', encoding='utf-8')
    assert run(c)['deletion_state'] == 'done'
    assert all(not path.exists() for path in extra + c.owned)
    assert all(path.read_bytes() == b'other video source copy' for path in c.other_copies)


@pytest.mark.parametrize('slot', [3, 4, 5])
def test_unconfirmed_boundary_or_source_media_cleanup_never_finalizes_sql(context, monkeypatch, slot):
    c = context; original_unlink = Path.unlink; target = c.owned[slot]
    def unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError('SYNTHETIC_PRIVATE_ERROR')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(module.DeletionIncomplete) as error: run(c)
    assert error.value.reason == 'files_delete_failed' and target.exists()
    assert_original_retained(c)
    assert operations(c)[0].state == 'index_deleted' and operations(c)[0].active_key == 1
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    assert run(c, error.value.operation_id)['deletion_state'] == 'done'
    assert not target.exists() and c.index.calls == [VIDEO]


def test_unlink_return_without_observed_absence_is_not_a_completed_delete(context, monkeypatch):
    c = context; original_unlink = Path.unlink; target = c.owned[4]
    def unlink(path, *args, **kwargs):
        if path == target: return None
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(module.DeletionIncomplete) as error: run(c)
    assert error.value.reason == 'files_delete_failed' and target.exists()
    assert_original_retained(c)
    assert operations(c)[0].state == 'index_deleted'


def test_linked_media_file_is_rejected_before_unlinking_any_fixed_copy(context, monkeypatch):
    c = context; original_lstat = Path.lstat; target = c.owned[5]
    def lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=2,
                                   st_file_attributes=getattr(info, 'st_file_attributes', 0))
        return info
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(module.DeletionIncomplete) as error: run(c)
    assert error.value.reason == 'files_delete_failed' and all(path.exists() for path in c.owned)
    assert_original_retained(c)


def test_directory_in_a_source_copy_slot_is_never_recursively_deleted(context):
    c = context; target = c.owned[5]
    target.unlink(); target.mkdir(); (target / 'keep.txt').write_text('do not recursively delete', encoding='utf-8')
    with pytest.raises(module.DeletionIncomplete) as error: run(c)
    assert error.value.reason == 'files_delete_failed'
    assert target.is_dir() and (target / 'keep.txt').exists()
    assert all(path.exists() for path in c.owned)
    assert_original_retained(c)


def test_redirected_source_media_directory_is_rejected_without_following_it(context, monkeypatch):
    c = context; original = Path.is_symlink; root = c.audio / 'source_media'
    monkeypatch.setattr(Path, 'is_symlink', lambda path: True if path == root else original(path))
    with pytest.raises(module.DeletionIncomplete) as error: run(c)
    assert error.value.reason == 'files_delete_failed' and all(path.exists() for path in c.owned)
    assert_original_retained(c)


def test_existing_files_deleted_checkpoint_rechecks_all_fixed_source_copies_before_final_sql(context, monkeypatch):
    c = context; original_commit = c.db.commit; commits = []
    def commit():
        commits.append(1)
        if len(commits) == 4: raise OSError('SYNTHETIC_FINAL_COMMIT_FAILURE')
        original_commit()
    monkeypatch.setattr(c.db, 'commit', commit)
    with pytest.raises(module.DeletionIncomplete) as error: run(c)
    assert operations(c)[0].state == 'files_deleted'
    # Simulate an older cleanup version that had confirmed only its shorter
    # list; this active operation still has no authority over a new incarnation.
    for path in c.owned[3:]: path.write_bytes(b'older-version retained source copy')
    assert run(c, error.value.operation_id)['deletion_state'] == 'done'
    assert all(not path.exists() for path in c.owned) and c.index.calls == [VIDEO]


@pytest.mark.parametrize('fail_at,expected_phase', [(2, 'pending'), (3, 'index_deleted'), (4, 'files_deleted')])
def test_sql_checkpoint_or_final_failure_preserves_original_until_explicit_resume(context, monkeypatch, fail_at, expected_phase):
    c = context; original_commit = c.db.commit; commits = []
    def commit():
        commits.append(1)
        if len(commits) == fail_at:
            raise OSError('SYNTHETIC_PRIVATE_ERROR')
        original_commit()
    monkeypatch.setattr(c.db, 'commit', commit)
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    assert error.value.reason == ('final_commit_failed' if fail_at == 4 else 'progress_commit_failed')
    assert_original_retained(c)
    op = operations(c)[0]
    assert op.state == expected_phase and op.active_key == 1
    assert run(c, op.operation_id)['deletion_state'] == 'done'
    assert len(operations(c)) == 1


def test_new_delete_continues_pending_operation_without_allocating_another(context):
    c = context; c.index.fail = True
    with pytest.raises(module.DeletionIncomplete):
        run(c)
    before = operations(c)[0].operation_id
    c.index.fail = False
    assert run(c)['operation_id'] == before and len(operations(c)) == 1


def test_old_completed_resume_cannot_touch_a_later_import_incarnation(context):
    c = context; first = run(c)
    cache = material(c); cache.status = 'done'; cache.transcript_text = '后来重新入库的新原文'
    cache.processed_at = datetime.now(timezone.utc)
    c.db.add(KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id=VIDEO)); c.db.commit()
    for path in c.owned:
        path.write_bytes(b'later import')
    calls = list(c.index.calls)
    assert run(c, first['operation_id'])['already_completed'] is True
    assert material(c).transcript_text == '后来重新入库的新原文' and c.index.calls == calls
    assert all(path.read_bytes() == b'later import' for path in c.owned)
    assert len(list(c.db.scalars(select(KnowledgeBaseVideo)))) == 1
    second = run(c)
    assert second['operation_id'] != first['operation_id'] and len(operations(c)) == 2


@pytest.mark.parametrize('field,value', [('status', 'done'), ('transcript_text', '外部新原文'),
                                       ('analysis_json', '{"replacement":true}')])
def test_active_old_intent_cannot_delete_changed_material(context, field, value):
    c = context; c.index.fail = True
    with pytest.raises(module.DeletionIncomplete) as error:
        run(c)
    cache = material(c); setattr(cache, field, value); c.db.commit()
    c.index.fail = False; calls = list(c.index.calls)
    with pytest.raises(module.DeletionIncomplete, match='资料状态或内容已变化'):
        run(c, error.value.operation_id)
    assert getattr(material(c), field) == value and c.index.calls == calls
    assert all(path.exists() for path in c.owned)


def test_lost_final_commit_acknowledgement_reads_done_without_replaying_external_deletion(context, monkeypatch):
    c = context; original_commit = c.db.commit; calls = []
    def commit():
        calls.append(1)
        original_commit()
        if len(calls) == 4:
            raise OSError('SYNTHETIC_ACK_LOST_AFTER_COMMIT')
    monkeypatch.setattr(c.db, 'commit', commit)
    result = run(c)
    assert result['deletion_state'] == 'done' and result['already_completed'] is True
    assert operations(c)[0].active_key is None and material(c).status == 'pending'
    assert c.index.calls == [VIDEO] and all(not path.exists() for path in c.owned)


def test_unknown_or_wrong_video_operation_id_cannot_start_unrelated_deletion(context):
    c = context
    with pytest.raises(module.DeletionIncomplete) as missing:
        run(c, str(uuid.uuid4()))
    assert missing.value.reason == 'operation_not_found' and not c.index.calls and not operations(c)
    c.index.fail = True
    with pytest.raises(module.DeletionIncomplete) as pending:
        run(c)
    calls = list(c.index.calls)
    with pytest.raises(module.DeletionIncomplete) as wrong:
        module.delete_material(c.db, 'different-video', chroma=c.index, audio_dir=c.audio, video_dir=c.video,
                               expected_operation_id=pending.value.operation_id)
    assert wrong.value.reason == 'operation_conflict' and c.index.calls == calls
    assert all(path.exists() for path in c.owned)


def test_active_slot_is_unique_but_completed_history_does_not_block_a_new_operation(context):
    c = context; c.index.fail = True
    with pytest.raises(module.DeletionIncomplete):
        run(c)
    old = operations(c)[0]
    c.db.add(KnowledgeDeletionOperation(operation_id=str(uuid.uuid4()), platform_item_id=VIDEO,
        cache_id=old.cache_id, material_fingerprint=old.material_fingerprint, active_key=1, state='pending'))
    with pytest.raises(IntegrityError):
        c.db.commit()
    c.db.rollback()
    assert len(operations(c)) == 1


@pytest.mark.parametrize('video', ['../outside', '', 'a' * 65])
def test_invalid_video_scope_does_not_start_an_operation(context, video):
    c = context
    with pytest.raises(ValueError):
        module.delete_material(c.db, video, chroma=c.index, audio_dir=c.audio, video_dir=c.video)
    assert c.index.calls == [] and operations(c) == [] and all(path.exists() for path in c.owned)


class Collection:
    """Record exact mutator order and optionally fail after partial mutation."""
    def __init__(self, rows):
        self.rows = deepcopy(rows); self.calls = []; self.upserts = 0
        self.fail_upserts = set(); self.fail_delete_once = False; self.fail_get_after_write = False
    def get(self, *, include, where=None, ids=None):
        self.calls.append(('get', deepcopy(where), deepcopy(ids)))
        if self.fail_get_after_write and self.upserts:
            raise OSError('SYNTHETIC_PRIVATE_ERROR')
        keys = [key for key, row in self.rows.items() if
                (ids is None or key in ids) and (where is None or row['metadata']['platform_item_id'] == where['platform_item_id'])]
        return {'ids': keys, 'metadatas':[deepcopy(self.rows[key]['metadata']) for key in keys],
                'documents':[self.rows[key]['document'] for key in keys],
                'embeddings':[deepcopy(self.rows[key]['embedding']) for key in keys]}
    def upsert(self, *, ids, embeddings, metadatas, documents):
        self.calls.append(('upsert', list(ids))); self.upserts += 1
        for key, vector, metadata, document in zip(ids, embeddings, metadatas, documents):
            self.rows[key] = {'embedding':deepcopy(vector),'metadata':deepcopy(metadata),'document':document}
            if self.upserts in self.fail_upserts:
                raise OSError('SYNTHETIC_PRIVATE_ERROR')
    def delete(self, *, ids=None, where=None):
        self.calls.append(('delete', deepcopy(ids), deepcopy(where)))
        keys = list(ids) if ids is not None else [key for key, row in self.rows.items()
                if row['metadata']['platform_item_id'] == where['platform_item_id']]
        for key in keys:
            self.rows.pop(key, None)
            if self.fail_delete_once:
                self.fail_delete_once = False
                raise OSError('SYNTHETIC_PRIVATE_ERROR')


def old_rows(count=3):
    return {f'{VIDEO}:{i}': {'embedding':[float(i), .5], 'document':f'旧原文{i}',
             'metadata':{'platform_item_id':VIDEO,'chunk_id':f'{VIDEO}:{i}', 'chunk_index':i,
                         'title':'旧标题','created_at':1}} for i in range(count)}


def chroma(rows=None):
    service = ChromaService.__new__(ChromaService)
    service._collection = Collection(old_rows() if rows is None else rows)
    return service


def test_index_replacement_upserts_before_deleting_only_stale_tail():
    service = chroma()
    assert service.upsert_video_chunks(VIDEO, '新标题', ['新原文0', '新原文1'], [[1., .5], [2., .5]]) == [f'{VIDEO}:0', f'{VIDEO}:1']
    calls = service._collection.calls
    mutations = [row for row in calls if row[0] != 'get']
    assert mutations == [('upsert', [f'{VIDEO}:0', f'{VIDEO}:1']), ('delete', [f'{VIDEO}:2'], None)]
    assert [row['document'] for row in service._collection.rows.values()] == ['新原文0', '新原文1']


@pytest.mark.parametrize('chunks,vectors', [(['新'], []), (['新', '二'], [[1.], [1., 2.]]),
    (['新'], [[float('nan')]]), (['新'], [[True]]), ([''], [[1.]]), ([], [[1.]])])
def test_invalid_index_inputs_never_read_delete_or_upsert_the_old_index(chunks, vectors):
    service = chroma(); before = deepcopy(service._collection.rows)
    with pytest.raises(ValueError):
        service.upsert_video_chunks(VIDEO, '新标题', chunks, vectors)
    assert service._collection.calls == [] and service._collection.rows == before


@pytest.mark.parametrize('failure', ['partial_upsert', 'stale_delete'])
def test_partial_index_failure_restores_and_verifies_the_exact_old_snapshot(failure):
    service = chroma(); before = deepcopy(service._collection.rows)
    if failure == 'partial_upsert':service._collection.fail_upserts = {1}
    else:service._collection.fail_delete_once = True
    with pytest.raises(RuntimeError, match='已核对恢复旧索引'):
        service.upsert_video_chunks(VIDEO, '新标题', ['新0', '新1'], [[1., .5], [2., .5]])
    assert service._collection.rows == before


def test_partial_index_growth_removes_new_ids_during_compensation():
    service = chroma(old_rows(1)); before = deepcopy(service._collection.rows)
    # Verification fails only once, after the larger new set was installed.
    original_get = service._collection.get; failures = [True]
    def get(**kwargs):
        if service._collection.upserts == 1 and kwargs.get('where') and failures:
            failures.pop(); raise OSError('SYNTHETIC_PRIVATE_ERROR')
        return original_get(**kwargs)
    service._collection.get = get
    with pytest.raises(RuntimeError, match='已核对恢复旧索引'):
        service.upsert_video_chunks(VIDEO, '新标题', ['新0', '新1'], [[1., .5], [2., .5]])
    assert service._collection.rows == before


def test_unconfirmed_compensation_is_explicit_unknown_not_a_success():
    service = chroma(); service._collection.fail_upserts = {1, 2}
    with pytest.raises(RuntimeError, match='恢复结果无法确认') as error:
        service.upsert_video_chunks(VIDEO, '新标题', ['新0'], [[1., .5]])
    assert 'PRIVATE' not in str(error.value)


def test_foreign_generated_id_collision_is_rejected_before_any_index_mutation():
    rows = old_rows(); rows[f'{VIDEO}:0']['metadata']['platform_item_id'] = 'foreign-video'
    service = chroma(rows); before = deepcopy(rows)
    with pytest.raises(RuntimeError, match='未写入或删除任何向量'):
        service.upsert_video_chunks(VIDEO, '新标题', ['新0'], [[1., .5]])
    assert service._collection.rows == before
    assert all(row[0] == 'get' for row in service._collection.calls)
