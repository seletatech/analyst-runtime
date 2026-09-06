from types import SimpleNamespace

import pytest

from analyst_runtime.cli.commands import (
    _resolve_consolidation_model,
    _resolve_sandbox_model,
)
from analyst_runtime.model_profiles import resolve_model_profile
from analyst_runtime.providers import litellm_provider as litellm_provider_module
from analyst_runtime.providers.litellm_provider import LiteLLMProvider


def test_zhipu_sandbox_defaults_to_glm_5_3_flash(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)

    assert _resolve_sandbox_model("zhipu") == "zai/glm-5.3-flash"


def test_openrouter_glm_5_3_flash_uses_gateway_route(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_ID", "z-ai/glm-5.3-flash")

    assert _resolve_sandbox_model("openrouter") == "openrouter/z-ai/glm-5.3-flash"


def test_tokenhub_glm_5_3_flash_uses_openai_compatible_route(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)

    assert _resolve_sandbox_model("tokenhub") == "tokenhub/glm-5.3-flash"
    provider = LiteLLMProvider(
        api_base="https://tokenhub.tencentmaas.com/v1",
        api_key="test-key",
        default_model="tokenhub/glm-5.3-flash",
        provider_name="tokenhub",
    )

    assert provider._resolve_model("tokenhub/glm-5.3-flash") == "openai/glm-5.3-flash"


def test_glm_profile_can_route_to_tokenhub_without_changing_product_model_id(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GLM_5_3_FLASH_PROVIDER", "tokenhub")

    profile = resolve_model_profile("glm-5.3-flash")

    assert profile.id == "glm-5.3-flash"
    assert profile.provider == "tokenhub"
    assert profile.model == "tokenhub/glm-5.3-flash"


def test_glm_profile_can_preserve_the_existing_openrouter_route(monkeypatch) -> None:
    monkeypatch.setenv("GLM_5_3_FLASH_PROVIDER", "openrouter")

    profile = resolve_model_profile("glm-5.3-flash")

    assert profile.provider == "openrouter"
    assert profile.model == "openrouter/z-ai/glm-5.3-flash"


def test_glm_profile_defaults_to_the_existing_openrouter_route(monkeypatch) -> None:
    monkeypatch.delenv("GLM_5_3_FLASH_PROVIDER", raising=False)

    profile = resolve_model_profile("glm-5.3-flash")

    assert profile.provider == "openrouter"
    assert profile.model == "openrouter/z-ai/glm-5.3-flash"


def test_glm_profile_rejects_unknown_provider(monkeypatch) -> None:
    monkeypatch.setenv("GLM_5_3_FLASH_PROVIDER", "unknown")

    with pytest.raises(ValueError, match="Unsupported GLM-5.3-Flash provider"):
        resolve_model_profile("glm-5.3-flash")


def test_deepseek_profile_can_route_to_nvidia_without_changing_product_model_id(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_V4_FLASH_PROVIDER", "nvidia")

    profile = resolve_model_profile("deepseek-chat")

    assert profile.id == "deepseek-chat"
    assert profile.provider == "nvidia"
    assert profile.model == "deepseek-ai/deepseek-v4-flash-0731"


def test_nvidia_sandbox_uses_openai_compatible_model_id(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)

    assert _resolve_sandbox_model("nvidia") == "deepseek-ai/deepseek-v4-flash-0731"


def test_zhipu_model_aliases_are_not_double_prefixed(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_ID", "zai/glm-5.3-flash")

    assert _resolve_sandbox_model("zhipu") == "zai/glm-5.3-flash"
    provider = LiteLLMProvider(default_model="zai/glm-5.3-flash")
    assert provider._resolve_model("zai/glm-5.3-flash") == "zai/glm-5.3-flash"


def test_zhipu_consolidation_model_uses_litellm_route(monkeypatch) -> None:
    monkeypatch.setenv("CONSOLIDATION_MODEL_ID", "glm-5.3-flash")

    assert _resolve_consolidation_model("zhipu") == "zai/glm-5.3-flash"


def test_glm_5_3_flash_applies_bigmodel_required_parameters() -> None:
    provider = LiteLLMProvider(default_model="zai/glm-5.3-flash")
    kwargs: dict[str, object] = {}

    provider._apply_model_overrides("zai/glm-5.3-flash", kwargs)

    assert kwargs == {
        "extra_body": {
            "reasoning_effort": "max",
            "thinking": {"clear_thinking": False, "type": "enabled"},
        },
        "temperature": 1.0,
        "top_p": 0.95,
    }


@pytest.mark.asyncio
async def test_glm_5_3_flash_chat_keeps_required_parameters_through_litellm(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content="ok",
                        reasoning_content="checked",
                        tool_calls=None,
                    ),
                )
            ],
            id="glm-response",
            model="glm-5.3-flash",
            usage=SimpleNamespace(
                completion_tokens=1,
                prompt_tokens=1,
                total_tokens=2,
            ),
        )

    monkeypatch.setattr(litellm_provider_module, "acompletion", fake_completion)
    provider = LiteLLMProvider(
        api_base="https://open.bigmodel.cn/api/paas/v4",
        api_key="test-key",
        default_model="zai/glm-5.3-flash",
        provider_name="zhipu",
    )

    response = await provider.chat([{"content": "hello", "role": "user"}])

    assert response.content == "ok"
    assert captured["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
    assert captured["model"] == "zai/glm-5.3-flash"
    assert captured["extra_body"] == {
        "reasoning_effort": "max",
        "thinking": {"clear_thinking": False, "type": "enabled"},
    }
    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 0.95


@pytest.mark.asyncio
async def test_request_credentials_override_provider_only_for_current_task(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=None)
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm_provider_module, "acompletion", fake_completion)
    provider = LiteLLMProvider(api_key="deployment-key", default_model="glm-5.3-flash")
    token = provider.set_request_credentials(api_key="byok-key", provider="deepseek")
    try:
        await provider.chat(
            [{"content": "one", "role": "user"}],
            model="deepseek-v4-flash",
        )
    finally:
        provider.reset_request_credentials(token)
    await provider.chat([{"content": "two", "role": "user"}])

    assert captured[0]["api_key"] == "byok-key"
    assert captured[0]["api_base"] == "https://api.deepseek.com"
    assert captured[0]["model"] == "deepseek/deepseek-v4-flash"
    assert captured[1]["api_key"] == "deployment-key"


@pytest.mark.asyncio
async def test_tokenhub_request_credentials_route_one_task_to_tokenhub(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    async def fake_completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=None)
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm_provider_module, "acompletion", fake_completion)
    provider = LiteLLMProvider(
        api_key="deployment-key",
        default_model="openrouter/z-ai/glm-5.3-flash",
        provider_name="openrouter",
    )
    token = provider.set_request_credentials(api_key="tokenhub-key", provider="tokenhub")
    try:
        await provider.chat(
            [{"content": "one", "role": "user"}],
            model="tokenhub/glm-5.3-flash",
        )
    finally:
        provider.reset_request_credentials(token)

    assert captured[0]["api_key"] == "tokenhub-key"
    assert captured[0]["api_base"] == "https://tokenhub.tencentmaas.com/v1"
    assert captured[0]["model"] == "openai/glm-5.3-flash"


@pytest.mark.asyncio
async def test_nvidia_request_credentials_apply_gateway_route_and_reasoning_parameters(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop", message=SimpleNamespace(content="ok", tool_calls=None)
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm_provider_module, "acompletion", fake_completion)
    provider = LiteLLMProvider(
        api_key="deployment-key",
        default_model="openrouter/z-ai/glm-5.3-flash",
        provider_name="openrouter",
    )
    token = provider.set_request_credentials(api_key="nvidia-key", provider="nvidia")
    try:
        await provider.chat(
            [{"content": "one", "role": "user"}],
            model="deepseek-ai/deepseek-v4-flash-0731",
        )
    finally:
        provider.reset_request_credentials(token)

    assert captured["api_key"] == "nvidia-key"
    assert captured["api_base"] == "https://integrate.api.nvidia.com/v1"
    assert captured["model"] == "openai/deepseek-ai/deepseek-v4-flash-0731"
    assert captured["temperature"] == 1.0
    assert captured["top_p"] == 0.95
    assert captured["max_tokens"] == 16384
    assert captured["extra_body"] == {
        "chat_template_kwargs": {"thinking": True, "reasoning_effort": "high"}
    }
