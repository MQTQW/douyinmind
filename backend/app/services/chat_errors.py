"""Finite public chat errors; provider text and tracebacks stay out of responses/logs."""
from __future__ import annotations

import logging
import re

from openai import APIConnectionError, APIError, APIResponseValidationError, APITimeoutError
from tenacity import RetryError

from app.services.retrieval_errors import EmbeddingUnavailable


MESSAGES = {
    'CHAT_SESSION_NOT_FOUND': '该历史对话已不存在，请新建对话。',
    'CHAT_SESSION_DELETED': '该历史对话已被删除，请新建对话。',
    'CHAT_SCOPE_MISMATCH': '这段历史对话属于另一个知识库，请切回原知识库或新建对话。',
    'CHAT_AI_NOT_CONFIGURED': 'AI 服务尚未配置，请在本地填写 DEEPSEEK_API_KEY 后重启后台。',
    'CHAT_RETRIEVAL_NOT_CONFIGURED': '语义检索尚未配置，请在本地填写 DASHSCOPE_API_KEY 后重启后台。',
    'CHAT_RETRIEVAL_UNAVAILABLE': '语义检索服务暂时不可用，请稍后重试。',
    'CHAT_RETRIEVAL_INVALID_RESPONSE': '检索服务返回的数据异常，请稍后重试或检查模型配置。',
    'CHAT_RETRIEVAL_AUTH_ERROR': '检索服务访问失败，请检查本地服务密钥、区域和权限。',
    'CHAT_RETRIEVAL_REQUEST_ERROR': '检索服务配置或请求异常，请检查服务区域和模型配置。',
    'CHAT_AI_TIMEOUT': 'AI 服务响应超时，请稍后重试。',
    'CHAT_AI_CONNECTION_ERROR': '暂时无法连接 AI 服务，请稍后重试。',
    'CHAT_AI_AUTH_ERROR': 'AI 服务访问失败，请检查本地服务密钥和权限。',
    'CHAT_AI_RATE_LIMITED': 'AI 服务请求较多，请稍后重试。',
    'CHAT_AI_UNAVAILABLE': 'AI 服务暂时不可用，请稍后重试。',
    'CHAT_AI_INVALID_RESPONSE': 'AI 服务返回的数据异常，请稍后重试。',
    'CHAT_AI_REQUEST_ERROR': 'AI 服务配置或请求异常，请检查模型配置后重试。',
    'CHAT_INTERNAL_ERROR': '处理问题时发生异常，请稍后重试；若持续失败，请检查后台服务。',
}

_KNOWN_MESSAGES = {
    '该历史对话已不存在，请新建对话。': 'CHAT_SESSION_NOT_FOUND',
    '该历史对话已被删除，请新建对话。': 'CHAT_SESSION_DELETED',
    '这段历史对话属于另一个知识库，请切回原知识库或新建对话。': 'CHAT_SCOPE_MISMATCH',
    '请先在 backend/.env 中填写 DEEPSEEK_API_KEY，然后重启后台服务。': 'CHAT_AI_NOT_CONFIGURED',
    '请先填写本地 DASHSCOPE_API_KEY，以便使用语义检索。': 'CHAT_RETRIEVAL_NOT_CONFIGURED',
    '查询检索服务响应格式无效，未使用无效向量。': 'CHAT_RETRIEVAL_INVALID_RESPONSE',
    '查询检索数据返回不完整，未使用无效向量。': 'CHAT_RETRIEVAL_INVALID_RESPONSE',
    '查询检索数据无效，未使用无效向量。': 'CHAT_RETRIEVAL_INVALID_RESPONSE',
}
_QUERY_STATUS = re.compile(
    r'查询检索转换调用失败（状态 (\d{3}|未知)），请检查服务区域、密钥权限和模型配置。'
)


def _retry_cause(error: BaseException) -> BaseException:
    # Preserve actionable configuration/status classification after the existing
    # non-streaming retry wrapper without inspecting arbitrary exception strings.
    for _ in range(3):
        if not isinstance(error, RetryError):
            break
        try:
            nested = error.last_attempt.exception()
        except Exception:
            break
        if not isinstance(nested, BaseException) or nested is error:
            break
        error = nested
    return error


def public_chat_error(error: BaseException) -> dict[str, str]:
    """Classify only types, finite status codes and whole known local messages."""
    error = _retry_cause(error)
    code = None
    if isinstance(error, EmbeddingUnavailable):
        code = 'CHAT_RETRIEVAL_UNAVAILABLE'
    elif isinstance(error, APITimeoutError):
        code = 'CHAT_AI_TIMEOUT'
    elif isinstance(error, APIConnectionError):
        code = 'CHAT_AI_CONNECTION_ERROR'
    elif isinstance(error, APIResponseValidationError):
        code = 'CHAT_AI_INVALID_RESPONSE'
    elif isinstance(error, APIError):
        status = getattr(error, 'status_code', None)
        if type(status) is int and status in (401, 403):
            code = 'CHAT_AI_AUTH_ERROR'
        elif status == 408:
            code = 'CHAT_AI_TIMEOUT'
        elif status == 429:
            code = 'CHAT_AI_RATE_LIMITED'
        elif type(status) is int and 500 <= status <= 599:
            code = 'CHAT_AI_UNAVAILABLE'
        else:
            code = 'CHAT_AI_REQUEST_ERROR'
    elif isinstance(error, (ValueError, RuntimeError)):
        value = error.args[0] if len(error.args) == 1 else None
        if isinstance(value, str):
            code = _KNOWN_MESSAGES.get(value)
            status = _QUERY_STATUS.fullmatch(value)
            if code is None and status:
                code = ('CHAT_RETRIEVAL_AUTH_ERROR' if status[1] in ('401', '403')
                        else 'CHAT_RETRIEVAL_REQUEST_ERROR')
    code = code or 'CHAT_INTERNAL_ERROR'
    return {'code': code, 'message': MESSAGES[code]}


def logged_chat_error(logger: logging.Logger, error: BaseException) -> dict[str, str]:
    payload = public_chat_error(error)
    safe_type = re.sub(r'[^A-Za-z0-9_]', '_', type(error).__name__)[:80]
    logger.error('Chat request failed error_type=%s code=%s', safe_type, payload['code'])
    return payload
