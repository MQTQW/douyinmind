"""Only temporary SQL and fake read-only index observations; never repair."""
from copy import deepcopy
import hashlib
import json
from types import ModuleType, SimpleNamespace
import sys
import uuid

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.entities import KnowledgeDeletionOperation, VideoCache
from app.services.material_audit import audit_materials, validate_index_snapshot
from app.services.material_deletion import _fingerprint
from app.services import chroma_service as index_module


PRIVATE = 'PRIVATE_TRANSCRIPT_KEY_AND_SIGNED_URL_NEVER_IN_REPORT'


@pytest.fixture
def context(tmp_path):
    path = tmp_path / 'audit.sqlite3'
    engine = create_engine(f'sqlite:///{path}')
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    try:
        yield SimpleNamespace(db=db, engine=engine, path=path)
    finally:
        db.close(); engine.dispose()


def cache(c, video='local', status='done'):
    value = VideoCache(platform_item_id=video, title='合成标题', status=status,
                       transcript_text=PRIVATE, analysis_json='{"private":"' + PRIVATE + '"}')
    c.db.add(value); c.db.commit()
    return value


def operation(c, material, *, state='pending', fingerprint=None, cache_id=None):
    value = KnowledgeDeletionOperation(operation_id=str(uuid.uuid4()), platform_item_id=material.platform_item_id,
        cache_id=material.id if cache_id is None else cache_id,
        material_fingerprint=_fingerprint(material) if fingerprint is None else fingerprint,
        state=state, active_key=1)
    c.db.add(value); c.db.commit(); return value


def count_row(video='local', *, source=1, metadata=0, unknown=0):
    return {'platform_item_id':video, 'total_chunks':source + metadata + unknown,
            'verified_content_chunks':source, 'metadata_chunks':metadata, 'unverified_chunks':unknown,
            'content_verified':unknown == 0, 'content_chunks':source if not unknown else None}


def census(rows):
    return {'read_complete':True, 'content_verified':all(row['content_verified'] for row in rows),
            'counts':rows, 'index_records':sum(row['total_chunks'] for row in rows),
            'index_hash':hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
            'verification_mode':'metadata_only', 'source_kind':'live_index_metadata'}


class Reader:
    def __init__(self, *values):
        self.values = values or (census([]),); self.calls = []
    def list_indexed_video_counts(self, **kwargs):
        value = self.values[min(len(self.calls), len(self.values) - 1)]; self.calls.append(kwargs)
        if isinstance(value, BaseException): raise value
        return deepcopy(value)


def reasons(result):
    return [row['reason'] for row in result['issues']]


def test_done_missing_or_explicit_metadata_only_index_are_distinct_confirmed_issues(context):
    cache(context, 'missing'); cache(context, 'metadata')
    result = audit_materials(context.db, chroma=Reader(census([count_row('metadata', source=0, metadata=1)])))
    assert result['read_complete'] and result['content_verified'] and result['repair_supported'] is False
    assert set(reasons(result)) == {'sql_done_without_content_index', 'sql_done_metadata_only_index'}
    assert result['summary']['issue_count'] == result['total_issues'] == 2
    assert PRIVATE not in json.dumps(result) and 'https://' not in json.dumps(result)


def test_orphan_and_non_done_index_records_are_reported_without_returning_source_text(context):
    cache(context, 'interrupted', 'embedding')
    reader = Reader(census([count_row('interrupted'), count_row('orphan')]))
    result = audit_materials(context.db, chroma=reader)
    assert set(reasons(result)) == {'index_for_non_done_material', 'index_without_sql_material'}
    assert result['summary']['sql_done'] == 0 and result['summary']['index_records'] == 2


def test_legacy_content_is_review_unknown_rather_than_zero_or_declared_damage(context):
    cache(context)
    result = audit_materials(context.db, chroma=Reader(census([count_row(source=0, unknown=2)])))
    assert result['read_complete'] is True and result['content_verified'] is False and result['complete'] is False
    assert reasons(result) == ['content_index_unverified']
    assert result['issues'][0]['kind'] == 'review' and result['summary']['issue_count'] == 0
    assert result['summary']['unverified_index_chunks'] == 2


def test_deleting_without_active_intent_is_not_silently_considered_finished(context):
    cache(context, status='deleting')
    result = audit_materials(context.db, chroma=Reader())
    assert reasons(result) == ['deleting_without_active_operation']


