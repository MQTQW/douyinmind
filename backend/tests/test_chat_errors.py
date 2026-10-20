"""Public error boundaries with synthetic markers; no real providers or stores."""
from concurrent.futures import Future
from contextlib import nullcontext
import logging

import httpx2
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from tenacity import RetryError

from app.api.routes import chat
from app.db.base import Base
from app.models.entities import ChatMessage, ChatSession
from app.services.chat_errors import MESSAGES, logged_chat_error, public_chat_error
from app.services import rag_service as production
from app.services.retrieval_errors import EmbeddingUnavailable

PRIVATE = 'SYNTHETIC_PRIVATE_SENTINEL_NOT_A_REAL_SECRET'


def status_error(status):
    request = httpx2.Request('POST', 'https://unit.invalid/'+PRIVATE)
    response = httpx2.Response(status, request=request, headers={'synthetic-private': PRIVATE})
    return openai.APIStatusError(PRIVATE, response=response, body={'message': PRIVATE})


@pytest.mark.parametrize('status,code', [
    (400, 'CHAT_AI_REQUEST_ERROR'), (401, 'CHAT_AI_AUTH_ERROR'), (403, 'CHAT_AI_AUTH_ERROR'),
    (404, 'CHAT_AI_REQUEST_ERROR'), (408, 'CHAT_AI_TIMEOUT'), (422, 'CHAT_AI_REQUEST_ERROR'),
    (429, 'CHAT_AI_RATE_LIMITED'), (500, 'CHAT_AI_UNAVAILABLE'), (503, 'CHAT_AI_UNAVAILABLE'),
])
def test_provider_status_maps_without_message_url_body_headers_or_logs(status, code, caplog):
    with caplog.at_level(logging.ERROR):
        payload = logged_chat_error(logging.getLogger('synthetic.chat'), status_error(status))
    assert payload == {'code': code, 'message': MESSAGES[code]}
    assert PRIVATE not in str(payload)+caplog.text
    assert 'Traceback' not in caplog.text


@pytest.mark.parametrize('message,code', [
    ('该历史对话已不存在，请新建对话。', 'CHAT_SESSION_NOT_FOUND'),
    ('该历史对话已被删除，请新建对话。', 'CHAT_SESSION_DELETED'),
    ('这段历史对话属于另一个知识库，请切回原知识库或新建对话。', 'CHAT_SCOPE_MISMATCH'),
    ('请先在 backend/.env 中填写 DEEPSEEK_API_KEY，然后重启后台服务。', 'CHAT_AI_NOT_CONFIGURED'),
    ('请先填写本地 DASHSCOPE_API_KEY，以便使用语义检索。', 'CHAT_RETRIEVAL_NOT_CONFIGURED'),
    ('查询检索数据无效，未使用无效向量。', 'CHAT_RETRIEVAL_INVALID_RESPONSE'),
    ('查询检索转换调用失败（状态 401），请检查服务区域、密钥权限和模型配置。', 'CHAT_RETRIEVAL_AUTH_ERROR'),
    ('查询检索转换调用失败（状态 404），请检查服务区域、密钥权限和模型配置。', 'CHAT_RETRIEVAL_REQUEST_ERROR'),
])
def test_only_complete_known_local_messages_keep_actionable_information(message, code):
    assert public_chat_error(RuntimeError(message)) == {'code': code, 'message': MESSAGES[code]}
    assert public_chat_error(RuntimeError(message+' '+PRIVATE))['code'] == 'CHAT_INTERNAL_ERROR'


def test_retry_wrapper_preserves_configuration_error_without_rendering_its_repr():
    future = Future()
    future.set_exception(RuntimeError('请先在 backend/.env 中填写 DEEPSEEK_API_KEY，然后重启后台服务。'))
    assert public_chat_error(RetryError(future))['code'] == 'CHAT_AI_NOT_CONFIGURED'


def test_typed_outage_is_safe_even_if_a_wrapper_attaches_private_text():
    error = EmbeddingUnavailable('connection')
    error.args = (PRIVATE,)
    assert public_chat_error(error) == {'code': 'CHAT_RETRIEVAL_UNAVAILABLE',
                                        'message': MESSAGES['CHAT_RETRIEVAL_UNAVAILABLE']}


