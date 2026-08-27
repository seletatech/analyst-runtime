"""Microsoft Teams tool via Microsoft Graph API."""

import json
import os
from typing import Any

import httpx

from analyst_runtime.agent.tools.base import Tool


class TeamsTool(Tool):
    """Interact with Microsoft Teams via the Graph API."""

    BASE_URL = "https://graph.microsoft.com/v1.0"

    def __init__(self, access_token: str | None = None):
        self._token = access_token or os.environ.get("TEAMS_ACCESS_TOKEN", "")

    @property
    def name(self) -> str:
        return "teams"

    @property
    def description(self) -> str:
        return "Interact with Microsoft Teams. Actions: list_chats, send_message, read_messages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_chats", "send_message", "read_messages"],
                    "description": "The action to perform",
                },
                "chat_id": {"type": "string", "description": "Chat ID (send_message, read_messages)"},
                "content": {"type": "string", "description": "Message content (send_message)"},
                "limit": {"type": "integer", "description": "Max messages (read_messages)", "minimum": 1, "maximum": 50},
            },
            "required": ["action"],
        }

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def execute(self, action: str, **kwargs: Any) -> str:
        if not self._token:
            return "not_configured"

        if action == "list_chats":
            return await self._list_chats()
        elif action == "send_message":
            return await self._send_message(
                chat_id=kwargs.get("chat_id", ""),
                content=kwargs.get("content", ""),
            )
        elif action == "read_messages":
            return await self._read_messages(
                chat_id=kwargs.get("chat_id", ""),
                limit=kwargs.get("limit", 20),
            )
        return f"Unknown action: {action}"

    async def _list_chats(self) -> str:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE_URL}/me/chats",
                    params={"$top": 50},
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            chats = [
                {
                    "id": ch.get("id"),
                    "topic": ch.get("topic", ""),
                    "chatType": ch.get("chatType", ""),
                    "lastUpdated": ch.get("lastUpdatedDateTime", ""),
                }
                for ch in data.get("value", [])
            ]
            return json.dumps({"chats": chats, "count": len(chats)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _send_message(self, chat_id: str, content: str) -> str:
        if not chat_id or not content:
            return "Error: chat_id and content are required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self.BASE_URL}/chats/{chat_id}/messages",
                    json={"body": {"content": content}},
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            return json.dumps({"success": True, "message_id": data.get("id", "")})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _read_messages(self, chat_id: str, limit: int) -> str:
        if not chat_id:
            return "Error: chat_id is required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE_URL}/chats/{chat_id}/messages",
                    params={"$top": min(max(limit, 1), 50)},
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            messages = [
                {
                    "id": m.get("id"),
                    "from": m.get("from", {}).get("user", {}).get("displayName", ""),
                    "content": m.get("body", {}).get("content", ""),
                    "createdDateTime": m.get("createdDateTime", ""),
                }
                for m in data.get("value", [])
            ]
            return json.dumps({"messages": messages, "count": len(messages)})
        except Exception as e:
            return json.dumps({"error": str(e)})
