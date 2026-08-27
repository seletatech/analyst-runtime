"""Regression test: current user message must not appear twice in the LLM context.

Root cause: _process_message called session.add_event(user_input) BEFORE
calling session.get_history(). Because get_history() includes the just-added
user_input as a {"role": "user"} message, and build_messages also appends
current_message, the same message appeared twice in the LLM context.

Symptoms: Samantha responded "I already stored your first message in the chat
history, now I've received your second similar test message" — seeing one
user message as "two consecutive identical messages".
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.bus.events import InboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.providers.base import LLMProvider, LLMResponse


class _CapturingProvider(LLMProvider):
    """Records every messages list received by chat(), returns a canned reply."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[dict[str, Any]]] = []

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
        self.calls.append(messages)
        return LLMResponse(content="ok", finish_reason="stop")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def agent_loop(workspace: Path) -> AgentLoop:
    provider = _CapturingProvider()
    bus = MessageBus()
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
        model="test-model",
    )


def _user_messages(msgs: list[dict]) -> list[str]:
    """Extract content of all user-role messages (excluding system prompt)."""
    return [
        m["content"] if isinstance(m["content"], str)
        else (m["content"][0].get("text", "") if isinstance(m["content"], list) else "")
        for m in msgs
        if m.get("role") == "user"
    ]


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_current_message_appears_exactly_once_in_llm_context(
    agent_loop: AgentLoop,
) -> None:
    """The user's current message must appear exactly once in the LLM context.

    Regression: user_input event was added to session BEFORE get_history() was
    called. This caused the current message to appear as a history entry AND as
    the current_message passed to build_messages — two identical consecutive
    user messages.
    """
    provider = agent_loop.provider  # type: ignore[assignment]
    assert isinstance(provider, _CapturingProvider)

    msg = InboundMessage(
        channel="telegram",
        sender_id="user123",
        chat_id="99999",
        content="收到了几条消息",
    )

    await agent_loop._process_message(msg)

    assert provider.calls, "Provider was never called"
    sent_messages = provider.calls[0]

    user_msgs = _user_messages(sent_messages)
    duplicate_count = user_msgs.count("收到了几条消息")
    assert duplicate_count == 1, (
        f"Expected the user message to appear exactly once in the LLM context, "
        f"but it appeared {duplicate_count} times.\n"
        f"All user messages seen by LLM: {user_msgs}"
    )


@pytest.mark.asyncio
async def test_current_message_appears_exactly_once_on_second_turn(
    agent_loop: AgentLoop,
) -> None:
    """After a prior turn exists in history, the NEW message must still appear exactly once.

    This verifies the fix also works when there IS history (not just first message).
    """
    provider = agent_loop.provider  # type: ignore[assignment]
    assert isinstance(provider, _CapturingProvider)

    # First turn (populates session history)
    await agent_loop._process_message(InboundMessage(
        channel="telegram",
        sender_id="user123",
        chat_id="99999",
        content="hello",
    ))

    provider.calls.clear()  # reset — we care only about the second turn

    second_message = "what did I just say?"
    await agent_loop._process_message(InboundMessage(
        channel="telegram",
        sender_id="user123",
        chat_id="99999",
        content=second_message,
    ))

    assert provider.calls, "Provider was never called for second turn"
    sent_messages = provider.calls[0]

    user_msgs = _user_messages(sent_messages)
    duplicate_count = user_msgs.count(second_message)
    assert duplicate_count == 1, (
        f"Expected the second message to appear exactly once, "
        f"but it appeared {duplicate_count} times.\n"
        f"All user messages seen by LLM: {user_msgs}"
    )


@pytest.mark.asyncio
async def test_distinct_runs_share_history_only_through_conversation_identity(
    agent_loop: AgentLoop,
) -> None:
    provider = agent_loop.provider  # type: ignore[assignment]
    assert isinstance(provider, _CapturingProvider)

    await agent_loop._process_message(InboundMessage(
        channel="web",
        sender_id="chat-run-1",
        chat_id="chat-run-1",
        content="first turn",
        conversation_id="conversation-456",
        run_id="run-1",
    ))

    provider.calls.clear()
    await agent_loop._process_message(InboundMessage(
        channel="web",
        sender_id="chat-run-2",
        chat_id="chat-run-2",
        content="second turn",
        conversation_id="conversation-456",
        run_id="run-2",
    ))

    user_messages = _user_messages(provider.calls[0])
    assert user_messages.count("first turn") == 1
    assert user_messages.count("second turn") == 1
