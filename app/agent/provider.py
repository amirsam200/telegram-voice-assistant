"""LLM provider interface and the DeepSeek (OpenAI-compatible) implementation."""

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # raw JSON string as returned by the model


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    model: str = ""

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return message


class LLMProvider(Protocol):
    model: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...


class DeepSeekProvider:
    """DeepSeek through its OpenAI-compatible API (works with any compatible base URL)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        from openai import OpenAI

        settings = get_settings()
        api_key = api_key or settings.deepseek_api_key
        if not api_key:
            raise LLMError("DEEPSEEK_API_KEY is not set")
        self.model = model or settings.deepseek_model
        self.max_retries = settings.llm_max_retries if max_retries is None else max_retries
        # Retries are handled here, with backoff, so the SDK's own retries are off.
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or settings.deepseek_base_url,
            timeout=timeout_s or settings.llm_timeout_s,
            max_retries=0,
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        import openai

        retryable = (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        )
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                completion = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools or openai.NOT_GIVEN,
                    temperature=0.1,
                )
                break
            except retryable as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise LLMError(f"LLM unavailable after {attempt} attempts: {exc.__class__.__name__}") from exc
                delay = min(2 ** attempt, 20) + random.uniform(0, 0.5)
                logger.warning("LLM call failed (%s); retry %d in %.1fs", exc.__class__.__name__, attempt, delay)
                time.sleep(delay)
            except openai.APIError as exc:
                raise LLMError(f"LLM request failed: {exc.__class__.__name__}: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        choice = completion.choices[0].message
        usage = completion.usage
        return LLMResponse(
            content=choice.content,
            tool_calls=[
                ToolCallRequest(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
                for c in (choice.tool_calls or [])
            ],
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
            model=completion.model or self.model,
        )
