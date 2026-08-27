from __future__ import annotations

import json
from pathlib import Path

import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.bus.events import InboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.profiles.manufacturing.intents import ConfirmationIntentJournal
from analyst_runtime.profiles.manufacturing.memory import CONFIRMATION_PHRASE
from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest


@pytest.fixture(autouse=True)
def _enable_manufacturing_semantics_profile(tmp_path: Path) -> None:
    (tmp_path / "workspace.json").write_text(
        '{"schema_version":1,"runtime_profiles":["manufacturing-semantics"]}',
        encoding="utf-8",
    )


class _UnusedProvider(LLMProvider):
    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, *args, **kwargs) -> LLMResponse:
        raise AssertionError("provider should not be called by the tool integration test")


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self._responses = responses
        self.seen_messages: list[list[dict[str, object]]] = []

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.seen_messages.append(kwargs["messages"])
        return self._responses.pop(0)


class _ProposeThenConfirmProvider(LLMProvider):
    """Ask both semantic tools to run during one inbound user turn."""

    def __init__(self, write_path: Path) -> None:
        super().__init__()
        self._calls = 0
        self.confirmation_result: dict[str, object] | None = None
        self.write_path = write_path

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(self, messages, *args, **kwargs) -> LLMResponse:
        self._calls += 1
        if self._calls == 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="write-before-confirm",
                        name="write_file",
                        arguments={
                            "path": str(self.write_path),
                            "content": "manufacturing result: 999",
                        },
                    ),
                    ToolCallRequest(
                        id="propose-1",
                        name="propose_manufacturing_semantics",
                        arguments={
                            "semantic_key": "defect-loss:HUD-70538",
                            "semantics": _wrinkle_loss_semantics(),
                        },
                    ),
                ],
            )
        if self._calls == 2:
            proposal_result = next(
                message
                for message in reversed(messages)
                if message.get("role") == "tool"
                and message.get("name") == "propose_manufacturing_semantics"
            )
            proposal_id = json.loads(proposal_result["content"])["proposal_id"]
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="confirm-1",
                        name="confirm_manufacturing_semantics",
                        arguments={
                            "proposal_id": proposal_id,
                            "confirmation": CONFIRMATION_PHRASE,
                        },
                    ),
                ],
            )
        confirmation_result = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "tool"
                and message.get("name") == "confirm_manufacturing_semantics"
            ),
            None,
        )
        if confirmation_result is not None:
            self.confirmation_result = json.loads(confirmation_result["content"])
        return LLMResponse(
            content="本轮不能同时提出并确认数据语义。",
            finish_reason="stop",
        )


def _wrinkle_loss_semantics() -> dict[str, object]:
    return {
        "scope": "HUD-70538 wrinkle loss during 2026-01 production",
        "time_basis": "event_time",
        "observation_unit": "physical defect event on a roll",
        "process_stages": ["coating", "slitting", "rewind"],
        "deduplication": "count one physical defect event once across process observations",
        "loss_basis": "final_disposition_net_loss",
        "causal_level": "candidate_association",
        "unit": "m",
    }


@pytest.mark.asyncio
async def test_runtime_rejects_propose_then_confirm_in_same_inbound_turn(
    tmp_path: Path,
) -> None:
    write_path = tmp_path / "artifacts" / "should-not-exist.txt"
    provider = _ProposeThenConfirmProvider(write_path)
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
    )

    response = await agent._process_message(
        InboundMessage(
            channel="web",
            sender_id="single-user",
            chat_id="chat-run-1",
            content=CONFIRMATION_PHRASE,
            conversation_id="conversation-70538",
            run_id="run-1",
        )
    )

    assert response is not None
    assert provider.confirmation_result is not None
    assert provider.confirmation_result["status"] == "confirmation_required"
    assert "later inbound turn" in str(provider.confirmation_result["error"])
    assert not (tmp_path / "memory" / "MEMORY.md").exists()
    assert not write_path.exists()


@pytest.mark.asyncio
async def test_runtime_semantic_tools_share_pending_state_across_runs(
    tmp_path: Path,
) -> None:
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_UnusedProvider(),
        workspace=tmp_path,
    )
    conversation_id = "conversation-70538"
    conversation_key = f"web:{conversation_id}"

    agent._set_tool_context(
        "web",
        "chat-run-1",
        session_key=conversation_key,
        inbound_turn_id="turn-1",
    )
    pending = json.loads(
        await agent.tools.execute(
            "propose_manufacturing_semantics",
            {
                "semantic_key": "defect-loss:HUD-70538",
                "semantics": _wrinkle_loss_semantics(),
            },
        )
    )

    assert pending["status"] == "awaiting_confirmation"
    assert not (tmp_path / "memory" / "MEMORY.md").exists()

    agent._set_tool_context(
        "web",
        "chat-run-2",
        session_key=conversation_key,
        user_message=CONFIRMATION_PHRASE,
        inbound_turn_id="turn-2",
    )
    confirmed = json.loads(
        await agent.tools.execute(
            "confirm_manufacturing_semantics",
            {
                "proposal_id": pending["proposal_id"],
                "confirmation": CONFIRMATION_PHRASE,
            },
        )
    )

    assert confirmed["status"] == "confirmed"
    assert confirmed["semantic_snapshot"]["status"] == "confirmed"
    assert confirmed["semantic_hash"] == (
        "8c047c79210070345d672c7d601ddcafc8e0273ce206b26f2500445cd1537808"
    )
    assert (tmp_path / "memory" / "MEMORY.md").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("user_reply", ["继续", "可以"])
