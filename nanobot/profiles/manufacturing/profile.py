"""Optional manufacturing-semantics runtime profile."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.delivery import DeliveryAcknowledgement
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.profiles.manufacturing.intents import ConfirmationIntentJournal
from nanobot.profiles.manufacturing.memory import (
    CONFIRMED_SEMANTICS_SECTION,
    ManufacturingSemanticMemory,
)
from nanobot.profiles.manufacturing.tools import (
    ConfirmManufacturingSemanticsTool,
    ProposeManufacturingSemanticsTool,
)
from nanobot.profiles.types import ProfileTurnContext


class ManufacturingSemanticsProfile:
    """Own all manufacturing-specific tools and handoff behavior."""

    control_tool_names = frozenset(
        {"propose_manufacturing_semantics", "confirm_manufacturing_semantics"}
    )
    protected_memory_sections = (CONFIRMED_SEMANTICS_SECTION,)
    handoff_finish_reason = "semantic_handoff"

    def __init__(self, workspace: Path) -> None:
        self.semantic_memory = ManufacturingSemanticMemory(workspace)
        self.confirmation_journal = ConfirmationIntentJournal(workspace)
        self.confirmation_tool = ConfirmManufacturingSemanticsTool(
            self.semantic_memory,
            self.confirmation_journal,
        )
        self.proposal_tool = ProposeManufacturingSemanticsTool(self.semantic_memory)

    def register_tools(self, tools: ToolRegistry) -> None:
        tools.register(self.proposal_tool)
        tools.register(self.confirmation_tool)

    @staticmethod
    def ensure_safe_to_disable(workspace: Path) -> None:
        """Reject silent removal while this profile still owns persisted state."""
        memory_path = workspace / "memory" / "MEMORY.md"
        if not memory_path.exists():
            return
        memory = memory_path.read_text(encoding="utf-8")
        if CONFIRMED_SEMANTICS_SECTION.start_marker in memory:
            raise ValueError(
                "manufacturing-semantics cannot be disabled while persisted state exists; "
                "migrate or remove its protected memory section first"
            )

    def set_tool_context(self, context: ProfileTurnContext) -> None:
        for tool in (self.proposal_tool, self.confirmation_tool):
            tool.set_context(
                context.channel,
                context.chat_id,
                session_key=context.session_key,
                user_message=context.user_message,
                inbound_turn_id=context.inbound_turn_id,
                analysis_conversation_id=context.analysis_conversation_id,
                run_id=context.run_id,
            )

    def handoff_message(self, tool_name: str, result: Any) -> str | None:
        if tool_name != self.confirmation_tool.name or not isinstance(result, str):
            return None
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return None
        if not (
            isinstance(payload, dict)
            and payload.get("status") == "confirmed"
            and isinstance(payload.get("semantic_hash"), str)
            and isinstance(payload.get("semantic_snapshot"), dict)
        ):
            return None
        return "数据语义已确认，分析任务正在创建。完成后可在任务列表中查看结果和证据。"

    @staticmethod
    def skipped_after_handoff_result() -> str:
        return json.dumps(
            {
                "status": "semantic_handoff",
                "error": "tool call skipped after confirmed semantic handoff",
            }
        )

    @staticmethod
    def blocked_non_control_result() -> str:
        return json.dumps(
            {
                "status": "semantic_confirmation_required",
                "error": "non-semantic tools are blocked in a semantic-control turn",
            }
        )

    async def replay_one(self, bus: MessageBus) -> bool:
        pending = self.confirmation_journal.recover(
            self.semantic_memory.read_confirmed(),
            self.semantic_memory.restore_confirmed,
        )
        quarantined_count = self.confirmation_journal.quarantined_count()
        if quarantined_count:
            logger.error(
                "Confirmation intent journal has {} quarantined entr{}",
                quarantined_count,
                "y" if quarantined_count == 1 else "ies",
            )
        if not pending:
            return False
        intent = pending[0]
        delivery = DeliveryAcknowledgement()
        await bus.publish_outbound(
            OutboundMessage(
                channel=intent.channel,
                chat_id=intent.chat_id,
                content="",
                run_id=intent.run_id,
                conversation_id=intent.conversation_id,
                metadata={
                    "control": "confirmation_intent_replay",
                    "confirmation_intent": intent.to_wire_dict(),
                },
                delivery=delivery,
            )
        )
        await asyncio.wait_for(delivery.wait(), timeout=15.0)
        self.confirmation_journal.mark_delivered(intent.intent_id)
        return True

    def enrich_outbound_metadata(self, metadata: dict[str, Any]) -> None:
        confirmed_semantics = self.confirmation_tool.consume_confirmation()
        if confirmed_semantics is None:
            return
        metadata["confirmed_manufacturing_semantics"] = confirmed_semantics
        confirmation_intent = confirmed_semantics.get("confirmation_intent")
        if isinstance(confirmation_intent, dict):
            metadata["confirmation_intent"] = confirmation_intent