def test_unknown_program_or_index_error_remains_explicit_error(caplog):
    for error in [RuntimeError(PRIVATE), ValueError(PRIVATE), TypeError(PRIVATE)]:
        result = logged_chat_error(logging.getLogger('synthetic.unknown'), error)
        assert result['code'] == 'CHAT_INTERNAL_ERROR'
        assert '异常' in result['message']
    assert PRIVATE not in caplog.text


def test_http_and_outer_sse_error_responses_and_logs_never_include_raw_provider_data(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError(PRIVATE)
    app = FastAPI()
    app.include_router(chat.router)
    app.dependency_overrides[chat.get_db] = lambda: object()
    monkeypatch.setattr(chat.rag_service, 'answer', fail)
    monkeypatch.setattr(chat.rag_service, 'answer_stream', fail)
    monkeypatch.setattr(chat, 'session_factory', lambda: nullcontext(object()))
    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        normal = client.post('/chat/ask', json={'query': '有效问题'})
        streamed = client.post('/chat/ask/stream', json={'query': '有效问题'})
    assert normal.json() == {'success': False, 'code': 'CHAT_INTERNAL_ERROR',
                             'message': MESSAGES['CHAT_INTERNAL_ERROR']}
    assert 'event: error' in streamed.text and 'CHAT_INTERNAL_ERROR' in streamed.text
    assert PRIVATE not in normal.text+streamed.text+caplog.text
    assert 'Traceback' not in caplog.text


def test_inner_stream_failure_keeps_delivered_text_but_does_not_persist_or_leak(monkeypatch, caplog):
    def fail(**kwargs):
        yield '部分文字'
        raise status_error(503)
    service = production.RagService()
    monkeypatch.setattr(service, '_resolve_scope', lambda *args: set())
    monkeypatch.setattr(production.llm_client, 'stream_chat', fail)
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db, caplog.at_level(logging.ERROR):
        events = list(service.answer_stream(db, '你好', None))
        assert ('delta', {'text': '部分文字'}) in events
        assert events[-1] == ('error', {'code': 'CHAT_AI_UNAVAILABLE', 'message': MESSAGES['CHAT_AI_UNAVAILABLE']})
        assert db.scalar(select(func.count()).select_from(ChatSession)) == 0
        assert db.scalar(select(func.count()).select_from(ChatMessage)) == 0
    engine.dispose()
    assert PRIVATE not in str(events)+caplog.text


def test_early_scope_rejection_preserves_known_business_message(monkeypatch):
    service = production.RagService()
    def mismatch(*args):
        raise ValueError('这段历史对话属于另一个知识库，请切回原知识库或新建对话。')
    monkeypatch.setattr(service, '_validate_session_scope', mismatch)
    monkeypatch.setattr(production.llm_client, 'stream_chat', lambda **kwargs: pytest.fail('scope mismatch cannot call model'))
    events = list(service.answer_stream(object(), '它是什么意思？', 1, knowledge_base_id=2))
    assert events == [('error', {'code': 'CHAT_SCOPE_MISMATCH', 'message': MESSAGES['CHAT_SCOPE_MISMATCH']})]


def test_index_error_is_not_turned_into_success_or_empty_library(monkeypatch, caplog):
    service = production.RagService()
    monkeypatch.setattr(service, '_resolve_scope', lambda *args: {'local'})
    def fail(*args):
        raise RuntimeError(PRIVATE)
    monkeypatch.setattr(service, '_dense_retrieve', fail)
    monkeypatch.setattr(production.llm_client, 'stream_chat', lambda **kwargs: pytest.fail('index errors cannot generate an answer'))
    app = FastAPI()
    app.include_router(chat.router)
    monkeypatch.setattr(chat, 'session_factory', lambda: nullcontext(object()))
    monkeypatch.setattr(chat.rag_service, 'answer_stream', service.answer_stream)
    with TestClient(app) as client:
        response = client.post('/chat/ask/stream', json={'query': '检修前如何关闭电源？'})
    assert 'CHAT_INTERNAL_ERROR' in response.text
    assert 'event: done' not in response.text and 'event: delta' not in response.text
    assert PRIVATE not in response.text+caplog.text