async def test_runtime_rejects_model_supplied_confirmation_when_user_reply_is_ambiguous(
    tmp_path: Path,
    user_reply: str,
) -> None:
    provider = _SequenceProvider([])
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    conversation_id = "conversation-70538"
    conversation_key = f"web:{conversation_id}"

    agent._set_tool_context(
        "web",
        "chat-run-1",
        session_key=conversation_key,
        inbound_turn_id="turn-1",
    )
    pending = json.loads(
        await agent.tools.execute(
            "propose_manufacturing_semantics",
            {
                "semantic_key": "defect-loss:HUD-70538",
                "semantics": _wrinkle_loss_semantics(),
            },
        )
    )
    provider._responses.extend(
        [
            LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="confirm-1",
                        name="confirm_manufacturing_semantics",
                        arguments={
                            "proposal_id": pending["proposal_id"],
                            "confirmation": CONFIRMATION_PHRASE,
                        },
                    ),
                ],
            ),
            LLMResponse(
                content="数据不足：未收到精确确认语句，因此没有保存语义。",
                finish_reason="stop",
            ),
        ]
    )

    response = await agent._process_message(
        InboundMessage(
            channel="web",
            sender_id="single-user",
            chat_id="chat-run-2",
            content=user_reply,
            conversation_id=conversation_id,
            run_id="run-2",
        )
    )

    assert response is not None
    assert not (tmp_path / "memory" / "MEMORY.md").exists()
    assert "confirmed_manufacturing_semantics" not in response.metadata


@pytest.mark.asyncio
async def test_runtime_persists_semantics_when_current_user_message_is_exact_confirmation(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider([])
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    conversation_id = "00000000-0000-4000-8000-000000000401"
    conversation_key = f"web:{conversation_id}"

    agent._set_tool_context(
        "web",
        "chat-run-1",
        session_key=conversation_key,
        inbound_turn_id="turn-1",
    )
    pending = json.loads(
        await agent.tools.execute(
            "propose_manufacturing_semantics",
            {
                "semantic_key": "defect-loss:HUD-70538",
                "semantics": _wrinkle_loss_semantics(),
            },
        )
    )
    provider._responses.extend(
        [
            LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="confirm-1",
                        name="confirm_manufacturing_semantics",
                        arguments={
                            "proposal_id": pending["proposal_id"],
                            "confirmation": CONFIRMATION_PHRASE,
                        },
                    ),
                    ToolCallRequest(
                        id="write-after-confirm",
                        name="write_file",
                        arguments={
                            "path": str(tmp_path / "artifacts" / "forbidden.txt"),
                            "content": "must not execute",
                        },
                    ),
                ],
            ),
            LLMResponse(content="分析已完成：语义已确认。", finish_reason="stop"),
        ]
    )

    response = await agent._process_message(
        InboundMessage(
            channel="web",
            sender_id="single-user",
            chat_id="chat-run-2",
            content=CONFIRMATION_PHRASE,
            conversation_id=conversation_id,
            run_id="00000000-0000-4000-8000-000000000402",
        )
    )

    assert response is not None
    assert (tmp_path / "memory" / "MEMORY.md").is_file()
    confirmed = response.metadata["confirmed_manufacturing_semantics"]
    assert confirmed["semantic_key"] == "defect-loss:HUD-70538"
    assert confirmed["semantic_hash"] == (
        "8c047c79210070345d672c7d601ddcafc8e0273ce206b26f2500445cd1537808"
    )
    assert confirmed["semantic_snapshot"]["status"] == "confirmed"
    intent = response.metadata["confirmation_intent"]
    assert intent["run_id"] == "00000000-0000-4000-8000-000000000402"
    assert intent["conversation_id"] == conversation_id
    assert intent["semantic_hash"] == confirmed["semantic_hash"]
    assert ConfirmationIntentJournal(tmp_path).pending()[0].to_wire_dict() == intent
    assert (
        response.content == "数据语义已确认，分析任务正在创建。完成后可在任务列表中查看结果和证据。"
    )
    assert len(provider._responses) == 1
    assert not (tmp_path / "artifacts" / "forbidden.txt").exists()

    follow_up = await agent._process_message(
        InboundMessage(
            channel="web",
            sender_id="single-user",
            chat_id="chat-run-3",
            content="查看任务状态",
            conversation_id=conversation_id,
            run_id="00000000-0000-4000-8000-000000000403",
        )
    )
    assert follow_up is not None
    persisted_tool_results = {
        message.get("tool_call_id")
        for message in provider.seen_messages[1]
        if message.get("role") == "tool"
    }
    assert {"confirm-1", "write-after-confirm"} <= persisted_tool_results
