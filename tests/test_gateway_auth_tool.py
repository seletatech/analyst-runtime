from __future__ import annotations

import json
from typing import Any

import pytest

from nanobot.agent.tools import gateway_auth
from nanobot.agent.tools import firecrawl


def test_gateway_client_does_not_inherit_desktop_proxy_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Client:
        pass

    def _client(**kwargs: Any) -> _Client:
        captured.update(kwargs)
        return _Client()

    monkeypatch.setattr(gateway_auth.httpx, "AsyncClient", _client)

    gateway_auth.gateway_client()

    assert captured["trust_env"] is False


@pytest.mark.asyncio
async def test_firecrawl_search_transport_timeout_exceeds_the_upstream_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, Any] = {}

    async def request(path: str, payload: dict[str, Any], *, timeout_s: float) -> dict[str, Any]:
        captured.update(path=path, payload=payload, timeout_s=timeout_s)
        return {"success": True, "data": {"web": []}}

    async def report(payload: dict[str, Any]) -> None:
        del payload

    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    monkeypatch.setenv("GATEWAY_JWT_TOKEN", "test-token")
    monkeypatch.setenv("SANDBOX_ID", "test-sandbox")
    monkeypatch.setenv("PROJECT_ID", "test-project")
    monkeypatch.setenv("OWNER_ID", "test-owner")
    monkeypatch.setattr(firecrawl, "_firecrawl_gateway_request", request)
    monkeypatch.setattr(firecrawl, "_report_firecrawl_event", report)

    result = json.loads(
        await firecrawl.FirecrawlSearchTool().execute(
            query="Firecrawl Search API",
            timeout_ms=60_000,
        )
    )

    assert result["success"] is True
    assert captured["timeout_s"] == 70.0


@pytest.mark.asyncio
async def test_get_oauth_token_routes_notion_to_notion_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"status": "authorized", "access_token": "notion-token"}

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, path: str, json: dict[str, Any]) -> _FakeResponse:
            requests.append((path, json))
            return _FakeResponse()

    monkeypatch.setattr(gateway_auth, "gateway_client", lambda timeout=30.0: _FakeClient())

    payload = await gateway_auth.get_oauth_token("notion")

    assert payload["status"] == "authorized"
    assert requests == [("/tools/notion/oauth/token", {"service": "notion"})]


@pytest.mark.asyncio
async def test_gateway_auth_returns_auth_url_for_notion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(service: str) -> dict[str, Any]:
        assert service == "notion"
        return {
            "status": "auth_required",
            "service": "notion",
            "auth_url": "https://api.notion.com/v1/oauth/authorize?mock=1",
        }

    monkeypatch.setattr(gateway_auth, "get_oauth_token", _fake_get)

    tool = gateway_auth.GatewayAuthTool()
    result = await tool.execute(action="get_auth_url", service="notion")

    data = json.loads(result)
    assert data == {
        "auth_url": "https://api.notion.com/v1/oauth/authorize?mock=1",
        "service": "notion",
    }


@pytest.mark.asyncio
async def test_gateway_auth_reports_already_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(service: str) -> dict[str, Any]:
        assert service == "notion"
        return {"status": "authorized", "access_token": "token-123"}

    monkeypatch.setattr(gateway_auth, "get_oauth_token", _fake_get)

    tool = gateway_auth.GatewayAuthTool()
    result = await tool.execute(action="get_auth_url", service="notion")

    assert json.loads(result) == {"status": "already_authorized", "service": "notion"}


@pytest.mark.asyncio
async def test_gateway_auth_reports_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(service: str) -> dict[str, Any]:
        assert service == "google"
        return {
            "status": "not_configured",
            "service": "google",
            "reason": "missing_google_client_config",
        }

    monkeypatch.setattr(gateway_auth, "get_oauth_token", _fake_get)

    tool = gateway_auth.GatewayAuthTool()
    result = await tool.execute(action="get_auth_url", service="google")

    assert json.loads(result) == {
        "status": "not_configured",
        "service": "google",
        "reason": "missing_google_client_config",
    }


