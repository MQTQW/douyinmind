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
from numbers import Real
from typing import Iterable, Sequence

from openai import APIConnectionError, APIError, APIResponseValidationError, APITimeoutError, OpenAI
from tenacity import retry, stop_after_attempt, wait_fixed

from app.core.config import settings
from app.services.retrieval_errors import EmbeddingUnavailable

logger = logging.getLogger(__name__)


class LLMClient:
    """
    LLM 客户端

    使用 DeepSeek API（兼容 OpenAI 协议）。
    支持普通对话和流式输出。
    """

    def __init__(self) -> None:
        """
        初始化 DeepSeek 客户端
        """
        self._client: OpenAI | None = None

    def _get_client(self) -> OpenAI:
        """Allow the app to start before credentials are configured."""
        if not settings.deepseek_api_key.strip() or settings.deepseek_api_key.startswith("sk-your-"):
            raise RuntimeError("请先在 backend/.env 中填写 DEEPSEEK_API_KEY，然后重启后台服务。")
        if self._client is None:
            self._client = OpenAI(
                api_key=settings.deepseek_api_key,
                base_url=settings.llm_base_url,
            )
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.6,
        max_tokens: int = 2048,
        timeout: float = 60.0,
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
        started = time.perf_counter()
        response = self._get_client().chat.completions.create(
            extra_body={"thinking": {"type": "enabled" if settings.llm_thinking_enabled else "disabled"}}
            if settings.llm_base_url.startswith("https://api.deepseek.com") else None,
            model=settings.llm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "LLM chat 完成 (%sms, model=%s)", elapsed_ms, settings.llm_model
        )
        return response.choices[0].message.content or ""

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
        started = time.perf_counter()
        stream = self._get_client().chat.completions.create(
            extra_body={"thinking": {"type": "enabled" if settings.llm_thinking_enabled else "disabled"}}
            if settings.llm_base_url.startswith("https://api.deepseek.com") else None,
            model=settings.llm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            stream=True,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "LLM stream 完成 (%sms, model=%s)",
            elapsed_ms,
            settings.llm_model,
        )


class EmbeddingClient:
    """
    Embedding 客户端

    使用 DashScope TextEmbedding API。
    """

    def __init__(self) -> None:
        """
        初始化 DashScope Embedding 客户端
        """
        self._client: OpenAI | None = None
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
        try:
            response = self._get_query_client().embeddings.create(
                model=settings.embedding_model, input=[text],
            )
        except APITimeoutError:
            raise EmbeddingUnavailable("timeout") from None
        except APIConnectionError:
            raise EmbeddingUnavailable("connection") from None
        except APIResponseValidationError:
            raise RuntimeError("查询检索服务响应格式无效，未使用无效向量。") from None
        except APIError as error:
            status = getattr(error, "status_code", None)
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

        items = getattr(response, "data", None)
        if not isinstance(items, list) or len(items) != 1:
            raise RuntimeError("查询检索数据返回不完整，未使用无效向量。")
        item = items[0]
        index = getattr(item, "index", None)
        vector = getattr(item, "embedding", None)
        if (not isinstance(index, int) or isinstance(index, bool) or index != 0
                or not isinstance(vector, (list, tuple))
                or not vector):
            raise RuntimeError("查询检索数据无效，未使用无效向量。")
        try:
            valid = all(isinstance(value, Real) and not isinstance(value, bool)
                        and math.isfinite(value) for value in vector)
        except (OverflowError, TypeError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError("查询检索数据无效，未使用无效向量。")
        return [float(value) for value in vector]

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1))
    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """
        将文本列表转换为向量

        :param texts: 待向量化的文本列表
        :return: 对应的向量列表，每个向量为 float 列表
        :raises RuntimeError: API Key 未配置或调用失败
        """
        if not texts:
            return []

        if not settings.dashscope_api_key.strip():
            raise RuntimeError("请先填写本地 DASHSCOPE_API_KEY，以便将识别结果保存为可搜索的资料。")
        if self._client is None:
            self._client = OpenAI(api_key=settings.dashscope_api_key, base_url=settings.embedding_base_url)

        texts = list(texts)
        started = time.perf_counter()

        embeddings: list[list[float]] = []
        # text-embedding-v4 accepts at most ten texts in each batch.
        for start in range(0, len(texts), 10):
            batch = texts[start:start + 10]
            try:
                resp = self._client.embeddings.create(model=settings.embedding_model, input=batch)
            except APIError as error:
                status = getattr(error, "status_code", None)
                raise RuntimeError(f"资料检索转换调用失败（状态 {status or '网络异常'}），请检查百炼区域、密钥和余额。识别结果已保留。") from None
            items = sorted(resp.data, key=lambda item: item.index)
            if len(items) != len(batch) or [item.index for item in items] != list(range(len(batch))):
                raise RuntimeError("资料检索数据返回不完整，未标记入库成功。")
            if any(not item.embedding or any(not math.isfinite(value) for value in item.embedding) for item in items):
                raise RuntimeError("资料检索数据无效，未标记入库成功。")
            embeddings.extend(item.embedding for item in items)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "Embedding 完成: %d texts, %dms, model=%s",
            len(texts),
            elapsed_ms,
            settings.embedding_model,
        )

        return embeddings

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
