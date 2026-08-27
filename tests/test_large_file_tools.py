from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import MemoryStore
from nanobot.agent.tools.filesystem import (
    AppendFileTool,
    EditFileTool,
    PatchFileTool,
    WriteFileTool,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (WriteFileTool(), {"content": "overwritten"}),
        (AppendFileTool(), {"content": "\nappended"}),
        (
            PatchFileTool(),
            {"patches": [{"old_text": "Approved fact", "new_text": "forged fact"}]},
        ),
        (EditFileTool(), {"old_text": "Approved fact", "new_text": "forged fact"}),
    ],
)
async def test_generic_file_tools_cannot_modify_project_memory_in_unrestricted_mode(
    tmp_path: Path,
    tool,
    arguments: dict,
) -> None:
    workspace = tmp_path / "workspace"
    store = MemoryStore(workspace)
    store.write_long_term("# Long-term Memory\n\nApproved fact")
    original = store.read_long_term()

    result = await tool.execute(path=str(store.memory_file), **arguments)

    assert result.startswith("Error:")
    assert "MemoryStore" in result
    assert store.read_long_term() == original


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (WriteFileTool(), {"content": "forged intent"}),
        (AppendFileTool(), {"content": "forged intent"}),
        (
            PatchFileTool(),
            {"patches": [{"old_text": "trusted", "new_text": "forged"}]},
        ),
        (EditFileTool(), {"old_text": "trusted", "new_text": "forged"}),
    ],
)
async def test_generic_file_tools_cannot_forge_confirmation_journal(
    tmp_path: Path,
    tool,
    arguments: dict,
) -> None:
    target = (
        tmp_path
        / "workspace"
        / "memory"
        / "confirmation-intents"
        / "pending"
        / "confirmation-forged.json"
    )

    result = await tool.execute(path=str(target), **arguments)

    assert result.startswith("Error:")
    assert not target.exists()


async def test_append_file_tool_creates_and_appends_chunks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "site" / "index.html"
    tool = AppendFileTool(allowed_dir=workspace)

    result1 = await tool.execute(path=str(path), content="<html>")
    result2 = await tool.execute(path=str(path), content="Hello</html>")

    assert "Successfully appended 6 bytes" in result1
    assert "Successfully appended 12 bytes" in result2
    assert path.read_text(encoding="utf-8") == "<html>Hello</html>"


async def test_patch_file_tool_repairs_selected_sections(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "site" / "index.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("<h1>Draft</h1>\n<p>Teh copy</p>\n", encoding="utf-8")
    tool = PatchFileTool(allowed_dir=workspace)

    result = await tool.execute(
        path=str(path),
        patches=[
            {"old_text": "<h1>Draft</h1>", "new_text": "<h1>Final</h1>"},
            {"old_text": "Teh copy", "new_text": "The copy"},
        ],
    )

    assert "Successfully patched 2 section(s)" in result
    assert path.read_text(encoding="utf-8") == "<h1>Final</h1>\n<p>The copy</p>\n"


async def test_registry_suggests_chunked_recovery_for_truncated_write_calls() -> None:
    registry = ToolRegistry()
    registry.register(WriteFileTool())

    result = await registry.execute("write_file", {"path": "/workspace/site/index.html"})

    assert "Invalid parameters" in result
    assert "append_file" in result
    assert "patch_file" in result


def test_system_prompt_guides_large_file_write_recovery(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "append_file" in prompt
    assert "patch_file" in prompt
    assert "large generated files" in prompt


async def test_agent_loop_recovers_from_truncated_large_file_tool_calls(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = workspace / "site" / "index.html"

    class FakeProvider(LLMProvider):
        def __init__(self, target_path: str):
            super().__init__()
            self.target_path = target_path
            self.calls: list[list[dict]] = []

        async def chat(
            self,
            messages: list[dict],
            tools: list[dict] | None = None,
            model: str | None = None,
            max_tokens: int = 4096,
            temperature: float = 0.7,
        ) -> LLMResponse:
            self.calls.append(messages)

            if len(self.calls) == 1:
                return LLMResponse(
                    content="Writing HTML in chunks.",
                    tool_calls=[
                        ToolCallRequest(
                            id="tc1",
                            name="append_file",
                            arguments={"path": self.target_path},
                        )
                    ],
                )

            if len(self.calls) == 2:
                recovery_hints = [
                    message["content"]
                    for message in messages
                    if message.get("role") == "system"
                    and "Do not use exec" in message.get("content", "")
                ]
                assert recovery_hints
                assert "under 8000 characters" in recovery_hints[-1]
                return LLMResponse(
                    content=None,
                    tool_calls=[
                        ToolCallRequest(
                            id="tc2",
                            name="write_file",
                            arguments={"path": self.target_path, "content": ""},
                        ),
                        ToolCallRequest(
                            id="tc3",
                            name="append_file",
                            arguments={"path": self.target_path, "content": "<html>"},
                        ),
                        ToolCallRequest(
                            id="tc4",
                            name="append_file",
                            arguments={"path": self.target_path, "content": "Hello</html>"},
                        ),
                    ],
                )

            return LLMResponse(content="done")

        def get_default_model(self) -> str:
            return "fake-model"

    provider = FakeProvider(str(path))
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    response = await agent.process_direct("Create a large HTML file.")

    assert response == "done"
    assert path.read_text(encoding="utf-8") == "<html>Hello</html>"


async def test_registry_suggests_chunked_recovery_for_truncated_append_calls(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    registry.register(AppendFileTool())

    result = await registry.execute("append_file", {"path": str(tmp_path / "index.html")})

    assert "Invalid parameters" in result
    assert "append_file" in result
    assert "Do not switch to exec" in result
