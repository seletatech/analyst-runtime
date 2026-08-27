"""Unit tests for NotionTool."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nanobot.agent.tools.notion import NotionTool


_AUTHORIZED_TOKEN_PAYLOAD = {
    "status": "authorized",
    "service": "notion",
    "access_token": "secret-notion-token",
    "expires_at": "2099-01-01T00:00:00+00:00",
    "workspace_name": "My Workspace",
}

_AUTH_REQUIRED_PAYLOAD = {
    "status": "auth_required",
    "service": "notion",
    "auth_url": "https://api.notion.com/v1/oauth/authorize?mock=1",
    "expires_at": "2099-01-01T00:00:00+00:00",
}


def _make_tool() -> NotionTool:
    return NotionTool()


@pytest.mark.asyncio
async def test_notion_check_auth_returns_auth_required() -> None:
    tool = _make_tool()
    with patch(
        "nanobot.agent.tools.notion.get_oauth_token",
        new_callable=AsyncMock,
        return_value=_AUTH_REQUIRED_PAYLOAD,
    ):
        result = await tool.check_auth()

    assert result == {"status": "auth_required", "service": "notion"}


@pytest.mark.asyncio
async def test_notion_execute_returns_auth_required_when_no_token() -> None:
    tool = _make_tool()
    with patch(
        "nanobot.agent.tools.notion.get_oauth_token",
        new_callable=AsyncMock,
        return_value=_AUTH_REQUIRED_PAYLOAD,
    ):
        result = await tool.execute(action="search", query="design docs")

    data = json.loads(result)
    assert data["status"] == "auth_required"
    assert data["service"] == "notion"


@pytest.mark.asyncio
async def test_notion_search_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool()

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "results": [
                    {
                        "id": "page-1",
                        "object": "page",
                        "properties": {
                            "title": {
                                "title": [{"plain_text": "Roadmap"}],
                            }
                        },
                        "url": "https://notion.so/page-1",
                    }
                ]
            }

    class _FakeAsyncClient:
        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> _FakeResponse:
            assert url.endswith("/search")
            assert json == {"query": "design docs"}
            assert headers["Authorization"] == "Bearer secret-notion-token"
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    with patch(
        "nanobot.agent.tools.notion.get_oauth_token",
        new_callable=AsyncMock,
        return_value=_AUTHORIZED_TOKEN_PAYLOAD,
    ):
        result = await tool.execute(action="search", query="design docs")

    data = json.loads(result)
    assert data["count"] == 1
    assert data["results"][0]["title"] == "Roadmap"


@pytest.mark.asyncio
async def test_notion_read_page_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool()

    class _FakeResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    class _FakeAsyncClient:
        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> _FakeResponse:
            if url.endswith("/pages/page-1"):
                return _FakeResponse({"id": "page-1", "url": "https://notion.so/page-1"})
            return _FakeResponse(
                {
                    "results": [
                        {
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [{"plain_text": "Hello from Notion"}],
                            },
                        }
                    ]
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    with patch(
        "nanobot.agent.tools.notion.get_oauth_token",
        new_callable=AsyncMock,
        return_value=_AUTHORIZED_TOKEN_PAYLOAD,
    ):
        result = await tool.execute(action="read_page", page_id="page-1")

    data = json.loads(result)
    assert data["id"] == "page-1"
    assert data["content"] == "Hello from Notion"


@pytest.mark.asyncio
async def test_notion_create_page_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool()

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"id": "page-2", "url": "https://notion.so/page-2"}

    class _FakeAsyncClient:
        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> _FakeResponse:
            assert url.endswith("/pages")
            assert json["parent"] == {"page_id": "parent-1"}
            assert json["properties"]["title"]["title"][0]["text"]["content"] == "My note"
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    with patch(
        "nanobot.agent.tools.notion.get_oauth_token",
        new_callable=AsyncMock,
        return_value=_AUTHORIZED_TOKEN_PAYLOAD,
    ):
        result = await tool.execute(
            action="create_page",
            parent_id="parent-1",
            title="My note",
            content="Body text",
        )

    data = json.loads(result)
    assert data["success"] is True
    assert data["id"] == "page-2"


@pytest.mark.asyncio
async def test_notion_append_block_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _make_tool()

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class _FakeAsyncClient:
        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def patch(self, url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> _FakeResponse:
            assert url.endswith("/blocks/block-1/children")
            assert json["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "Append me"
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    with patch(
        "nanobot.agent.tools.notion.get_oauth_token",
        new_callable=AsyncMock,
        return_value=_AUTHORIZED_TOKEN_PAYLOAD,
    ):
        result = await tool.execute(action="append_block", block_id="block-1", content="Append me")

    data = json.loads(result)
    assert data == {"success": True, "block_id": "block-1"}
