"""Real local Chroma, temporary SQL/media and synthetic vectors only.

Run in the public-code snapshot with the portable guard. A missing native SDK
or a blocked SDK bootstrap is an error, never a skip or a fake native pass.
"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import Base
from app.models.entities import (
    KnowledgeBase, KnowledgeBaseVideo, KnowledgeDeletionOperation, VideoCache,
)
from app.services.chroma_service import ChromaService
from app.services.material_deletion import delete_material
from app.services.worker import Worker


VIDEO = 'native-synthetic'
OTHER = 'native-unrelated'


def snapshot(collection):
    """Compare SDK readback, independent of the service's snapshot helpers."""
    result = collection.get(include=['documents', 'metadatas', 'embeddings'])
    return {
        identifier: {
            'document': result['documents'][index],
            'metadata': deepcopy(result['metadatas'][index]),
            'embedding': [float(number) for number in result['embeddings'][index]],
        }
        for index, identifier in enumerate(result['ids'])
    }


@pytest.fixture
def native_index(tmp_path, monkeypatch):
    path = tmp_path / 'native-chroma'
    monkeypatch.setattr(settings, 'chroma_persist_dir', str(path))
    service = ChromaService()
    assert service._client.get_settings().anonymized_telemetry is False
    assert service._collection._embedding_function is None
    assert service._collection.count() == 0
    try:
        yield service
    finally:
        service._client.close()


def seed(service):
    service.upsert_video_chunks(VIDEO, '合成旧资料', ['旧一', '旧二', '旧三'],
                                [[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]])
    service.upsert_video_chunks(OTHER, '另一份合成资料', ['不得改变'], [[1., 1., 0.]])


def test_native_replacement_removes_old_tail_and_invalid_vectors_preserve_every_row(native_index):
    service = native_index
    seed(service)
    unrelated = snapshot(service._collection)[f'{OTHER}:0']
    assert service.upsert_video_chunks(VIDEO, '合成新资料', ['仅保留这一块'], [[.5, .5, 0.]]) == [f'{VIDEO}:0']
    current = snapshot(service._collection)
    assert set(current) == {f'{VIDEO}:0', f'{OTHER}:0'}
    assert current[f'{VIDEO}:0']['document'] == '仅保留这一块'
    assert current[f'{VIDEO}:0']['metadata']['title'] == '合成新资料'
    assert current[f'{VIDEO}:0']['embedding'] == pytest.approx([.5, .5, 0.], rel=1e-6, abs=1e-8)
    assert current[f'{OTHER}:0'] == unrelated
    for invalid in ([[float('nan'), 0., 1.]], [[True, 0., 1.]],
                    [[1., 0., 0.], [0., 1.]]):
        with pytest.raises(ValueError, match='invalid_vector_batch'):
            service.upsert_video_chunks(VIDEO, '非法输入', ['不能写入'] * len(invalid), invalid)
        assert snapshot(service._collection) == current


def test_native_partial_upsert_fault_restores_old_documents_metadata_and_vectors(native_index, monkeypatch):
    service = native_index
    seed(service)
    actual = service._collection
    before = snapshot(actual)

    class FailOnePartialUpsert:
        def __init__(self):
            self.calls = 0
            self.partial_observed = False

        def get(self, **arguments):
            return actual.get(**arguments)

        def delete(self, **arguments):
            return actual.delete(**arguments)

        def upsert(self, **arguments):
            self.calls += 1
            if self.calls == 1:
                selected = (0, len(arguments['ids']) - 1)
                actual.upsert(**{key: [values[index] for index in selected]
                                 for key, values in arguments.items()})
                partial = snapshot(actual)
                self.partial_observed = (
                    partial[f'{VIDEO}:0']['document'] == '新一'
                    and partial[f'{VIDEO}:3']['document'] == '新四'
                )
                raise OSError('synthetic one-shot acknowledgement loss after partial write')
            return actual.upsert(**arguments)

    fault = FailOnePartialUpsert()
    monkeypatch.setattr(service, '_collection', fault)
    with pytest.raises(RuntimeError, match='已核对恢复旧索引'):
        service.upsert_video_chunks(VIDEO, '合成未完成替换', ['新一', '新二', '新三', '新四'],
                                    [[.5, .5, 0.], [.5, 0., .5], [0., .5, .5], [1., 1., 1.]])
    assert fault.calls == 2 and fault.partial_observed
    assert snapshot(actual) == before


