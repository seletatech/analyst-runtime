"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from analyst_runtime.bus.delivery import DeliveryAcknowledgement


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    run_id: str | None = None  # One execution/correlation identity
    conversation_id: str | None = None  # Stable history identity

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return f"{self.channel}:{self.conversation_id or self.chat_id}"

    @property
    def execution_key(self) -> str:
        """Unique key for routing, cancellation, and active execution state."""
        return f"{self.channel}:{self.run_id or self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    conversation_id: str | None = None
    delivery: DeliveryAcknowledgement | None = field(
        default=None,
        repr=False,
        compare=False,
    )
