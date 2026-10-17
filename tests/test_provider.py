from types import SimpleNamespace

import httpx
import openai
import pytest

from app.agent import provider as provider_module
from app.agent.provider import DeepSeekProvider, LLMError


def _completion(content: str = "سلام"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3),
        model="deepseek-chat",
    )


class FlakyCompletions:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise openai.APITimeoutError(request=httpx.Request("POST", "http://llm.test"))
        return _completion()


def _provider(failures: int, max_retries: int = 3) -> tuple[DeepSeekProvider, FlakyCompletions]:
    p = DeepSeekProvider(api_key="test", base_url="http://llm.test", model="deepseek-chat", max_retries=max_retries)
    completions = FlakyCompletions(failures)
    p._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return p, completions


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(provider_module.time, "sleep", lambda s: None)


def test_retries_then_succeeds() -> None:
    p, completions = _provider(failures=2)
    response = p.chat([{"role": "user", "content": "hi"}], tools=[])
    assert completions.calls == 3
    assert response.content == "سلام"
    assert (response.prompt_tokens, response.completion_tokens) == (10, 3)


def test_gives_up_after_max_retries() -> None:
    p, completions = _provider(failures=10, max_retries=2)
    with pytest.raises(LLMError):
        p.chat([{"role": "user", "content": "hi"}], tools=[])
    assert completions.calls == 3
