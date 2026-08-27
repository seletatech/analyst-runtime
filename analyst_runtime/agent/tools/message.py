"""Message tool for sending messages to users."""

import json
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from analyst_runtime.bus.delivery import DeliveryAcknowledgement

from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.bus.events import OutboundMessage


class MessageTool(Tool):
    """Tool to send messages to users on chat channels."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = ""
    ):
        self._send_callback = send_callback
        self._context: ContextVar[tuple[str, str]] = ContextVar(
            "message_tool_context",
            default=(default_channel, default_chat_id),
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current message context."""
        self._context.set((channel, chat_id))

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """Set the callback for sending messages."""
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "Send a message to the user. Use this when you want to communicate something."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send"
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)"
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID"
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach (images, audio, documents)"
                }
            },
            "required": ["content"]
        }

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        media: list[str] | None = None,
        **kwargs: Any
    ) -> str:
        default_channel, default_chat_id = self._context.get()
        channel = channel or default_channel
        chat_id = chat_id or default_chat_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        delivery = DeliveryAcknowledgement() if media else None
        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            metadata={"intermediate": True, "phase": "delivery"},
            delivery=delivery,
        )

        try:
            await self._send_callback(msg)
            if delivery is None:
                return json.dumps({"delivered": True}, ensure_ascii=False)
            receipt = await delivery.wait()
            return json.dumps(
                {"delivered": True, **receipt},
                ensure_ascii=False,
            )
        except Exception as e:
            return f"Error sending message. {str(e)}"
