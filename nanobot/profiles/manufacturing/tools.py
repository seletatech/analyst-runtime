"""Tools for proposing and explicitly confirming manufacturing semantics."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.profiles.manufacturing.intents import (
    ConfirmationIntent,
    ConfirmationIntentJournal,
)
from nanobot.profiles.manufacturing.memory import (
    CAUSAL_LEVELS,
    CONFIRMATION_PHRASE,
    LOSS_BASES,
    SEMANTIC_FIELDS,
    TIME_BASES,
    ManufacturingSemanticMemory,
    SemanticConfirmationError,
    SemanticMemoryError,
    SemanticProposal,
)

_FIELD_LABELS = {
    "scope": "分析范围",
    "time_basis": "时间口径",
    "observation_unit": "观测单位",
    "process_stages": "工序阶段",
    "deduplication": "跨工序去重规则",
    "loss_basis": "损耗口径",
    "causal_level": "因果表述等级",
    "unit": "计量单位",
}

_ENUM_LABELS = {
    "event_time": "按事件发生时间",
    "batch_time": "按批次时间",
    "as_of_time": "按截止时点",
    "observation_total": "全部观测值合计",
    "first_observation_total": "首次观测合计",
    "final_disposition_net_loss": "最终处置净损耗",
    "not_applicable": "不适用",
    "not_assessed": "未评估因果",
    "candidate_association": "候选关联",
    "confirmed_association": "已确认关联",
    "hypothesis": "原因假设",
    "confirmed_cause": "已确认原因",
}


class _SemanticToolContext:
    def __init__(self) -> None:
        self._conversation: ContextVar[str] = ContextVar(
            f"{type(self).__name__}_conversation",
            default="",
        )
        self._user_message: ContextVar[str] = ContextVar(
            f"{type(self).__name__}_user_message",
            default="",
        )
        self._inbound_turn: ContextVar[str] = ContextVar(
            f"{type(self).__name__}_inbound_turn",
            default="",
        )
        self._analysis_conversation: ContextVar[str] = ContextVar(
            f"{type(self).__name__}_analysis_conversation",
            default="",
        )
        self._run_id: ContextVar[str] = ContextVar(
            f"{type(self).__name__}_run_id",
            default="",
        )

    def set_context(
        self,
        channel: str,
        chat_id: str,
        session_key: str | None = None,
        user_message: str | None = None,
        inbound_turn_id: str | None = None,
        analysis_conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Bind tool execution to the active stable conversation."""
        stable_conversation = (session_key or f"{channel}:{chat_id}").strip()
        self._conversation.set(stable_conversation)
        self._user_message.set(user_message or "")
        self._inbound_turn.set(inbound_turn_id or "")
        self._analysis_conversation.set(analysis_conversation_id or "")
        self._run_id.set(run_id or "")

    def _conversation_id(self) -> str:
        conversation_id = self._conversation.get()
        if not conversation_id:
            raise SemanticMemoryError("semantic tool has no active conversation context")
        return conversation_id


