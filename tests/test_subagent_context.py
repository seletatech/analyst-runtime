from pathlib import Path

from analyst_runtime.agent.subagent import SubagentManager
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.providers.base import LLMProvider, LLMResponse


class _Provider(LLMProvider):
    async def chat(self, *args, **kwargs) -> LLMResponse:
        return LLMResponse(content="done")

    def get_default_model(self) -> str:
        return "test-model"


def test_subagent_inherits_workspace_semantics_policy(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "读取业务数据前必须先确认 semantics。",
        encoding="utf-8",
    )
    manager = SubagentManager(_Provider(), tmp_path, MessageBus())

    prompt = manager._build_subagent_prompt("分析生产数据")

    assert "读取业务数据前必须先确认 semantics" in prompt
    assert "task must include the\n   user's confirmed definitions and scope" in prompt
