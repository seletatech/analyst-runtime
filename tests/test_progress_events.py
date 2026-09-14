from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _ToolThenAnswerProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="正在检查当前资料目录。",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(id="call-1", name="list_dir", arguments={"path": "."})
                ],
            )
        return LLMResponse(content="资料目录检查完成。", finish_reason="stop")


class _ReasoningOnlyToolProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content=None,
                reasoning_content="private chain of thought must not be published",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(id="call-1", name="list_dir", arguments={"path": "."})
                ],
            )
        return LLMResponse(content="资料目录检查完成。", finish_reason="stop")


class _ConfiguredToolProvider(LLMProvider):
    def __init__(self, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__()
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls = 0

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="正在读取封存结果。",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="call-sealed",
                        name=self.tool_name,
                        arguments=self.arguments,
                    )
                ],
            )
        return LLMResponse(content="封存结果读取完成。", finish_reason="stop")


class _StaticResultTool(Tool):
    def __init__(self, result: str) -> None:
        self.result = result

    @property
    def name(self) -> str:
        return "sealed_fetch"

    @property
    def description(self) -> str:
        return "Fetch a test sealed artifact."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return self.result


@pytest.mark.asyncio
async def test_agent_loop_reports_public_step_and_tool_completion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ToolThenAnswerProvider(),
        workspace=workspace,
        model="test-model",
    )
    progress: list[tuple[str | None, object | None]] = []

    async def capture(text: str | None, tool: object | None) -> None:
        progress.append((text, tool))

    answer, tools = await loop._run_agent_loop(
        [{"role": "user", "content": "检查资料"}],
        on_progress=capture,
    )

    assert answer == "资料目录检查完成。"
    assert tools == ["list_dir"]
    assert progress == [
        ("正在检查当前资料目录。", None),
        (
            None,
            {
                "detail": ".",
                "id": "call-1",
                "kind": "read",
                "name": "list_dir",
                "status": "running",
            },
        ),
        (
            None,
            {
                "detail": ".",
                "id": "call-1",
                "kind": "read",
                "name": "list_dir",
                "status": "completed",
            },
        ),
    ]


@pytest.mark.asyncio
async def test_agent_loop_does_not_emit_empty_reason_before_tool(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ReasoningOnlyToolProvider(),
        workspace=workspace,
        model="test-model",
    )
    progress: list[tuple[str | None, object | None]] = []

    async def capture(text: str | None, tool: object | None) -> None:
        progress.append((text, tool))

    await loop._run_agent_loop(
        [{"role": "user", "content": "检查资料"}],
        on_progress=capture,
    )

    assert progress[0][1] == {
        "detail": ".",
        "id": "call-1",
        "kind": "read",
        "name": "list_dir",
        "status": "running",
    }
    assert "private chain of thought" not in str(progress)


def test_tool_progress_redacts_credentials_from_public_command() -> None:
    progress = AgentLoop._tool_progress(
        ToolCallRequest(
            id="call-secret",
            name="exec",
            arguments={
                "command": "curl -H 'Authorization: Bearer private-token' "
                "'https://user:url-password@example.test?api_key=sk-secretvalue'"
            },
        ),
        "running",
    )

    assert "private-token" not in progress["detail"]
    assert "sk-secretvalue" not in progress["detail"]
    assert "url-password" not in progress["detail"]
    assert progress["detail"].count("[REDACTED]") >= 2


@pytest.mark.parametrize(
    "tool_result",
    [
        "Error: endpoint returned HTTP 409",
        "subprocess returned non-zero status 9",
        "Transport error: connection reset by peer",
        "HTTP 503 Service Unavailable",
    ],
)
@pytest.mark.asyncio
async def test_agent_loop_marks_error_tool_results_as_failed(
    tmp_path: Path,
    tool_result: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ConfiguredToolProvider("sealed_fetch", {"url": "http://api.test/audit"}),
        workspace=workspace,
        model="test-model",
    )
    loop.tools.register(_StaticResultTool(tool_result))
    progress: list[tuple[str | None, object | None]] = []

    async def capture(text: str | None, tool: object | None) -> None:
        progress.append((text, tool))

    await loop._run_agent_loop(
        [{"role": "user", "content": "读取封存结果"}],
        on_progress=capture,
    )

    assert progress[-1][1] == {
        "detail": "http://api.test/audit",
        "id": "call-sealed",
        "kind": "other",
        "name": "sealed_fetch",
        "status": "failed",
    }


@pytest.mark.asyncio
async def test_agent_loop_reports_only_safe_artifact_identity_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_id = "monthly-event-reconciliation:2025-07:925eff63c622"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_ConfiguredToolProvider("sealed_fetch", {"url": "http://api.test/audit"}),
        workspace=workspace,
        model="test-model",
    )
    loop.tools.register(
        _StaticResultTool(
            json.dumps(
                {
                    "artifact_id": artifact_id,
                    "release_gate": {"passed": True},
                    "private_payload": "must-not-leak",
                }
            )
        )
    )
    progress: list[tuple[str | None, object | None]] = []

    async def capture(text: str | None, tool: object | None) -> None:
        progress.append((text, tool))

    await loop._run_agent_loop(
        [{"role": "user", "content": "读取封存结果"}],
        on_progress=capture,
    )

    terminal = progress[-1][1]
    assert terminal == {
        "detail": "http://api.test/audit",
        "evidence": f"artifact_id={artifact_id}",
        "id": "call-sealed",
        "kind": "other",
        "name": "sealed_fetch",
        "status": "completed",
    }
    assert "must-not-leak" not in str(progress)
