"""Base channel interface for chat platforms."""

import os
from abc import ABC, abstractmethod
from typing import Any

import httpx
from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


class BaseChannel(ABC):
    """
    Abstract base class for chat channel implementations.

    Each channel (Telegram, Discord, etc.) should implement this interface
    to integrate with the nanobot message bus.
    """

    name: str = "base"

    def __init__(self, config: Any, bus: MessageBus):
        """
        Initialize the channel.

        Args:
            config: Channel-specific configuration.
            bus: The message bus for communication.
        """
        self.config = config
        self.bus = bus
        self._running = False

    @abstractmethod
    async def start(self) -> None:
        """
        Start the channel and begin listening for messages.

        This should be a long-running async task that:
        1. Connects to the chat platform
        2. Listens for incoming messages
        3. Forwards messages to the bus via _handle_message()
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> dict[str, Any] | None:
        """
        Send a message through this channel.

        Args:
            msg: The message to send.
        """
        pass

    async def _is_sender_authorized(self, channel: str, external_id: str) -> bool:
        """Check with API gateway whether this sender is bound to this sandbox's owner."""
        owner_id = os.environ.get("OWNER_ID", "")
        gateway_url = os.environ.get("GATEWAY_URL", "")
        token = os.environ.get("GATEWAY_JWT_TOKEN", "")
        if not owner_id or not gateway_url:
            # Dev mode: no gateway configured, allow all senders
            return True
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{gateway_url}/internal/channel-auth",
                    params={"channel": channel, "external_id": external_id, "owner_id": owner_id},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=3.0,
                )
            return r.status_code == 200 and r.json().get("authorized", False)
        except Exception as exc:
            logger.error("Channel auth request failed: %s", exc)
            return True  # Fail-open: allow when gateway is unreachable

    def is_allowed(self, sender_id: str) -> bool:
        """Check if *sender_id* is permitted.  Empty list → deny all; ``"*"`` → allow all."""
        allow_list = getattr(self.config, "allow_from", [])
        if not allow_list:
            logger.warning("{}: allow_from is empty — all access denied", self.name)
            return False
        if "*" in allow_list:
            return True
        sender_str = str(sender_id)
        return sender_str in allow_list or any(
            p in allow_list for p in sender_str.split("|") if p
        )

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        """
        Handle an incoming message from the chat platform.

        This method checks permissions and forwards to the bus.

        Args:
            sender_id: The sender's identifier.
            chat_id: The chat/channel identifier.
            content: Message text content.
            media: Optional list of media URLs.
            metadata: Optional channel-specific metadata.
        """
        if not self.is_allowed(sender_id):
            logger.warning(
                f"Access denied for sender {sender_id} on channel {self.name}. "
                f"Add them to allowFrom list in config to grant access."
            )
            return

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            run_id=run_id,
            conversation_id=conversation_id,
            media=media or [],
            metadata=metadata or {}
        )

        await self.bus.publish_inbound(msg)

    @property
    def is_running(self) -> bool:
        """Check if the channel is running."""
        return self._running