@pytest.mark.parametrize('problem,expected', [
    ('state', 'active_deletion_state_mismatch'), ('identity', 'active_deletion_material_changed'),
    ('fingerprint', 'active_deletion_fingerprint_mismatch'),
])
def test_active_deletion_scope_and_fingerprint_are_checked_read_only(context, problem, expected):
    material = cache(context, status='pending' if problem == 'state' else 'deleting')
    operation(context, material, cache_id=material.id + 1 if problem == 'identity' else None,
              fingerprint='0' * 64 if problem == 'fingerprint' else None)
    before = context.path.read_bytes()
    result = audit_materials(context.db, chroma=Reader())
    assert expected in reasons(result) and context.path.read_bytes() == before
    assert PRIVATE not in json.dumps(result)


def test_multiple_active_slots_and_unknown_operation_state_fail_closed_even_on_a_corrupt_schema(context):
    material = cache(context, status='deleting')
    # Only this TEMP table intentionally omits production constraints so the
    # read-only auditor sees a damaged/legacy schema that normal writes reject.
    with context.engine.begin() as connection:
        connection.exec_driver_sql('DROP TABLE knowledge_deletion_operations')
        connection.exec_driver_sql('CREATE TABLE knowledge_deletion_operations(operation_id TEXT, '
            'platform_item_id TEXT,cache_id INTEGER,material_fingerprint TEXT,active_key INTEGER,state TEXT,revision INTEGER)')
        for state in ('pending', 'unexpected_future_state'):
            connection.execute(text('INSERT INTO knowledge_deletion_operations VALUES(:id,:video,:cache,:hash,1,:state,1)'),
                {'id':str(uuid.uuid4()), 'video':material.platform_item_id, 'cache':material.id,
                 'hash':_fingerprint(material), 'state':state})
    before = context.path.read_bytes()
    result = audit_materials(context.db, chroma=Reader())
    assert {'multiple_active_deletions', 'deletion_operation_state_invalid'} <= set(reasons(result))
    assert result['summary']['active_deletion_operations'] == 2 and context.path.read_bytes() == before


@pytest.mark.parametrize('failure_on', ['all', 'second'])
def test_index_read_failure_is_unknown_not_an_empty_index(context, failure_on):
    cache(context)
    error = RuntimeError(PRIVATE)
    reader = Reader(error) if failure_on == 'all' else Reader(census([]), error)
    result = audit_materials(context.db, chroma=reader)
    assert result['read_complete'] is False and result['content_verified'] is None
    assert result['summary']['index_records'] is None and result['summary']['issue_count'] is None
    assert result['total_issues'] is None and 'index_read_unavailable' in reasons(result)
    assert 'sql_done_without_content_index' not in reasons(result)
    assert PRIVATE not in json.dumps(result)


def test_sql_read_failure_does_not_invent_missing_cache_or_zero_material_count(context, monkeypatch):
    cache(context)
    def broken(*args, **kwargs):
        raise OperationalError('synthetic', {}, RuntimeError(PRIVATE))
    monkeypatch.setattr(context.db, 'execute', broken)
    result = audit_materials(context.db, chroma=Reader(census([count_row()])))
    assert result['summary']['sql_materials'] is None and result['summary']['deletion_operations'] is None
    assert result['read_complete'] is False and result['total_issues'] is None
    assert 'index_without_sql_material' not in reasons(result) and PRIVATE not in json.dumps(result)


def test_missing_operation_table_is_unknown_not_proof_of_absent_deletion_intent(context):
    cache(context, status='deleting')
    KnowledgeDeletionOperation.__table__.drop(context.engine)
    result = audit_materials(context.db, chroma=Reader())
    assert 'sql_operation_read_unavailable' in reasons(result)
    assert 'deleting_without_active_operation' not in reasons(result)
    assert result['summary']['deletion_operations'] is None and result['read_complete'] is False


def test_changed_index_snapshot_suppresses_stale_damage_classification(context):
    cache(context)
    result = audit_materials(context.db, chroma=Reader(census([]), census([count_row()])))
    assert result['snapshot_changed'] is True and result['read_complete'] is False
    assert reasons(result) == ['audit_snapshot_changed'] and result['summary']['index_records'] is None


