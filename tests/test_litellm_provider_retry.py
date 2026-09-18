from __future__ import annotations

from types import SimpleNamespace

import pytest

from analyst_runtime.providers import litellm_provider
from analyst_runtime.providers.litellm_provider import LiteLLMProvider


def _response(content: str):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=None)


@pytest.mark.asyncio
async def test_chat_retries_transient_empty_provider_response(monkeypatch):
    attempts = 0

    async def fake_completion(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Unable to get json response - Expecting value")
        return _response("recovered")

    monkeypatch.setattr(litellm_provider, "acompletion", fake_completion)
    monkeypatch.setattr(litellm_provider, "_LLM_RETRY_DELAYS_SECONDS", (0, 0))
    provider = LiteLLMProvider(default_model="deepseek/deepseek-v4-flash")

    response = await provider.chat(messages=[{"role": "user", "content": "analyze"}])

    assert attempts == 2
    assert response.content == "recovered"
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_does_not_retry_permanent_authentication_error(monkeypatch):
    attempts = 0

    async def fake_completion(**kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("Authentication fails: invalid API key")

    monkeypatch.setattr(litellm_provider, "acompletion", fake_completion)
    monkeypatch.setattr(litellm_provider, "_LLM_RETRY_DELAYS_SECONDS", (0, 0))
    provider = LiteLLMProvider(default_model="deepseek/deepseek-v4-flash")

    response = await provider.chat(messages=[{"role": "user", "content": "analyze"}])

    assert attempts == 1
    assert response.finish_reason == "error"
    assert "Authentication fails" in response.content

