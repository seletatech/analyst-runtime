from copy import deepcopy
from types import SimpleNamespace

import pytest

from analyst_runtime.agent.context import ContextBuilder
from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.providers.base import LLMResponse, ToolCallRequest
from analyst_runtime.providers.litellm_provider import LiteLLMProvider


def test_session_identity_does_not_change_cached_system_prefix(tmp_path):
    builder = ContextBuilder(tmp_path, minimal=True)
    first = builder.build_messages([], "hello", channel="web", chat_id="run-1")
    second = builder.build_messages([], "hello", channel="web", chat_id="run-2")
    assert first[0]["content"][0] == second[0]["content"][0]
    assert "Current Time" not in builder.build_system_prompt()
    assert "run-1" in first[0]["content"][1]["text"]
    assert "run-2" in second[0]["content"][1]["text"]


@pytest.mark.parametrize("details", [{"cached_tokens": 2048}, SimpleNamespace(cached_tokens=2048)])
def test_openai_compatible_cache_usage_is_recorded(details):
    provider = LiteLLMProvider(provider_name="nebius")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=3000,
            completion_tokens=10,
            total_tokens=3010,
            prompt_tokens_details=details,
        ),
    )
    parsed = provider._parse_response(response)
    assert parsed.usage["cache_read_tokens"] == 2048
    assert parsed.usage["prompt_tokens"] == 3000


@pytest.mark.asyncio
async def test_active_loop_bounds_tool_results_without_mutating_previous_prefix(tmp_path):
    payload = "evidence row\n" * 10000
    source = tmp_path / "large.txt"
    source.write_text(payload)
    calls = []

    class Provider:
        def get_default_model(self):
            return "test-model"

        async def chat(self, messages, **kwargs):
            calls.append(deepcopy(messages))
            if len(calls) < 3:
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            id=f"read-{len(calls)}",
                            name="read_file",
                            arguments={"path": str(source)},
                        )
                    ],
                )
            return LLMResponse(content="done")

    loop = AgentLoop(bus=MessageBus(), provider=Provider(), workspace=tmp_path)
    await loop._run_agent_loop([{"role": "user", "content": "read"}])
    result = next(m for m in calls[1] if m["role"] == "tool")
    assert result["content"].startswith("evidence row")
    assert len(result["content"]) < loop._INLINE_RESULT_CHARS + 300
    assert (tmp_path / "sessions/tool-results/read-1.txt").read_text() == payload
    assert calls[2][: len(calls[1])] == calls[1]
