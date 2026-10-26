"""An explicit tool-call transport; leaves the existing web chat unchanged."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelTurn:
    message: dict
    usage: dict
    finish_reason: str = "stop"


class IncompleteModelResponse(RuntimeError):
    pass


class DeepSeekToolModel:
    def __init__(self, *, client: Any = None, model: str | None = None,
                 thinking: bool = False, timeout: float = 45.0):
        self.client = client
        self.model = model
        self.thinking = thinking
        self.timeout = timeout

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
        # Credentials remain inside the established client; never put them in
        # prompts, trace records or tool results. No retry wraps the tool loop.
        client = self.client
        if client is None:
            from app.services.llm_service import llm_client
            client = llm_client._get_client()
        model = self.model
        if model is None:
            from app.core.config import settings
            model = settings.llm_model
        transport = client.with_options(timeout=self.timeout, max_retries=0)
        response = transport.chat.completions.create(
            model=model,
            messages=messages, tools=tools, tool_choice="auto", max_tokens=1500,
            extra_body={"thinking": {"type": "enabled" if self.thinking else "disabled"}},
        )
        finish_reason = response.choices[0].finish_reason
        if finish_reason not in {"stop", "tool_calls"}:
            raise IncompleteModelResponse("model_response_incomplete")
        source = response.choices[0].message
        content = getattr(source, "content", None)
        if content is not None and not isinstance(content, str):
            raise ValueError("模型返回了不支持的消息正文")
        message = {"role": "assistant", "content": content or ""}
        calls = getattr(source, "tool_calls", None) or []
        if (calls and finish_reason != "tool_calls") or (not calls and finish_reason == "tool_calls"):
            raise IncompleteModelResponse("model_response_incomplete")
        if len(calls) > 8:
            raise ValueError("模型单轮工具调用过多")
        if calls:
            message["tool_calls"] = [
                {"id": call.id, "type": call.type,
                 "function": {"name": call.function.name, "arguments": call.function.arguments}}
                for call in calls
            ]
        # Current DeepSeek tool histories require this field throughout the
        # conversation when provided. It stays in memory, never in public output.
        reasoning = getattr(source, "reasoning_content", None)
        if isinstance(reasoning, str):
            message["reasoning_content"] = reasoning
        usage = getattr(response, "usage", None)
        counts = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            number = getattr(usage, key, None)
            if isinstance(number, int) and not isinstance(number, bool):
                counts[key] = number
        return ModelTurn(message, counts, finish_reason)
