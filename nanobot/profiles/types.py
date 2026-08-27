"""Shared contracts for optional runtime profiles."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProfileTurnContext:
    """Request-scoped routing and identity supplied to profile tools."""

    channel: str
    chat_id: str
    session_key: str | None = None
    user_message: str | None = None
    inbound_turn_id: str | None = None
    analysis_conversation_id: str | None = None
    run_id: str | None = None