class ProposeManufacturingSemanticsTool(_SemanticToolContext, Tool):
    """Create a transient semantic card; this tool never writes MEMORY.md."""

    def __init__(self, semantic_memory: ManufacturingSemanticMemory) -> None:
        super().__init__()
        self._semantic_memory = semantic_memory

    @property
    def name(self) -> str:
        return "propose_manufacturing_semantics"

    @property
    def description(self) -> str:
        return (
            "Create the manufacturing data-semantics card that must be shown to the user "
            "before quantitative, cross-process, loss, association, or cause analysis. "
            "This only creates a pending proposal and never writes long-term memory."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "scope": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": _FIELD_LABELS["scope"],
            },
            "time_basis": {
                "type": "string",
                "enum": sorted(TIME_BASES),
                "description": _FIELD_LABELS["time_basis"],
            },
            "observation_unit": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": _FIELD_LABELS["observation_unit"],
            },
            "process_stages": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": _FIELD_LABELS["process_stages"],
            },
            "deduplication": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": _FIELD_LABELS["deduplication"],
            },
            "loss_basis": {
                "type": "string",
                "enum": sorted(LOSS_BASES),
                "description": _FIELD_LABELS["loss_basis"],
            },
            "causal_level": {
                "type": "string",
                "enum": sorted(CAUSAL_LEVELS),
                "description": _FIELD_LABELS["causal_level"],
            },
            "unit": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": _FIELD_LABELS["unit"],
            },
        }
        return {
            "type": "object",
            "properties": {
                "semantic_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                    "description": "Stable key for this analysis meaning, such as defect-loss:HUD-70538",
                },
                "semantics": {
                    "type": "object",
                    "properties": properties,
                    "required": list(SEMANTIC_FIELDS),
                },
            },
            "required": ["semantic_key", "semantics"],
        }

    async def execute(
        self,
        semantic_key: str,
        semantics: dict[str, object],
        **kwargs: Any,
    ) -> str:
        try:
            proposal = self._semantic_memory.propose(
                conversation_id=self._conversation_id(),
                inbound_turn_id=self._inbound_turn.get(),
                semantic_key=semantic_key,
                semantics=semantics,
            )
        except SemanticMemoryError as error:
            return json.dumps(
                {"status": "invalid_semantics", "error": str(error)},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "proposal_id": proposal.proposal_id,
                "required_confirmation": CONFIRMATION_PHRASE,
                "semantic_card": _render_card(proposal),
                "semantic_snapshot": proposal.to_pending_snapshot(),
                "status": "awaiting_confirmation",
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class ConfirmManufacturingSemanticsTool(_SemanticToolContext, Tool):
    """Persist a pending semantic card after exact user confirmation."""

    def __init__(
        self,
        semantic_memory: ManufacturingSemanticMemory,
        confirmation_journal: ConfirmationIntentJournal | None = None,
    ) -> None:
        super().__init__()
        self._semantic_memory = semantic_memory
        self._confirmation_journal = confirmation_journal
        self._confirmed: ContextVar[dict[str, object] | None] = ContextVar(
            "confirmed_manufacturing_semantics",
            default=None,
        )

    def set_context(
        self,
        channel: str,
        chat_id: str,
        session_key: str | None = None,
        user_message: str | None = None,
        inbound_turn_id: str | None = None,
        analysis_conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        super().set_context(
            channel,
            chat_id,
            session_key=session_key,
            user_message=user_message,
            inbound_turn_id=inbound_turn_id,
            analysis_conversation_id=analysis_conversation_id,
            run_id=run_id,
        )
        self._confirmed.set(None)

    def consume_confirmation(self) -> dict[str, object] | None:
        """Return and clear the trusted confirmation produced in this inbound turn."""
        confirmed = self._confirmed.get()
        self._confirmed.set(None)
        return confirmed

    @property
    def name(self) -> str:
        return "confirm_manufacturing_semantics"

    @property
    def description(self) -> str:
        return (
            "Persist a previously shown manufacturing semantics card to MEMORY.md. "
            f"Call only after the user replies exactly: {CONFIRMATION_PHRASE}. "
            "The proposal must belong to the current stable conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "confirmation": {
                    "type": "string",
                    "enum": [CONFIRMATION_PHRASE],
                    "description": (
                        "Compatibility hint only; authorization is checked against "
                        "the trusted current user message, not this model argument."
                    ),
                },
            },
            "required": ["proposal_id", "confirmation"],
        }

    async def execute(
        self,
        proposal_id: str,
        confirmation: str,
        **kwargs: Any,
    ) -> str:
        # The model-owned compatibility argument is deliberately not an
        # authorization source. Only AgentLoop's current inbound user message is.
        _ = confirmation
        prepared_intent: ConfirmationIntent | None = None

        def prepare_intent(entry) -> None:
            nonlocal prepared_intent
            if self._confirmation_journal is None:
                return
            analysis_conversation = self._analysis_conversation.get()
            run_id = self._run_id.get()
            if not analysis_conversation or not run_id:
                return
            prepared_intent = self._confirmation_journal.prepare(
                channel=self._conversation_id().split(":", 1)[0],
                chat_id=f"chat-{run_id}",
                conversation_id=analysis_conversation,
                created_at=entry.confirmed_at,
                run_id=run_id,
                semantic_hash=entry.semantic_hash,
                semantic_key=entry.semantic_key,
                semantic_snapshot=entry.semantics,
            )

        try:
            confirmed = self._semantic_memory.confirm(
                conversation_id=self._conversation_id(),
                inbound_turn_id=self._inbound_turn.get(),
                proposal_id=proposal_id,
                confirmation=self._user_message.get(),
                before_persist=prepare_intent,
            )
        except SemanticConfirmationError as error:
            return json.dumps(
                {"status": "confirmation_required", "error": str(error)},
                ensure_ascii=False,
            )
        except SemanticMemoryError as error:
            confirmed = None
            memory_outcome_known = True
            if prepared_intent is not None:
                try:
                    confirmed = next(
                        (
                            entry
                            for entry in self._semantic_memory.read_confirmed()
                            if entry.semantic_key == prepared_intent.semantic_key
                            and entry.semantic_hash == prepared_intent.semantic_hash
                            and entry.confirmed_at == prepared_intent.created_at
                        ),
                        None,
                    )
                except SemanticMemoryError:
                    # The durable write outcome is ambiguous. Keep the WAL so restart
                    # recovery can reconcile it instead of losing an authorized task.
                    memory_outcome_known = False
                if (
                    confirmed is None
                    and memory_outcome_known
                    and self._confirmation_journal is not None
                ):
                    self._confirmation_journal.discard_prepared(prepared_intent.intent_id)
            if confirmed is None:
                return json.dumps(
                    {"status": "memory_error", "error": str(error)},
                    ensure_ascii=False,
                )
        if prepared_intent is not None:
            self._confirmation_journal.commit(prepared_intent.intent_id)
        confirmation = {
            "confirmed_at": confirmed.confirmed_at,
            "semantic_hash": confirmed.semantic_hash,
            "semantic_key": confirmed.semantic_key,
            "semantic_snapshot": confirmed.semantics,
            "status": "confirmed",
        }
        if prepared_intent is not None:
            confirmation["confirmation_intent"] = prepared_intent.to_wire_dict()
        self._confirmed.set(confirmation)
        return json.dumps(
            confirmation,
            ensure_ascii=False,
            sort_keys=True,
        )


def _render_card(proposal: SemanticProposal) -> str:
    lines = ["数据语义确认卡"]
    for field in SEMANTIC_FIELDS:
        value = proposal.semantics[field]
        if isinstance(value, list):
            display = "、".join(str(item) for item in value)
        else:
            normalized = str(value)
            display = (
                f"{_ENUM_LABELS[normalized]}（{normalized}）"
                if normalized in _ENUM_LABELS
                else normalized
            )
        lines.append(f"- {_FIELD_LABELS[field]}：{display}")
    lines.append(f"请回复“{CONFIRMATION_PHRASE}”，或逐项修改。确认后我会保存到长期记忆并开始分析。")
    return "\n".join(lines)
