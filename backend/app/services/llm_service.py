"""
LLM 与 Embedding 客户端模块

封装两个上游服务：
1. DeepSeek（OpenAI 兼容协议）—— LLM 对话 + 流式输出
2. DashScope —— 文本向量化（Embedding）
"""
from __future__ import annotations

import logging
import time
import math
import threading
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit
from email.utils import parsedate_to_datetime
from functools import wraps
from numbers import Real
from typing import Iterable, Sequence

from openai import APIConnectionError, APIError, APIResponseValidationError, APIStatusError, APITimeoutError, OpenAI

from app.core.config import settings
from app.services.retrieval_errors import EmbeddingUnavailable
from app.services.service_usage import CallObservation, UsageJournal, reported_usage

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 30.0
EMBEDDING_BATCH_BUDGET_SECONDS = 60.0


class ServiceCallFailure(RuntimeError):
    """Fixed public diagnostics, never provider bodies, URLs or credentials."""

    def __init__(self, reason, status=None, *, embedding=False):
        self.reason, self.http_status = reason, status
        message = (f"资料检索转换调用失败（状态 {status or '网络异常'}），请检查百炼区域、密钥和余额。识别结果已保留。"
                   if embedding else "文字回答服务调用未完成，请稍后重试。")
        super().__init__(message)


class _Budget:
    def __init__(self, seconds, clock):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError('invalid_service_budget')
        self.clock = clock
        self.deadline = clock() + seconds

    def remaining(self):
        return max(0.0, self.deadline - self.clock())


def _status(error):
    value = getattr(error, 'status_code', None)
    return value if type(value) is int and 100 <= value <= 599 else None


def _retry_delay(error, wall_clock):
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', {})
    try:
        raw = headers.get('retry-after')
    except Exception:
        return None
    if raw is None:
        return 1.0
    if type(raw) is not str or len(raw) > 128:
        return None
    try:
        delay = float(raw)
    except ValueError:
        try:
            date = parsedate_to_datetime(raw)
            if date.tzinfo is None:
                return None
            delay = max(0.0, date.timestamp() - wall_clock())
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def _transport(client, remaining):
    # Real SDK instances always support with_options. Small injected providers
    # may implement only create(); they have no hidden SDK retry layer.
    options = getattr(client, 'with_options', None)
    return options(max_retries=0, timeout=remaining) if callable(options) else client


def _single_retry_layer(function):
    """Keep the old __wrapped__ testing/API seam without an outer retry loop."""
    @wraps(function)
    def call(*args, **kwargs):
        return function(*args, **kwargs)
    return call


class _ObservedClient:
    def __init__(self, *, usage_sink=None, clock=None, sleep=None, wall_clock=None):
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._wall_clock = wall_clock or time.time
        self._usage = UsageJournal(usage_sink)

    def get_usage_observations(self, limit=20):
        return self._usage.recent(limit)

    @property
    def last_observation(self):
        rows = self._usage.recent(1)
        return rows[0] if rows else None

    def _request(self, create, budget, observation, batch_index, *, embedding=False):
        """Retry only explicit 429 rejection, within one remaining budget.

        Timeouts, connection failures, 408/5xx and invalid responses can hide
        a paid completed operation. They are never automatically replayed.
        Transport timeouts constrain I/O waits, not an OS-level hard deadline.
        """
        for attempt in range(MAX_ATTEMPTS):
            remaining = budget.remaining()
            if remaining <= 0:
                raise ServiceCallFailure('budget_exhausted', embedding=embedding)
            started = self._clock()
            try:
                response = create(remaining)
            except Exception as error:
                status = _status(error)
                rejected = isinstance(error, APIStatusError) and status is not None and 400 <= status < 500 and status != 408
                observation.attempt(batch_index=batch_index, started=started,
                                    outcome='rejected' if rejected else 'unknown', status=status)
                if not isinstance(error, APIStatusError) or status != 429:
                    raise ServiceCallFailure('provider_rejected' if rejected else 'outcome_unknown',
                                             status, embedding=embedding) from None
                if attempt + 1 == MAX_ATTEMPTS:
                    raise ServiceCallFailure('retry_limit', status, embedding=embedding) from None
                delay = _retry_delay(error, self._wall_clock)
                if delay is None or delay > MAX_RETRY_AFTER_SECONDS:
                    raise ServiceCallFailure('retry_after_limit', status, embedding=embedding) from None
                if delay >= budget.remaining():
                    raise ServiceCallFailure('budget_exhausted', status, embedding=embedding) from None
                if delay:
                    self._sleep(delay)
                continue
            except BaseException:
                observation.attempt(batch_index=batch_index, started=started, outcome='interrupted')
                raise
            return response, started
        raise ServiceCallFailure('retry_limit', embedding=embedding)


