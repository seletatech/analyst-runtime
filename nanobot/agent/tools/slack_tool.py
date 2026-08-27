"""Slack tool via Slack Web API."""

import json
import os
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool


class SlackTool(Tool):
    """Interact with Slack workspaces via the Web API."""

    BASE_URL = "https://slack.com/api"

    def __init__(self, bot_token: str | None = None):
        self._token = bot_token or os.environ.get("SLACK_BOT_TOKEN", "")

    @property
    def name(self) -> str:
        return "slack"

    @property
    def description(self) -> str:
        return "Interact with Slack. Actions: list_channels, read_messages, send_message."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_channels", "read_messages", "send_message"],
                    "description": "The action to perform",
                },
                "channel_id": {"type": "string", "description": "Channel ID (read_messages, send_message)"},
                "text": {"type": "string", "description": "Message text (send_message)"},
                "limit": {"type": "integer", "description": "Max messages (read_messages)", "minimum": 1, "maximum": 100},
            },
            "required": ["action"],
        }

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def execute(self, action: str, **kwargs: Any) -> str:
        if not self._token:
            return "not_configured"

        if action == "list_channels":
            return await self._list_channels()
        elif action == "read_messages":
            return await self._read_messages(
                channel_id=kwargs.get("channel_id", ""),
                limit=kwargs.get("limit", 20),
            )
        elif action == "send_message":
            return await self._send_message(
                channel_id=kwargs.get("channel_id", ""),
                text=kwargs.get("text", ""),
            )
        return f"Unknown action: {action}"

    async def _list_channels(self) -> str:
        try:
            channels: list[dict[str, Any]] = []
            channel_ids: set[str] = set()

            # Query public/private separately so one missing scope does not
            # block the other channel type.
            for channel_type in ("public_channel", "private_channel"):
                async with httpx.AsyncClient() as client:
                    r = await client.get(
                        f"{self.BASE_URL}/conversations.list",
                        params={"types": channel_type, "limit": 100},
                        headers=self._headers(),
                        timeout=15.0,
                    )
                    r.raise_for_status()

                data = r.json()
                if not data.get("ok"):
                    # Ignore private-channel scope misses when public channels
                    # are still accessible; this keeps read-only listing useful.
                    if channel_type == "private_channel" and data.get("error") == "missing_scope":
                        continue
                    return json.dumps({"error": data.get("error", "Unknown error")})

                for ch in data.get("channels", []):
                    channel_id = ch.get("id", "")
                    if channel_id and channel_id in channel_ids:
                        continue
                    if channel_id:
                        channel_ids.add(channel_id)
                    channels.append(
                        {
                            "id": channel_id,
                            "name": ch.get("name", ""),
                            "topic": ch.get("topic", {}).get("value", ""),
                            "member_count": ch.get("num_members", 0),
                        }
                    )

            return json.dumps({"channels": channels, "count": len(channels)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _read_messages(self, channel_id: str, limit: int) -> str:
        if not channel_id:
            return "Error: channel_id is required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE_URL}/conversations.history",
                    params={"channel": channel_id, "limit": min(max(limit, 1), 100)},
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                return json.dumps({"error": data.get("error", "Unknown error")})

            messages = [
                {
                    "user": m.get("user", ""),
                    "text": m.get("text", ""),
                    "ts": m.get("ts", ""),
                }
                for m in data.get("messages", [])
            ]
            return json.dumps({"messages": messages, "count": len(messages)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _send_message(self, channel_id: str, text: str) -> str:
        if not channel_id or not text:
            return "Error: channel_id and text are required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self.BASE_URL}/chat.postMessage",
                    json={"channel": channel_id, "text": text},
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                return json.dumps({"error": data.get("error", "Unknown error")})
            return json.dumps({"success": True, "channel": channel_id, "ts": data.get("ts", "")})
        except Exception as e:
            return json.dumps({"error": str(e)})
