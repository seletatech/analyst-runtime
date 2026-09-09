"""Run-scoped steering coordination for the Analyst Runtime agent loop."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from analyst_runtime.agent.context import ContextBuilder
from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.session.manager import Session, SessionManager

SteerStatus = Literal["pending", "applied", "rejected"]


@dataclass(frozen=True)
class SteerKey:
    """Session- and run-scoped identity for one steering command."""

    session_key: str
    execution_key: str
    steer_id: str

    @classmethod
    def from_message(cls, steer: InboundMessage) -> SteerKey | None:
        steer_id = str(steer.metadata.get("steer_id") or "")
        if not steer_id:
            return None
        return cls(
            session_key=steer.session_key,
            execution_key=steer.execution_key,
            steer_id=steer_id,
        )

    def storage_id(self) -> str:
        return json.dumps(
            [self.session_key, self.execution_key, self.steer_id],
            ensure_ascii=False,
            separators=(",", ":"),
        )


class SteeringCoordinator:
    """Queue, apply, persist, and acknowledge run-scoped steering commands."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        sessions: SessionManager,
        context: ContextBuilder,
    ) -> None:
        self.bus = bus
        self.sessions = sessions
        self.context = context
        self.queues: dict[str, asyncio.Queue[InboundMessage]] = {}
        self.pending_keys: set[SteerKey] = set()

    def was_applied(self, steer: InboundMessage) -> bool:
        key = SteerKey.from_message(steer)
        if key is None:
            return False
        session = self.sessions.get_or_create(steer.session_key)
        return key.storage_id() in set(session.metadata.get("applied_steer_keys") or [])

    async def publish_status(self, steer: InboundMessage, status: SteerStatus) -> None:
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=steer.channel,
                chat_id=steer.chat_id,
                content="",
                run_id=steer.run_id,
                conversation_id=steer.conversation_id,
                metadata={
                    "control": f"steer_{status}",
                    "steer_id": steer.metadata.get("steer_id"),
                },
            )
        )

    async def handle_control(self, steer: InboundMessage, *, run_active: bool) -> None:
        """Accept a steering command or report its durable status."""
        key = SteerKey.from_message(steer)
        if self.was_applied(steer):
            await self.publish_status(steer, "applied")
            return

        if steer.metadata.get("control") == "steer_status":
            await self.publish_status(
                steer,
                "pending" if key is not None and key in self.pending_keys else "rejected",
            )
            return

        if key is None:
            await self.publish_status(steer, "rejected")
            return

        if key in self.pending_keys:
            await self.publish_status(steer, "pending")
            return

        if not run_active or not steer.content.strip():
            await self.publish_status(steer, "rejected")
            return

        self.pending_keys.add(key)
        self.queues.setdefault(steer.execution_key, asyncio.Queue()).put_nowait(steer)

    async def apply_pending(
        self,
        execution_key: str | None,
        messages: list[dict[str, Any]],
        *,
        session: Session | None,
        parent_uuid: str | None,
        preceding_assistant: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """Apply accepted steering only at a safe agent-step boundary."""
        if not execution_key:
            return None
        queue = self.queues.get(execution_key)
        if not queue or queue.empty():
            return None

        steers: list[InboundMessage] = []
        while not queue.empty():
            steers.append(queue.get_nowait())
            queue.task_done()
        if not steers:
            return None

        if session is None:
            for steer in steers:
                self._forget_pending(steer)
                await self.publish_status(steer, "rejected")
            return messages

        matching = [steer for steer in steers if steer.session_key == session.key]
        rejected = [steer for steer in steers if steer.session_key != session.key]
        for steer in rejected:
            self._forget_pending(steer)
            await self.publish_status(steer, "rejected")
        if not matching:
            return messages

        unapplied = [steer for steer in matching if not self.was_applied(steer)]
        if not unapplied:
            for steer in matching:
                self._forget_pending(steer)
                await self.publish_status(steer, "applied")
            return messages

        updated = messages
        if preceding_assistant:
            updated = self.context.add_assistant_message(updated, preceding_assistant, None)
        combined = "\n\n".join(
            steer.content.strip() for steer in unapplied if steer.content.strip()
        )
        updated.append(
            {
                "role": "user",
                "content": (
                    "[The user added this instruction while you were working. "
                    "Apply it now to the current task.]\n\n" + combined
                ),
            }
        )
        applied_ids = list(session.metadata.get("applied_steer_ids") or [])
        applied_keys = list(session.metadata.get("applied_steer_keys") or [])
        for steer in unapplied:
            key = SteerKey.from_message(steer)
            assert key is not None
            session.add_event(
                {
                    "uuid": str(uuid.uuid4()),
                    "parent_uuid": parent_uuid,
                    "type": "user_input",
                    "steer": True,
                    "steer_id": key.steer_id,
                    "content": steer.content,
                    "channel": steer.channel,
                    "chat_id": steer.chat_id,
                }
            )
            if key.steer_id not in applied_ids:
                applied_ids.append(key.steer_id)
            if key.storage_id() not in applied_keys:
                applied_keys.append(key.storage_id())
        session.metadata["applied_steer_ids"] = applied_ids[-512:]
        session.metadata["applied_steer_keys"] = applied_keys[-512:]
        # Persist the run-scoped idempotency record before acknowledging the command.
        self.sessions.save(session)
        for steer in matching:
            self._forget_pending(steer)
            await self.publish_status(steer, "applied")
        return updated

    async def reject_pending(self, execution_key: str) -> None:
        queue = self.queues.pop(execution_key, None)
        if not queue:
            return
        while not queue.empty():
            steer = queue.get_nowait()
            queue.task_done()
            self._forget_pending(steer)
            await self.publish_status(steer, "rejected")

    def _forget_pending(self, steer: InboundMessage) -> None:
        key = SteerKey.from_message(steer)
        if key is not None:
            self.pending_keys.discard(key)