class LLMClient(_ObservedClient):
    """
    LLM 客户端

    使用 DeepSeek API（兼容 OpenAI 协议）。
    支持普通对话和流式输出。
    """

    def __init__(self, *, usage_sink=None, clock=None, sleep=None, wall_clock=None) -> None:
        """
        初始化 DeepSeek 客户端
        """
        super().__init__(usage_sink=usage_sink, clock=clock, sleep=sleep, wall_clock=wall_clock)
        self._client: OpenAI | None = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> OpenAI:
        """Allow the app to start before credentials are configured."""
        if not settings.deepseek_api_key.strip() or settings.deepseek_api_key.startswith("sk-your-"):
            raise RuntimeError("请先在 backend/.env 中填写 DEEPSEEK_API_KEY，然后重启后台服务。")
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = OpenAI(api_key=settings.deepseek_api_key,
                                          base_url=settings.llm_base_url, max_retries=0)
        return self._client

    def configuration_identity(self) -> dict[str, str]:
        """Opaque actual transport identity without creating any SDK client.

        No endpoint or credential value is returned, including userinfo/query.
        A credential fingerprint is a local cache discriminator, not a bill.
        """
        client = self._client
        endpoint = settings.llm_base_url if client is None else str(client.base_url)
        credential = settings.deepseek_api_key if client is None else client.api_key
        model = settings.llm_model
        if not all(type(value) is str for value in (endpoint, credential, model)):
            raise RuntimeError('llm_configuration_identity_unavailable')
        try:
            parts = urlsplit(endpoint)
            if parts.scheme not in {'http', 'https'} or not parts.netloc:
                raise ValueError()
            endpoint = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip('/'), parts.query, parts.fragment))
        except (TypeError, ValueError):
            raise RuntimeError('llm_configuration_identity_unavailable') from None
        parameters = {'extra_body': ({'thinking': {'type': 'enabled' if settings.llm_thinking_enabled else 'disabled'}}
                                    if settings.llm_base_url.startswith('https://api.deepseek.com') else None)}
        digest = lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest()
        return {'model': model, 'endpoint_hash': digest(endpoint), 'credential_hash': digest(credential),
                'generation_parameters_hash': digest(json.dumps(parameters, sort_keys=True, separators=(',', ':')))}

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.6,
        max_tokens: int = 2048,
        timeout: float = 60.0,
        *, observation_sink=None,
    ) -> str:
        """
        发送非流式对话请求

        :param system_prompt: 系统提示词
        :param user_prompt: 用户消息
        :param temperature: 温度参数（0-2）
        :param max_tokens: 最大输出 Token 数
        :param timeout: 超时时间（秒）
        :return: LLM 生成的完整回复
        """
        observation = CallObservation('chat', self._usage, clock=self._clock, budget_seconds=timeout,
                                      on_finish=observation_sink)
        try:
            budget = _Budget(timeout, self._clock)
            client = self._get_client()
            call_model = settings.llm_model
            extra_body = ({"thinking": {"type": "enabled" if settings.llm_thinking_enabled else "disabled"}}
                          if settings.llm_base_url.startswith("https://api.deepseek.com") else None)
            def create(remaining):
                return _transport(client, remaining).chat.completions.create(
                    extra_body=extra_body,
                    model=call_model, temperature=temperature, max_tokens=max_tokens, timeout=remaining,
                    messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
            response, started = self._request(create, budget, observation, 0)
            try:
                text = response.choices[0].message.content
                if text is not None and not isinstance(text, str):
                    raise ValueError()
            except (AttributeError, IndexError, TypeError, ValueError):
                observation.attempt(batch_index=0, started=started, outcome='invalid_response', response=response)
                raise ServiceCallFailure('invalid_response') from None
            observation.attempt(batch_index=0, started=started, outcome='succeeded', response=response)
            observation.complete_batch()
            if budget.remaining() <= 0:
                raise ServiceCallFailure('budget_exhausted_after_response')
            observation.finish('succeeded', 'completed')
            return text or ''
        except ServiceCallFailure as error:
            observation.finish('unknown' if observation.data['unknown_outcome'] else 'failed', error.reason)
            raise
        except Exception:
            observation.finish('not_started' if not observation.data['request_attempts'] else 'failed', 'client_error')
            raise
        except BaseException:
            observation.finish('unknown' if observation.data['request_attempts'] else 'not_started', 'interrupted')
            raise

    def stream_chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.6,
        max_tokens: int = 2048,
        timeout: float = 60.0,
    ) -> Iterable[str]:
        """
        发送流式对话请求

        逐 token 返回生成内容，适合 SSE 推送给前端。

        :param system_prompt: 系统提示词
        :param user_prompt: 用户消息
        :param temperature: 温度参数
        :param max_tokens: 最大输出 Token 数
        :param timeout: 超时时间（秒）
        :yield: 每次生成的一小段文本
        """
        budget = _Budget(timeout, self._clock)
        observation = CallObservation('stream_chat', self._usage, clock=self._clock, budget_seconds=timeout)
        stream = usage_response = None
        started = None
        outcome, reason = 'not_started', 'client_error'
        try:
            client = self._get_client()
            if budget.remaining() <= 0:
                raise ServiceCallFailure('budget_exhausted')
            client = _transport(client, budget.remaining())
            started = self._clock()
            outcome, reason = 'interrupted', 'stream_interrupted'
            stream = client.chat.completions.create(
                extra_body={"thinking": {"type": "enabled" if settings.llm_thinking_enabled else "disabled"}}
                if settings.llm_base_url.startswith("https://api.deepseek.com") else None,
                model=settings.llm_model, temperature=temperature, max_tokens=max_tokens,
                timeout=budget.remaining(), stream=True,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}])
            for chunk in stream:
                if reported_usage(chunk):
                    usage_response = chunk
                if budget.remaining() <= 0:
                    raise ServiceCallFailure('budget_exhausted')
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
            if budget.remaining() <= 0:
                raise ServiceCallFailure('budget_exhausted')
            outcome, reason = 'succeeded', 'completed'
            observation.complete_batch()
        except ServiceCallFailure as error:
            reason = error.reason
            raise
        except Exception as error:
            if started is None:
                outcome, reason = 'not_started', 'client_error'
                if isinstance(error, RuntimeError):
                    raise
                raise ServiceCallFailure(reason) from None
            status = _status(error)
            rejected = stream is None and isinstance(error, APIStatusError) and status is not None and 400 <= status < 500 and status != 408
            outcome, reason = ('rejected', 'provider_rejected') if rejected else ('unknown', 'stream_interrupted')
            raise ServiceCallFailure(reason, status) from None
        finally:
            if started is not None:
                observation.attempt(batch_index=0, started=started, outcome=outcome, response=usage_response)
            if stream is not None:
                try:
                    close = getattr(stream, 'close', None)
                    if callable(close):
                        close()
                except Exception:
                    pass
            observation.finish(outcome, reason)