def test_sql_change_between_observations_is_marked_not_reported_as_a_stable_index_failure(context):
    material = cache(context)
    class ChangingReader(Reader):
        def list_indexed_video_counts(self, **kwargs):
            if not self.calls:
                material.status = 'pending'
                context.db.commit()  # Simulates a concurrent writer, not auditor work.
            return super().list_indexed_video_counts(**kwargs)
    result = audit_materials(context.db, chroma=ChangingReader())
    assert result['snapshot_changed'] is True and result['summary']['sql_materials'] is None
    assert reasons(result) == ['audit_snapshot_changed']


def test_fingerprint_read_failure_is_unknown_not_a_claim_that_the_material_changed(context, monkeypatch):
    material = cache(context, status='deleting'); operation(context, material)
    execute = context.db.execute
    def read(statement, *args, **kwargs):
        if 'video_cache.transcript_text' in str(statement):
            raise OperationalError('synthetic', {}, RuntimeError(PRIVATE))
        return execute(statement, *args, **kwargs)
    monkeypatch.setattr(context.db, 'execute', read)
    result = audit_materials(context.db, chroma=Reader())
    assert result['read_complete'] is False and 'sql_fingerprint_read_unavailable' in reasons(result)
    assert 'active_deletion_fingerprint_mismatch' not in reasons(result)
    assert result['summary']['issue_count'] is None


def test_audit_never_autoflushes_dirty_or_new_orm_objects_and_keeps_original_bytes(context):
    material = cache(context)
    material.status = 'pending'
    extra = VideoCache(platform_item_id='not-flushed', title=PRIVATE, status='done')
    context.db.add(extra)
    before = context.path.read_bytes()
    result = audit_materials(context.db, chroma=Reader(census([count_row()])))
    assert result['summary']['sql_materials'] == result['summary']['sql_done'] == 1
    assert material.status == 'pending' and extra in context.db.new
    assert context.path.read_bytes() == before and result['issues'] == []


def test_problem_pagination_is_bounded_deterministic_and_has_no_source_payload(context):
    for i in range(6): cache(context, f'video-{i}')
    first = audit_materials(context.db, chroma=Reader(), limit=2)
    second = audit_materials(context.db, chroma=Reader(), limit=2, offset=2)
    last = audit_materials(context.db, chroma=Reader(), limit=2, offset=4)
    assert len(first['issues']) == len(second['issues']) == len(last['issues']) == 2
    assert first['next_offset'] == 2 and second['next_offset'] == 4 and last['next_offset'] is None
    assert first['total_issues'] == last['total_issues'] == 6
    identities = [item['platform_item_id'] for result in (first, second, last) for item in result['issues']]
    assert len(set(identities)) == 6 and PRIVATE not in json.dumps([first, second, last])


class MetadataCollection:
    def __init__(self, rows):
        self.rows = rows; self.calls = []; self.count_calls = 0; self.fail = False
    def count(self):
        self.count_calls += 1; return len(self.rows)
    def get(self, *, limit, offset, include):
        self.calls.append({'limit':limit, 'offset':offset, 'include':include})
        if self.fail: raise RuntimeError(PRIVATE)
        selected = self.rows[offset:offset + limit]
        value = {'ids':[row[0] for row in selected], 'metadatas':[row[1] for row in selected]}
        if 'documents' in include: value['documents'] = [row[2] for row in selected]
        assert 'embeddings' not in include
        return value


def service(rows):
    value = index_module.ChromaService.__new__(index_module.ChromaService)
    value._collection = MetadataCollection(rows); return value


def test_metadata_census_pages_only_metadata_and_never_returns_private_fields():
    rows = [(f'chunk-{i}', {'platform_item_id':'local', 'chunk_index':i,
             'dm_content_role_version':1, 'dm_content_role':'source' if i else 'metadata',
             'api_key':PRIVATE, 'url':'https://unit.invalid/?signed=' + PRIVATE}, PRIVATE) for i in range(5)]
    value = service(rows)
    result = value.list_indexed_video_counts(page_size=2)
    assert [call['offset'] for call in value._collection.calls] == [0, 2, 4]
    assert all(call['include'] == ['metadatas'] for call in value._collection.calls)
    assert result['counts'][0]['content_chunks'] == 4 and result['index_records'] == 5
    assert PRIVATE not in json.dumps(result) and 'https://' not in json.dumps(result)