@pytest.mark.asyncio
async def test_get_oauth_token_rejects_unsupported_service() -> None:
    with pytest.raises(ValueError, match="Unsupported OAuth service"):
        await gateway_auth.get_oauth_token("slack")


@pytest.mark.asyncio
async def test_get_oauth_token_routes_google_to_start_with_services_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "status": "auth_required",
                "services": ["gmail", "google_calendar"],
                "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?mock=1",
            }

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, path: str, json: dict[str, Any]) -> _FakeResponse:
            requests.append((path, json))
            return _FakeResponse()

    monkeypatch.setattr(gateway_auth, "gateway_client", lambda timeout=30.0: _FakeClient())

    await gateway_auth.get_oauth_token("google")

    assert requests == [
        ("/tools/google/oauth/start", {"services": ["gmail", "google_calendar"]}),
    ]


@pytest.mark.asyncio
async def test_get_oauth_token_routes_gmail_to_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"status": "authorized", "access_token": "gmail-token"}

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, path: str, json: dict[str, Any]) -> _FakeResponse:
            requests.append((path, json))
            return _FakeResponse()

    monkeypatch.setattr(gateway_auth, "gateway_client", lambda timeout=30.0: _FakeClient())

    payload = await gateway_auth.get_oauth_token("gmail")

    assert payload["status"] == "authorized"
    assert requests == [("/tools/google/oauth/token", {"service": "gmail"})]


@pytest.mark.asyncio
async def test_gateway_auth_google_scoped_not_configured_escalates_to_broad_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a Google scoped service returns not_configured, auto-escalate to broad google auth."""
    calls: list[str] = []

    async def _fake_get(service: str) -> dict[str, Any]:
        calls.append(service)
        if service == "google_calendar":
            return {"status": "not_configured", "service": "google_calendar", "reason": "no_token"}
        if service == "google":
            return {
                "status": "auth_required",
                "auth_url": "https://accounts.google.com/auth?mock=1",
            }
        return {"status": "authorized"}

    monkeypatch.setattr(gateway_auth, "get_oauth_token", _fake_get)

    tool = gateway_auth.GatewayAuthTool()
    result = await tool.execute(action="get_auth_url", service="google_calendar")

    data = json.loads(result)
    assert data == {"auth_url": "https://accounts.google.com/auth?mock=1", "service": "google"}
    assert calls == ["google_calendar", "google"]


@pytest.mark.asyncio
async def test_gateway_auth_gmail_not_configured_escalates_to_broad_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gmail not_configured also escalates to broad google auth."""
    calls: list[str] = []

    async def _fake_get(service: str) -> dict[str, Any]:
        calls.append(service)
        if service == "gmail":
            return {"status": "not_configured", "service": "gmail", "reason": "no_token"}
        return {
            "status": "auth_required",
            "auth_url": "https://accounts.google.com/auth?mock=2",
        }

    monkeypatch.setattr(gateway_auth, "get_oauth_token", _fake_get)

    tool = gateway_auth.GatewayAuthTool()
    result = await tool.execute(action="get_auth_url", service="gmail")

    data = json.loads(result)
    assert data["auth_url"] == "https://accounts.google.com/auth?mock=2"
    assert "google" in calls




@pytest.mark.asyncio
async def test_gateway_auth_store_credentials_google_posts_to_byoc_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """store_credentials for google posts client_id/secret to the byoc-store endpoint."""
    requests: list[tuple[str, dict[str, Any]]] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"status": "stored"}

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def post(self, path: str, json: dict[str, Any]) -> _FakeResponse:
            requests.append((path, json))
            return _FakeResponse()

    monkeypatch.setattr(gateway_auth, "gateway_client", lambda timeout=30.0: _FakeClient())

    tool = gateway_auth.GatewayAuthTool()
    result = await tool.execute(
        action="store_credentials",
        service="google",
        client_id="my-client.apps.googleusercontent.com",
        client_secret="my-secret",
    )

    data = json.loads(result)
    assert data == {"status": "stored"}
    assert requests == [
        (
            "/tools/google/oauth/byoc-store",
            {"client_id": "my-client.apps.googleusercontent.com", "client_secret": "my-secret"},
        )
    ]
