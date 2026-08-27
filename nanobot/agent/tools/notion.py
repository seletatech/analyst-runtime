"""Notion tool via REST API v1."""

import json
import os
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.gateway_auth import (
    format_auth_required_result,
    get_oauth_token,
)


class NotionTool(Tool):
    """Interact with Notion workspaces via the Notion API."""

    BASE_URL = "https://api.notion.com/v1"
    NOTION_VERSION = "2026-03-11"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("NOTION_API_KEY", "")

    @property
    def name(self) -> str:
        return "notion"

    @property
    def description(self) -> str:
        return "Interact with Notion. Actions: search, read_page, create_page, append_block."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "read_page", "create_page", "append_block"],
                    "description": "The action to perform",
                },
                "query": {"type": "string", "description": "Search query (search)"},
                "page_id": {"type": "string", "description": "Page ID (read_page)"},
                "parent_id": {"type": "string", "description": "Parent page/database ID (create_page). Omit or leave empty to create at workspace root level."},
                "title": {"type": "string", "description": "Page title (create_page)"},
                "content": {"type": "string", "description": "Text content (create_page, append_block)"},
                "block_id": {"type": "string", "description": "Block ID to append to (append_block)"},
            },
            "required": ["action"],
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Notion-Version": self.NOTION_VERSION,
        }

    async def check_auth(self) -> dict | None:
        """Return compact auth_required dict if Notion is not yet authorized."""
        if self._api_key:
            return None
        try:
            token_payload = await get_oauth_token("notion")
        except Exception:
            return {"status": "auth_required", "service": "notion"}
        if token_payload.get("status") == "auth_required":
            return {"status": "auth_required", "service": "notion"}
        return None

    async def execute(self, action: str, **kwargs: Any) -> str:
        access_token, auth_response = await self._resolve_access_token()
        if auth_response:
            return auth_response
        if not access_token:
            return json.dumps({"error": "Notion OAuth token unavailable"})

        self._api_key = access_token

        if action == "search":
            return await self._search(query=kwargs.get("query", ""))
        elif action == "read_page":
            return await self._read_page(page_id=kwargs.get("page_id", ""))
        elif action == "create_page":
            return await self._create_page(
                parent_id=kwargs.get("parent_id", ""),
                title=kwargs.get("title", ""),
                content=kwargs.get("content", ""),
            )
        elif action == "append_block":
            return await self._append_block(
                block_id=kwargs.get("block_id", ""),
                content=kwargs.get("content", ""),
            )
        return f"Unknown action: {action}"

    async def _resolve_access_token(self) -> tuple[str | None, str | None]:
        try:
            token_payload = await get_oauth_token("notion")
        except Exception as exc:
            if self._api_key:
                return self._api_key, None
            return None, json.dumps({"error": f"Failed to resolve Notion OAuth token: {exc}"})

        if token_payload.get("status") == "auth_required":
            return None, format_auth_required_result("notion")

        token = str(token_payload.get("access_token") or self._api_key or "").strip()
        if not token:
            return None, json.dumps({"error": "Notion OAuth token response is missing access_token"})
        return token, None

    @staticmethod
    def _http_error(exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            detail = exc.response.text
            return f"Notion API HTTP {exc.response.status_code}: {detail}"
        return str(exc)

    async def _search(self, query: str) -> str:
        body: dict[str, Any] = {}
        if query:
            body["query"] = query

        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self.BASE_URL}/search",
                    json=body,
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            results = r.json().get("results", [])
            items = []
            for item in results[:20]:
                title = ""
                props = item.get("properties", {})
                if "title" in props:
                    title_parts = props["title"].get("title", [])
                    title = "".join(t.get("plain_text", "") for t in title_parts)
                elif "Name" in props:
                    name_parts = props["Name"].get("title", [])
                    title = "".join(t.get("plain_text", "") for t in name_parts)
                items.append({
                    "id": item.get("id"),
                    "type": item.get("object"),
                    "title": title,
                    "url": item.get("url", ""),
                })
            return json.dumps({"results": items, "count": len(items)})
        except Exception as e:
            return json.dumps({"error": self._http_error(e)})

    async def _read_page(self, page_id: str) -> str:
        if not page_id:
            return "Error: page_id is required"

        try:
            async with httpx.AsyncClient() as client:
                # Fetch page properties
                r = await client.get(
                    f"{self.BASE_URL}/pages/{page_id}",
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
                page = r.json()

                # Fetch page content (blocks)
                r2 = await client.get(
                    f"{self.BASE_URL}/blocks/{page_id}/children",
                    headers=self._headers(),
                    timeout=15.0,
                )
                r2.raise_for_status()
                blocks = r2.json().get("results", [])

            content_parts = []
            for block in blocks:
                block_type = block.get("type", "")
                block_data = block.get(block_type, {})
                rich_text = block_data.get("rich_text", [])
                text = "".join(t.get("plain_text", "") for t in rich_text)
                if text:
                    content_parts.append(text)

            return json.dumps({
                "id": page.get("id"),
                "url": page.get("url", ""),
                "content": "\n".join(content_parts),
            })
        except Exception as e:
            return json.dumps({"error": self._http_error(e)})

    async def _create_page(self, parent_id: str, title: str, content: str) -> str:
        if not title:
            return "Error: title is required"

        children = []
        if content:
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content}}],
                },
            })

        parent = {"type": "workspace", "workspace": True} if not parent_id else {"page_id": parent_id}
        body = {
            "parent": parent,
            "properties": {
                "title": {"title": [{"text": {"content": title}}]},
            },
            "children": children,
        }

        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{self.BASE_URL}/pages",
                    json=body,
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            return json.dumps({"success": True, "id": data.get("id"), "url": data.get("url", "")})
        except Exception as e:
            return json.dumps({"error": self._http_error(e)})

    async def _append_block(self, block_id: str, content: str) -> str:
        if not block_id or not content:
            return "Error: block_id and content are required"

        body = {
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content}}],
                    },
                }
            ],
        }

        try:
            async with httpx.AsyncClient() as client:
                r = await client.patch(
                    f"{self.BASE_URL}/blocks/{block_id}/children",
                    json=body,
                    headers=self._headers(),
                    timeout=15.0,
                )
                r.raise_for_status()
            return json.dumps({"success": True, "block_id": block_id})
        except Exception as e:
            return json.dumps({"error": self._http_error(e)})