@pytest.mark.parametrize('ordinal,document,verified', [
    (0, '这是真实存在的本地合成正文。', 1),
    (1, '合成标题', 0),
    (0, '作者：合成作者', 0),
])
def test_legacy_index_ordinals_never_prove_content_but_explicit_local_verification_can(ordinal, document, verified):
    value = service([('legacy', {'platform_item_id':'local','chunk_index':ordinal,'title':'合成标题'}, document)])
    default = value.list_indexed_video_counts()
    assert default['counts'][0]['content_chunks'] is None and default['content_verified'] is False
    assert value._collection.calls[0]['include'] == ['metadatas']
    result = value.list_indexed_video_counts(verify_legacy_text=True)
    assert result['content_verified'] is True and result['counts'][0]['content_chunks'] == verified
    assert value._collection.calls[1]['include'] == ['metadatas', 'documents']
    assert document not in json.dumps(result, ensure_ascii=False)


def test_explicit_legacy_verification_is_forwarded_but_never_backfills_index_metadata(context):
    cache(context)
    value = service([('legacy-zero', {'platform_item_id':'local','chunk_index':0,'title':'合成标题'},
                      '这是实际存在的合成正文。')])
    before = deepcopy(value._collection.rows)
    result = audit_materials(context.db, chroma=value, verify_legacy_text=True)
    assert result['read_complete'] and result['content_verified'] and result['issues'] == []
    assert all(call['include'] == ['metadatas', 'documents'] for call in value._collection.calls)
    assert value._collection.rows == before


def test_duplicate_index_ids_or_changed_record_count_never_return_a_partial_census():
    rows = [('duplicate', {'platform_item_id':'local'}, PRIVATE)] * 2
    value = service(rows)
    with pytest.raises(index_module.IndexAuditUnavailable) as duplicate:
        value.list_indexed_video_counts(page_size=1)
    assert duplicate.value.reason == 'index_snapshot_changed'
    value = service([('first', {'platform_item_id':'local'}, PRIVATE)])
    counts = iter((1, 2)); value._collection.count = lambda: next(counts)
    with pytest.raises(index_module.IndexAuditUnavailable) as changed:
        value.list_indexed_video_counts()
    assert changed.value.reason == 'index_snapshot_changed'


def test_new_writer_marks_only_explicit_source_headers_or_metadata_without_reclassifying_generic_chunks():
    assert index_module._content_role('语音文字稿（来自视频声音）：\n[00:00–00:02] 合成原句。', '标题') == 'source'
    assert index_module._content_role('画面分析（来自视频画面）：\n白板上可见合成文字。', '标题') == 'source'
    assert index_module._content_role('视频标题：标题\n作者：作者', '标题') == 'metadata'
    assert index_module._content_role('普通旧正文，即使索引为零也不能猜测其类型。', '标题') == 'unverified'


def test_metadata_read_errors_and_scan_limits_are_typed_not_zero():
    value = service([('id', {'platform_item_id':'local'}, PRIVATE)])
    value._collection.fail = True
    with pytest.raises(index_module.IndexAuditUnavailable): value.list_indexed_video_counts()
    value._collection.fail = False
    value._collection.rows.append(('second', {'platform_item_id':'other'}, PRIVATE))
    with pytest.raises(index_module.IndexAuditUnavailable) as error:
        value.list_indexed_video_counts(max_records=1)
    assert error.value.reason == 'index_scan_limit'
    # Invalid options are a programming error, rather than a service outage.
    with pytest.raises(ValueError): value.list_indexed_video_counts(max_records=0)


def test_incomplete_snapshot_fields_are_rejected_instead_of_defaulting_content_to_zero():
    value = census([count_row(source=0, unknown=1)])
    del value['counts'][0]['content_chunks']
    with pytest.raises(ValueError): validate_index_snapshot(value)


def test_constructor_explicitly_disables_telemetry_and_default_embedding_without_opening_a_real_index(monkeypatch):
    sdk = ModuleType('chromadb'); config = ModuleType('chromadb.config'); calls = []
    class Settings:
        def __init__(self, **kwargs): self.options = kwargs
    config.Settings = Settings
    class Client:
        def get_or_create_collection(self, **kwargs):
            calls.append(('collection', kwargs)); return SimpleNamespace(count=lambda:0)
    def create(**kwargs): calls.append(('client', kwargs)); return Client()
    sdk.PersistentClient = create
    monkeypatch.setitem(sys.modules, 'chromadb', sdk); monkeypatch.setitem(sys.modules, 'chromadb.config', config)
    index_module.ChromaService()
    assert calls[0][1]['settings'].options == {'anonymized_telemetry':False}
    assert calls[1][1]['embedding_function'] is None