def test_native_material_delete_finishes_all_stores_and_old_done_receipt_protects_reimport(native_index, tmp_path):
    service = native_index
    seed(service)
    unrelated_index = snapshot(service._collection)[f'{OTHER}:0']
    audio, video = tmp_path / 'audio', tmp_path / 'video'
    audio.mkdir()
    video.mkdir()
    owned = [audio / f'{VIDEO}.wav', audio / f'{VIDEO}.wav.analysis.json', video / f'{VIDEO}.mp4']
    for path in owned:
        path.write_bytes(b'synthetic cached media')
    unrelated_media = audio / f'{OTHER}.wav'
    unrelated_media.write_bytes(b'unrelated synthetic cache')
    engine = create_engine(f'sqlite:///{tmp_path / "business.sqlite3"}')
    Base.metadata.create_all(engine)
    worker = Worker()
    try:
        with Session(engine, expire_on_commit=False) as db:
            db.add(KnowledgeBase(id=1, name='合成本地库', name_key='合成本地库'))
            db.add(VideoCache(platform_item_id=VIDEO, title='合成原始资料', status='done',
                              transcript_text='完整合成原文', visual_analysis_text='合成画面',
                              analysis_json='{"synthetic":true}', analysis_model='no-provider',
                              processed_at=datetime.now(timezone.utc)))
            db.flush()
            db.add(KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id=VIDEO))
            db.commit()
            with worker.maintenance_guard():
                result = delete_material(db, VIDEO, chroma=service, audio_dir=audio, video_dir=video)
            db.expire_all()
            cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))
            operation = db.scalar(select(KnowledgeDeletionOperation))
            assert result['deletion_state'] == 'done' and result['operation_id'] == operation.operation_id
            assert operation.state == 'done' and operation.active_key is None
            assert cache.status == 'pending' and cache.transcript_text == cache.visual_analysis_text == ''
            assert cache.analysis_json == '{}' and cache.processed_at is None
            assert list(db.scalars(select(KnowledgeBaseVideo))) == []
            assert all(not path.exists() for path in owned)
            assert snapshot(service._collection) == {f'{OTHER}:0': unrelated_index}

            cache.status, cache.transcript_text = 'done', '后来重新入库的完整新原文'
            cache.analysis_model, cache.processed_at = 'no-provider-new', datetime.now(timezone.utc)
            db.add(KnowledgeBaseVideo(knowledge_base_id=1, platform_item_id=VIDEO))
            db.commit()
            for path in owned:
                path.write_bytes(b'later synthetic import')
            service.upsert_video_chunks(VIDEO, '后来入库', ['后来新块'], [[0., 1., 1.]])
            reimported = snapshot(service._collection)
            with worker.maintenance_guard():
                receipt = delete_material(db, VIDEO, chroma=service, audio_dir=audio, video_dir=video,
                                          expected_operation_id=result['operation_id'])
            db.expire_all()
            assert receipt['already_completed'] is True and receipt['operation_id'] == result['operation_id']
            assert snapshot(service._collection) == reimported
            assert all(path.read_bytes() == b'later synthetic import' for path in owned)
            assert unrelated_media.read_bytes() == b'unrelated synthetic cache'
            assert db.scalar(select(VideoCache.transcript_text).where(VideoCache.platform_item_id == VIDEO)) == '后来重新入库的完整新原文'
            assert len(list(db.scalars(select(KnowledgeBaseVideo)))) == 1
            assert len(list(db.scalars(select(KnowledgeDeletionOperation)))) == 1
            assert worker._thread.ident is None
    finally:
        engine.dispose()