class EmbeddingClient(_ObservedClient):
    """
    Embedding 客户端

    使用 DashScope TextEmbedding API。
    """

    def __init__(self, *, usage_sink=None, clock=None, sleep=None, wall_clock=None,
                 batch_timeout=EMBEDDING_BATCH_BUDGET_SECONDS) -> None:
        """
        初始化 DashScope Embedding 客户端
        """
        super().__init__(usage_sink=usage_sink, clock=clock, sleep=sleep, wall_clock=wall_clock)
        _Budget(batch_timeout, self._clock)
        self._batch_timeout = batch_timeout
        self._client: OpenAI | None = None
        self._client_lock = threading.Lock()
        self._query_client: OpenAI | None = None
        self._query_client_lock = threading.Lock()

    def _get_query_client(self) -> OpenAI:
        """Initialize the short query transport without changing ingestion."""
        if not settings.dashscope_api_key.strip():
            raise RuntimeError("请先填写本地 DASHSCOPE_API_KEY，以便使用语义检索。")
        if self._query_client is None:
            with self._query_client_lock:
                if self._query_client is None:
                    self._query_client = OpenAI(
                        api_key=settings.dashscope_api_key,
                        base_url=settings.embedding_base_url,
                        timeout=settings.query_embedding_timeout_seconds,
                        max_retries=0,
                    )
        return self._query_client

    def embed_query(self, text: str) -> list[float]:
        """Embed one retrieval query once, with classified safe failures.

        Network outages may use the scoped local lexical route. Permission,
        request and payload errors remain explicit errors rather than being
        hidden as successful fallback. The SDK timeout limits transport waits;
        it is not a promise of an exact total wall-clock deadline.
        """
        observation = CallObservation('embed_query', self._usage, clock=self._clock,
                                      budget_seconds=settings.query_embedding_timeout_seconds)
        started = response = None
        def failed(outcome, reason, status=None):
            if started is not None:
                observation.attempt(batch_index=0, started=started, outcome=outcome,
                                    response=response, status=status)
            observation.finish(outcome if started is not None else 'not_started', reason)
        try:
            client = self._get_query_client()
            started = self._clock()
            response = client.embeddings.create(
                model=settings.embedding_model, input=[text],
            )
        except APITimeoutError:
            failed('unknown', 'timeout')
            raise EmbeddingUnavailable("timeout") from None
        except APIConnectionError:
            failed('unknown', 'connection')
            raise EmbeddingUnavailable("connection") from None
        except APIResponseValidationError:
            failed('invalid_response', 'invalid_response')
            raise RuntimeError("查询检索服务响应格式无效，未使用无效向量。") from None
        except APIError as error:
            status = getattr(error, "status_code", None)
            failed('rejected' if type(status) is int and 400 <= status < 500 and status != 408 else 'unknown',
                   'provider_error', _status(error))
            if status == 408:
                raise EmbeddingUnavailable("timeout") from None
            if status == 429:
                raise EmbeddingUnavailable("rate_limit") from None
            if isinstance(status, int) and 500 <= status <= 599:
                raise EmbeddingUnavailable("upstream") from None
            safe_status = status if isinstance(status, int) else "未知"
            raise RuntimeError(
                f"查询检索转换调用失败（状态 {safe_status}），请检查服务区域、密钥权限和模型配置。"
            ) from None
        except Exception:
            failed('unknown', 'client_error')
            raise

        items = getattr(response, "data", None)
        if not isinstance(items, list) or len(items) != 1:
            failed('invalid_response', 'invalid_response')
            raise RuntimeError("查询检索数据返回不完整，未使用无效向量。")
        item = items[0]
        index = getattr(item, "index", None)
        vector = getattr(item, "embedding", None)
        if (not isinstance(index, int) or isinstance(index, bool) or index != 0
                or not isinstance(vector, (list, tuple))
                or not vector):
            failed('invalid_response', 'invalid_response')
            raise RuntimeError("查询检索数据无效，未使用无效向量。")
        try:
            valid = all(isinstance(value, Real) and not isinstance(value, bool)
                        and math.isfinite(value) for value in vector)
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            failed('invalid_response', 'invalid_response')
            raise RuntimeError("查询检索数据无效，未使用无效向量。")
        observation.attempt(batch_index=0, started=started, outcome='succeeded', response=response)
        observation.complete_batch()
        observation.finish('succeeded', 'completed')
        return [float(value) for value in vector]

    @_single_retry_layer
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """
        将文本列表转换为向量

        :param texts: 待向量化的文本列表
        :return: 对应的向量列表，每个向量为 float 列表
        :raises RuntimeError: API Key 未配置或调用失败
        """
        if not texts:
            return []

        texts = list(texts)
        observation = CallObservation('embed_texts', self._usage, clock=self._clock,
                                      budget_seconds=self._batch_timeout, batches=(len(texts) + 9) // 10)
        observation.data['budget_scope'] = 'per_batch'
        embeddings: list[list[float]] = []
        try:
            if not settings.dashscope_api_key.strip():
                raise RuntimeError("请先填写本地 DASHSCOPE_API_KEY，以便将识别结果保存为可搜索的资料。")
            if self._client is None:
                with self._client_lock:
                    if self._client is None:
                        self._client = OpenAI(api_key=settings.dashscope_api_key,
                                              base_url=settings.embedding_base_url,
                                              max_retries=0, timeout=self._batch_timeout)
            # A completed batch stays here; retrying a later batch cannot replay
            # earlier requests or publish partial vectors to the caller.
            for offset in range(0, len(texts), 10):
                batch = texts[offset:offset + 10]
                budget = _Budget(self._batch_timeout, self._clock)
                def create(remaining):
                    return _transport(self._client, remaining).embeddings.create(
                        model=settings.embedding_model, input=batch)
                response, started = self._request(create, budget, observation, offset // 10, embedding=True)
                invalid_message = "资料检索数据返回不完整，未标记入库成功。"
                try:
                    items = response.data
                    if (not isinstance(items, list) or len(items) != len(batch)
                            or any(type(item.index) is not int for item in items)):
                        raise ValueError()
                    items = sorted(items, key=lambda item: item.index)
                    if [item.index for item in items] != list(range(len(batch))):
                        raise ValueError()
                    invalid_message = "资料检索数据无效，未标记入库成功。"
                    vectors = [item.embedding for item in items]
                    if any(not isinstance(vector, (list, tuple)) or not vector
                           or any(not isinstance(value, Real) or isinstance(value, bool)
                                  or not math.isfinite(value) for value in vector) for vector in vectors):
                        raise ValueError()
                    dimension = len(embeddings[0]) if embeddings else len(vectors[0])
                    if any(len(vector) != dimension for vector in vectors):
                        raise ValueError()
                except (AttributeError, TypeError, ValueError, OverflowError):
                    observation.attempt(batch_index=offset // 10, started=started,
                                        outcome='invalid_response', response=response)
                    raise RuntimeError(invalid_message) from None
                observation.attempt(batch_index=offset // 10, started=started,
                                    outcome='succeeded', response=response)
                if budget.remaining() <= 0:
                    raise ServiceCallFailure('budget_exhausted_after_response', embedding=True)
                embeddings.extend([float(value) for value in vector] for vector in vectors)
                observation.complete_batch()
            observation.finish('succeeded', 'completed')
            return embeddings
        except ServiceCallFailure as error:
            observation.finish('unknown' if observation.data['unknown_outcome'] else 'failed', error.reason)
            raise
        except Exception:
            observation.finish('unknown' if observation.data['unknown_outcome'] else 'failed', 'invalid_response_or_configuration')
            raise
        except BaseException:
            observation.finish('unknown' if observation.data['request_attempts'] else 'not_started', 'interrupted')
            raise

    def embed_text(self, text: str) -> list[float]:
        """
        将单条文本转换为向量

        :param text: 待向量化的文本
        :return: 向量
        """
        return self.embed_query(text)


# 全局单例
llm_client = LLMClient()
embedding_client = EmbeddingClient()
