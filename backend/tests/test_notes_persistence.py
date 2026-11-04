"""TEMP SQL and inert provider/worker fixtures only; never paid requests."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.models.entities import KnowledgeNoteGeneration as Generation, KnowledgeNoteRequest as Request, KnowledgeNoteStep as Step, VideoCache
from app.services import notes_service as module
from app.services.notes_store import NotesStore, digest, recover_incomplete_notes, erase_material_notes


VIDEO, PRIVATE = 'synthetic-notes', 'SYNTHETIC_PRIVATE_SOURCE_NOT_IN_HISTORY'


class Queue:
    def __init__(self):
        self.items, self.progress = [], []
        self.fail = False
        self.stopping = False
    def is_stopping(self):
        return self.stopping
    @contextmanager
    def submission_guard(self):
        yield
    def submit(self, task_id, func, *args, job_metadata, job_kind):
        assert job_kind == 'notes'
        assert job_metadata == {'platform_item_ids': [VIDEO], 'knowledge_base_id': None}
        self.items.append((task_id, func, args))
        if self.fail: raise OSError('PRIVATE_QUEUE_ERROR')
    def update_progress(self, *args, **kwargs):
        self.progress.append((args, kwargs))
    def execute(self):
        task_id, callback, args = self.items.pop(0)
        callback(*args, task_id=task_id)


class Model:
    def __init__(self):
        self.calls, self.on_call = [], lambda *_: None
        self.fail_at, self.usage, self.observe = None, True, True
        self.identity = {'model': 'synthetic-model', 'endpoint_hash': 'e' * 64, 'credential_hash': 'k' * 64,
                         'generation_parameters_hash': 'g' * 64}
    def configuration_identity(self):
        return deepcopy(self.identity)
    def chat(self, *, observation_sink, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls)
        self.on_call(index)
        if self.observe:
            observation_sink({'call_id': uuid.uuid4().hex, 'request_attempts': 1, 'elapsed_seconds': .01,
                'unknown_outcome': self.fail_at == index, 'usage_complete': self.usage and self.fail_at != index,
                'known_usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10} if self.usage else {},
                'PRIVATE_KEY': PRIVATE, 'documents': PRIVATE})
        if self.fail_at == index: raise TimeoutError('PRIVATE_PROVIDER_ERROR')
        return f'### 内容摘要\n合成笔记第{index}段，忠于已保存资料。'


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "notes.sqlite3"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    db = factory()
    db.add(VideoCache(platform_item_id=VIDEO, title='完整合成标题', status='done', transcript_text=PRIVATE,
        visual_analysis_text='合成画面', analysis_json=json.dumps({'warnings': ['不确定声音'],
            'transcript_segments': [{'start_seconds': 1, 'end_seconds': 3, 'text': PRIVATE}]}, ensure_ascii=False),
        analysis_model='synthetic-recognition', processed_at=datetime.now(timezone.utc)))
    db.commit()
    queue, model = Queue(), Model()
    service = module.NotesService(session_factory=factory, worker=queue, llm=model, ready=lambda: True)
    try:
        yield SimpleNamespace(db=db, engine=engine, factory=factory, queue=queue, model=model, service=service, tmp=tmp_path)
    finally:
        db.close(); engine.dispose()


def submit(c, **kwargs):
    body = {'request_id': str(uuid.uuid4()), 'confirm': True,
            'expected_source_fingerprint': c.service.source_view(c.db, VIDEO)['fingerprint']}
    body.update(kwargs)
    return c.service.submit(c.db, VIDEO, **body), body


def request(c, identity):
    return c.service.lookup(c.db, identity)['item']


def long_source(c):
    cache = c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))
    cache.transcript_text = '甲' * 16000
    c.db.commit()


def test_explicit_generation_saves_fixed_bytes_and_new_uuid_cache_reuses_without_paid_call(context):
    c = context
    response, body = submit(c)
    assert response['item']['state'] == 'queued' and len(c.queue.items) == 1 and not c.model.calls
    duplicate = c.service.submit(c.db, VIDEO, **body)
    assert duplicate['duplicate'] and len(c.queue.items) == 1
    c.queue.execute()
    item = request(c, body['request_id'])
    assert item['state'] == 'done' and item['usage']['state'] == 'complete'
    assert item['usage']['known_tokens']['total_tokens'] == 10 and len(c.model.calls) == 1
    data = c.service.artifact(c.db, item['note_id'])
    assert PRIVATE.encode() in data and '不确定声音'.encode() in data and b'[00:01' in data
    assert PRIVATE not in json.dumps(item) and 'PRIVATE_KEY' not in json.dumps(item)
    assert c.service.artifact(c.db, item['note_id']) == data
    cached, _ = submit(c)
    assert cached['cached'] and cached['item']['note_id'] == item['note_id']
    assert cached['item']['expected_call_count'] == 0 and not c.queue.items and len(c.model.calls) == 1


@pytest.mark.parametrize('field', ['title', 'transcript_text', 'visual_analysis_text', 'analysis_json', 'analysis_model'])
def test_every_saved_source_field_invalidates_new_generation_key_but_old_bytes_stay_fixed(context, field):
    c = context; initial, body = submit(c); c.queue.execute()
    old = request(c, body['request_id']); saved = c.service.artifact(c.db, old['note_id'])
    cache = c.db.scalar(select(VideoCache).where(VideoCache.platform_item_id == VIDEO))
    setattr(cache, field, '{"new":"saved analysis"}' if field == 'analysis_json' else '新版本已保存内容')
    c.db.commit()
    projected = request(c, body['request_id'])
    assert projected['state'] == 'stale' and projected['source_current'] is False
    assert projected['can_download'] and projected['can_redo']
    assert c.service.artifact(c.db, old['note_id']) == saved
    with pytest.raises(module.NotesError, match='资料已变化'):
        submit(c, expected_source_fingerprint=body['expected_source_fingerprint'])
    fresh, _ = submit(c)
    assert not fresh['cached'] and fresh['item']['note_id'] != old['note_id']


@pytest.mark.parametrize('key', ['model', 'credential_hash', 'endpoint_hash', 'generation_parameters_hash'])
def test_actual_model_configuration_changes_do_not_hit_old_cache(context, key):
    c = context; old, _ = submit(c); c.queue.execute()
    c.model.identity[key] = 'new-value'
    new, _ = submit(c)
    assert not new['cached'] and new['item']['note_id'] != old['item']['note_id']


@pytest.mark.parametrize('usage,observe', [(False, True), (True, False)])
def test_returned_content_with_missing_usage_is_saved_not_repaid_or_output_unknown(context, usage, observe):
    c = context; c.model.usage, c.model.observe = usage, observe
    response, body = submit(c); c.queue.execute()
    item = request(c, body['request_id'])
    assert item['state'] == 'done' and item['usage']['state'] == 'partial'
    assert item['usage']['complete'] is False and item['usage']['known_tokens'] == {}
    assert item['can_download'] and len(c.model.calls) == 1
    assert submit(c)[0]['cached'] and len(c.model.calls) == 1


def test_unknown_leaf_never_replays_old_uuid_and_explicit_new_request_reuses_completed_leaf(context):
    c = context; long_source(c); c.model.fail_at = 2
    response, body = submit(c)
    assert response['item']['expected_call_count'] == 3
    with pytest.raises(RuntimeError, match='没有自动重新调用'): c.queue.execute()
    item = request(c, body['request_id'])
    assert item['state'] == 'needs_review' and item['usage']['state'] == 'unknown'
    with pytest.raises(module.NotesError) as no_lineage:
        submit(c)
    assert no_lineage.value.code == 'NOTES_REDO_INVALID'
    steps = NotesStore(c.db).steps(item['note_id'])
    assert [step.state for step in steps] == ['succeeded', 'unknown', 'pending']
    saved = steps[0].output
    assert len(c.model.calls) == 2 and c.service.submit(c.db, VIDEO, **body)['duplicate']
    assert not c.queue.items and len(c.model.calls) == 2
    c.model.fail_at = None
    new, _ = submit(c, redo=True, recovery_of=body['request_id'])
    assert new['item']['reused_step_count'] == 1 and new['item']['expected_call_count'] == 2
    c.queue.execute()
    new_item = request(c, new['item']['request_id'])
    assert new_item['state'] == 'done' and len(c.model.calls) == 4
    assert NotesStore(c.db).steps(new_item['note_id'])[0].output == saved
    assert request(c, body['request_id'])['state'] == 'needs_review'
    assert NotesStore(c.db).steps(item['note_id'])[1].state == 'unknown'


def test_each_leaf_and_merge_has_committed_started_barrier_and_saved_prior_output(context):
    c = context; long_source(c)
    def inspect(index):
        with c.factory() as other:
            steps = list(other.scalars(select(Step).order_by(Step.ordinal)))
            assert steps[index - 1].state == 'started'
            assert all(step.state == 'succeeded' and step.output for step in steps[:index - 1])
            assert not list(other.scalars(select(Generation).where(Generation.state == 'done')))
    c.model.on_call = inspect
    _, body = submit(c); c.queue.execute()
    assert request(c, body['request_id'])['state'] == 'done' and len(c.model.calls) == 3


def test_failed_started_commit_prevents_any_provider_call(context, monkeypatch):
    c = context; response, body = submit(c)
    fault = c.factory(); original = fault.commit; failed = []
    def commit():
        if not failed and fault.scalar(select(Step.state).where(Step.note_id == response['item']['note_id'])) == 'started':
            failed.append(True); raise OSError('PRIVATE_COMMIT_ERROR')
        original()
    monkeypatch.setattr(fault, 'commit', commit); c.service.session_factory = lambda: fault
    with pytest.raises(RuntimeError): c.queue.execute()
    assert not c.model.calls and request(c, body['request_id'])['state'] == 'needs_review'


@pytest.mark.parametrize('lost_ack', [False, True])
def test_successful_provider_result_commit_failure_never_authorizes_another_call(context, monkeypatch, lost_ack):
    c = context; response, body = submit(c)
    fault = c.factory(); original = fault.commit; failed = []
    def commit():
        if not failed and fault.scalar(select(Step.state).where(Step.note_id == response['item']['note_id'])) == 'succeeded':
            failed.append(True)
            if lost_ack: original()
            raise OSError('PRIVATE_COMMIT_ERROR')
        original()
    monkeypatch.setattr(fault, 'commit', commit); c.service.session_factory = lambda: fault
    if lost_ack: c.queue.execute()
    else:
        with pytest.raises(RuntimeError): c.queue.execute()
    item = request(c, body['request_id'])
    assert item['state'] == ('done' if lost_ack else 'needs_review') and len(c.model.calls) == 1
    assert c.service.submit(c.db, VIDEO, **body)['duplicate'] and not c.queue.items
    if not lost_ack:
        step = NotesStore(c.db).steps(item['note_id'])[0]
        assert step.state == 'unknown' and not step.output and item['usage']['state'] == 'unknown'


def test_failed_final_publication_reuses_all_successful_outputs_on_explicit_redo_without_model(context, monkeypatch):
    c = context; response, body = submit(c)
    fault = c.factory(); original = fault.commit; failed = []
    def commit():
        if not failed and fault.scalar(select(Generation.state).where(Generation.note_id == response['item']['note_id'])) == 'done':
            failed.append(True); raise OSError('PRIVATE_FINAL_COMMIT_ERROR')
        original()
    monkeypatch.setattr(fault, 'commit', commit); c.service.session_factory = lambda: fault
    with pytest.raises(RuntimeError): c.queue.execute()
    assert request(c, body['request_id'])['state'] == 'needs_review' and len(c.model.calls) == 1
    c.service.session_factory = c.factory
    new, _ = submit(c, redo=True, recovery_of=body['request_id']); c.queue.execute()
    assert request(c, new['item']['request_id'])['state'] == 'done' and len(c.model.calls) == 1


def test_source_change_after_paid_leaf_preserves_success_and_stops_next_paid_call(context):
    c = context; long_source(c)
    def change(_):
        with c.factory() as other:
            cache = other.scalar(select(VideoCache)); cache.title = '付费响应期间资料变更'; other.commit()
    c.model.on_call = change
    _, body = submit(c)
    with pytest.raises(RuntimeError): c.queue.execute()
    item = request(c, body['request_id'])
    assert item['state'] == 'needs_review' and item['reason_code'] == 'source_changed' and len(c.model.calls) == 1
    assert NotesStore(c.db).steps(item['note_id'])[0].state == 'succeeded'


def test_configuration_change_after_paid_leaf_stops_next_call_and_preserves_known_output(context):
    c = context; long_source(c)
    c.model.on_call = lambda index: c.model.identity.update(generation_parameters_hash='changed')
    _, body = submit(c)
    with pytest.raises(RuntimeError): c.queue.execute()
    item = request(c, body['request_id'])
    assert item['reason_code'] == 'configuration_changed' and len(c.model.calls) == 1
    assert NotesStore(c.db).steps(item['note_id'])[0].state == 'succeeded'


def test_stop_between_started_commit_and_provider_call_keeps_barrier_without_dispatch(context, monkeypatch):
    c = context; _, body = submit(c); original = NotesStore.begin_step
    def begin(store, identity):
        original(store, identity); c.queue.stopping = True
    monkeypatch.setattr(NotesStore, 'begin_step', begin)
    with pytest.raises(RuntimeError): c.queue.execute()
    item = request(c, body['request_id'])
    assert item['reason_code'] == 'worker_stopping' and not c.model.calls
    assert NotesStore(c.db).steps(item['note_id'])[0].state == 'unknown'


def test_restart_quarantines_started_and_queued_generations_without_executing_any_callback(context):
    c = context; initial, body = submit(c)
    store = NotesStore(c.db); assert store.begin_generation(initial['item']['note_id'])
    store.begin_step(store.steps(initial['item']['note_id'])[0].step_id)
    assert recover_incomplete_notes(c.db) == 1
    assert request(c, body['request_id'])['state'] == 'needs_review'
    assert store.steps(initial['item']['note_id'])[0].state == 'unknown'
    assert recover_incomplete_notes(c.db) == 0 and not c.model.calls
    # Even a stale callable left in this synthetic queue has no authority.
    c.queue.execute(); assert not c.model.calls


def test_queue_unknown_preserves_uuid_and_stale_queued_callback_cannot_call_provider(context):
    c = context; c.queue.fail = True; identity = str(uuid.uuid4())
    with pytest.raises(module.NotesError) as error: submit(c, request_id=identity)
    assert error.value.code == 'NOTES_SUBMISSION_UNCONFIRMED'
    assert request(c, identity)['state'] == 'needs_review'
    c.queue.execute(); assert not c.model.calls


@pytest.mark.parametrize('option', ['not_confirmed', 'bad_source', 'redo_without_parent', 'unavailable', 'too_large'])
def test_rejected_generation_never_registers_or_calls_model(context, option):
    c = context; kwargs = {}
    if option == 'not_confirmed': kwargs['confirm'] = False
    if option == 'bad_source': kwargs['expected_source_fingerprint'] = 'f' * 64
    if option == 'redo_without_parent': kwargs['redo'] = True
    if option in {'unavailable', 'too_large'}:
        cache = c.db.scalar(select(VideoCache))
        if option == 'unavailable': cache.status = 'deleting'
        else: cache.transcript_text = '超' * module.MAX_SOURCE_CHARS
        c.db.commit()
    with pytest.raises(module.NotesError): submit(c, **kwargs)
    assert not c.queue.items and not c.model.calls and not list(c.db.scalars(select(Request)))


def test_request_uuid_is_bound_to_original_intent_and_never_reassigned(context):
    c = context; _, body = submit(c)
    with pytest.raises(module.NotesError) as error:
        c.service.submit(c.db, VIDEO, **{**body, 'redo': True})
    assert error.value.code == 'NOTES_REQUEST_CONFLICT' and len(c.queue.items) == 1


def test_maximum_plan_keeps_all_characters_and_never_exceeds_eleven_calls():
    text = ('甲' * 6500 + '\n') * 18
    text += '尾' * (module.MAX_SOURCE_CHARS - len(text))
    parts = module._chunks(text)
    assert len(parts) <= 10 and all(len(part) <= module.CHUNK_CHARS for part in parts)
    assert ''.join(parts).replace('\n', '') == text.replace('\n', '')
    assert parts[-1].endswith('尾')


def test_erasure_is_transactional_keeps_unknown_barrier_and_old_uuid_cannot_read_new_material(context):
    c = context; c.model.fail_at = 1
    old, body = submit(c)
    with pytest.raises(RuntimeError): c.queue.execute()
    erase_material_notes(c.db, VIDEO); c.db.rollback()
    assert NotesStore(c.db).generation(old['item']['note_id']).source_snapshot
    erase_material_notes(c.db, VIDEO); c.db.commit()
    generation = NotesStore(c.db).generation(old['item']['note_id'])
    step = NotesStore(c.db).steps(generation.note_id)[0]
    assert generation.state == 'deleted' and not generation.source_snapshot and not generation.markdown
    assert step.state == 'unknown' and not step.output and step.call_id
    assert request(c, body['request_id'])['usage']['state'] == 'unknown'
    cache = c.db.scalar(select(VideoCache)); cache.transcript_text = '全新入库的文字'; c.db.commit()
    duplicate = c.service.submit(c.db, VIDEO, **body)
    assert duplicate['duplicate'] and duplicate['item']['state'] == 'deleted' and not duplicate['item']['can_redo']
    with pytest.raises(module.NotesError): c.service.artifact(c.db, generation.note_id)
    assert len(c.model.calls) == 1


def test_schema_rejects_unknown_state_or_null_active_slot_for_queued_generation(context):
    c = context; initial, _ = submit(c)
    generation = NotesStore(c.db).generation(initial['item']['note_id']); generation.active_key = None
    with pytest.raises(IntegrityError): c.db.commit()
    c.db.rollback()
    generation = NotesStore(c.db).generation(initial['item']['note_id']); generation.state = 'surprise'
    with pytest.raises(IntegrityError): c.db.commit()
    c.db.rollback()


def test_gets_and_unknown_lookup_do_not_mutate_sql_or_call_model(context):
    c = context; response, body = submit(c); c.queue.execute(); c.db.close()
    path = c.tmp / 'notes.sqlite3'; before = path.read_bytes()
    with c.factory() as db:
        assert c.service.lookup(db, str(uuid.uuid4()))['found'] is False
        assert c.service.history(db, VIDEO)['current_note']['note_id'] == response['item']['note_id']
        assert c.service.lookup(db, body['request_id'])['item']['state'] == 'done'
        assert c.service.artifact(db, response['item']['note_id'])
    assert path.read_bytes() == before and len(c.model.calls) == 1
