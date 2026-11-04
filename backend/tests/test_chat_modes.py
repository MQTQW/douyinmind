"""Explicit AI-only sessions cannot accidentally retrieve saved documents."""
import pytest
from sqlalchemy import create_engine, select, text

from app.models.entities import ChatMessage, ChatSession, VideoCache
from app.db.migrations import ensure_chat_mode_column
from tests.test_chat_stream import rag_module, db


def no_retrieval(service, module, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('AI-only must not read Chroma, saved documents or embedding')
    for name in ('_resolve_scope', '_dense_retrieve', '_db_content_hits', '_resolve_collection_scope'):
        monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(module, 'get_chroma_service', forbidden)
    monkeypatch.setattr(module.llm_client, 'chat', lambda **kwargs: '纯 AI 回答。', raising=False)


@pytest.mark.parametrize('streaming', [False, True])
def test_explicit_ai_mode_skips_every_saved_document_path_and_is_persisted(rag_module, db, monkeypatch, streaming):
    service = rag_module.RagService()
    no_retrieval(service, rag_module, monkeypatch)
    query = '收藏里有哪些视频？帮我总结所有资料。'
    if streaming:
        events = list(service.answer_stream(db, query, None, 'all', chat_only=True))
        result = next(data for kind, data in events if kind == 'meta')
        assert events[-1] == ('done', {'ok': True})
    else:
        result = service.answer(db, query, None, 'all', chat_only=True)
    assert result['route_type'] == 'direct' and result['sources'] == [] and result['chat_only'] is True
    saved = db.get(ChatSession, result['session_id'])
    assert saved.chat_only is True and saved.knowledge_base_id is None
    assert service.list_sessions(db)[0]['chat_only'] is True
    # Omitted mode on a legacy client request inherits this immutable session.
    follow = list(service.answer_stream(db, '再解释一下', saved.id))
    assert next(data for kind, data in follow if kind == 'meta')['chat_only'] is True
    assert len(list(db.scalars(select(ChatMessage)))) == 4


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('scope', [{'knowledge_base_id': 1}, {'collection_id': 'specific-collection'}])
def test_ai_mode_cannot_hide_a_knowledge_scope(rag_module, db, monkeypatch, streaming, scope):
    service = rag_module.RagService()
    no_retrieval(service, rag_module, monkeypatch)
    if streaming:
        events = list(service.answer_stream(db, '问题', None, chat_only=True, **scope))
        assert len(events) == 1 and events[0][0] == 'error'
        assert events[0][1]['code'] == 'CHAT_MODE_INVALID'
    else:
        with pytest.raises(ValueError, match='纯 AI 对话不能'):
            service.answer(db, '问题', None, chat_only=True, **scope)
    assert list(db.scalars(select(ChatMessage))) == []


@pytest.mark.parametrize('original,requested', [(True, False), (False, True)])
def test_existing_session_mode_cannot_be_changed_by_login_or_request(rag_module, db, original, requested):
    service = rag_module.RagService()
    saved = ChatSession(title='固定模式', chat_only=original)
    db.add(saved); db.commit()
    events = list(service.answer_stream(db, '继续', saved.id, chat_only=requested))
    assert events == [('error', {'code': 'CHAT_MODE_MISMATCH',
        'message': '这段历史对话的资料模式已固定，请恢复原模式或新建对话。'})]
    assert db.get(ChatSession, saved.id).chat_only is original
    assert list(db.scalars(select(ChatMessage))) == []


def test_ai_mode_prompt_is_explicit_and_never_claims_collection_access(rag_module, db, monkeypatch):
    service = rag_module.RagService(); no_retrieval(service, rag_module, monkeypatch)
    captured = []
    def answer(**kwargs):
        captured.append(kwargs)
        return '这是普通对话。'
    monkeypatch.setattr(rag_module.llm_client, 'chat', answer)
    result = service.answer(db, '你好', None, chat_only=True)
    assert '不检索或使用本地知识库和收藏资料' in captured[0]['system_prompt']
    assert result['trace']['context_stats']['provided_chunks'] == 0


def test_legacy_schema_migration_preserves_old_rows_and_defaults_to_knowledge():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE chat_sessions(id INTEGER PRIMARY KEY,title TEXT,knowledge_base_id INTEGER)'))
        connection.execute(text("INSERT INTO chat_sessions VALUES(1,'保留的历史',NULL)"))
    ensure_chat_mode_column(engine); ensure_chat_mode_column(engine)
    with engine.connect() as connection:
        assert connection.execute(text('SELECT id,title,chat_only FROM chat_sessions')).all() == [(1,'保留的历史',0)]
    engine.dispose()


def test_request_mode_is_strict_and_backend_capability_is_explicit(rag_module):
    from pydantic import ValidationError
    from app.api.routes.chat import AskRequest, chat_capabilities
    assert AskRequest(query='问题').chat_only is None
    assert AskRequest(query='问题', chat_only=True).chat_only is True
    for value in ('true', 'false', 0, 1):
        with pytest.raises(ValidationError):
            AskRequest(query='问题', chat_only=value)
    assert chat_capabilities()['chat_only_sessions'] is True


def test_all_local_scope_uses_sql_completion_and_excludes_stale_or_deleting_vectors(rag_module, db):
    for identity, status in [('ready','done'), ('partial','embedding'), ('removed','deleting'), ('failed','failed')]:
        db.add(VideoCache(platform_item_id=identity, title=identity, status=status))
    db.commit()
    assert rag_module.RagService()._resolve_scope(db, None, None) == {'ready'}


def test_collection_scope_is_frozen_and_omission_cannot_widen_history(rag_module, db, monkeypatch):
    service = rag_module.RagService()
    seen = []
    monkeypatch.setattr(service, '_resolve_scope', lambda database, collection, knowledge: seen.append(collection) or set())
    monkeypatch.setattr(rag_module.llm_client, 'chat', lambda **kwargs: '你好。', raising=False)
    first = service.answer(db, '你好', None, collection_id='original-folder', chat_only=False)
    assert first['collection_id'] == 'original-folder'
    second = service.answer(db, '你好', first['session_id'])
    assert second['collection_id'] == 'original-folder' and seen == ['original-folder','original-folder']
    events = list(service.answer_stream(db, '继续', first['session_id'], collection_id='all'))
    assert events[0][0] == 'error' and events[0][1]['code'] == 'CHAT_SCOPE_MISMATCH'
    assert service.list_sessions(db)[0]['collection_id'] == 'original-folder'
    assert service.list_sessions(db)[0]['created_at'].endswith('Z')
    assert all(item['created_at'].endswith('Z') for item in service.get_messages(db, first['session_id']))
