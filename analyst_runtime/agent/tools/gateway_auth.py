"""Authentication helpers and GatewayAuthTool for gateway tool calls."""

import json
import os
from typing import Any

import httpx

from analyst_runtime.agent.tools.base import Tool

_SERVICE_TOKEN_ENDPOINTS: dict[str, str] = {
    "google": "/tools/google/oauth/start",
    "gmail": "/tools/google/oauth/token",
    "google_calendar": "/tools/google/oauth/token",
    "google_drive": "/tools/google/oauth/token",
    "google_workspace": "/tools/google/oauth/token",
    "notion": "/tools/notion/oauth/token",
}

# Google services that use the per-service /token endpoint (vs the broad /start)
_GOOGLE_SCOPED_SERVICES = frozenset({"gmail", "google_calendar", "google_drive", "google_workspace"})

_CREDENTIALS_ENDPOINTS = {
    "google": "/tools/google/oauth/credentials",
    "x": "/tools/x/credentials",
}

_STORE_ENDPOINTS = {
    "google": "/tools/google/oauth/byoc-store",
    "x": "/tools/x/credentials",
}


async def store_credentials(service: str, **data: Any) -> dict:
    """Store credentials for a service (e.g. X username/password)."""
    endpoint = _STORE_ENDPOINTS.get(service)
    if not endpoint:
        raise ValueError(f"No store endpoint for service: {service}")
    async with gateway_client(timeout=15.0) as client:
        resp = await client.post(endpoint, json=data)
        resp.raise_for_status()
        return resp.json()


async def fetch_credentials(service: str) -> dict:
    """Fetch stored OAuth credentials from the gateway (for writing to local file)."""
    endpoint = _CREDENTIALS_ENDPOINTS.get(service)
    if not endpoint:
        raise ValueError(f"No credentials endpoint for service: {service}")
    async with gateway_client(timeout=15.0) as client:
        resp = await client.get(endpoint)
        resp.raise_for_status()
        return resp.json()


def get_gateway_url() -> str:
    """Return the FastAPI gateway base URL."""
    return os.environ.get("GATEWAY_URL", "http://fastapi:8000").rstrip("/")


def get_auth_headers() -> dict[str, str]:
    """Return headers for gateway tool calls (auth + sandbox context)."""
    token = os.environ.get("GATEWAY_JWT_TOKEN", "")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # All /tools/* calls are internal-only and must include sandbox identity.
    sandbox_id = os.environ.get("SANDBOX_ID", "").strip()
    project_id = os.environ.get("PROJECT_ID", "").strip()
    owner_id = os.environ.get("OWNER_ID", "").strip()
    if sandbox_id:
        headers["X-Sandbox-Id"] = sandbox_id
    if project_id:
        headers["X-Project-Id"] = project_id
    if owner_id:
        headers["X-Owner-Id"] = owner_id
    return headers


def gateway_client(timeout: float = 30.0) -> httpx.AsyncClient:
    """Create an httpx AsyncClient pre-configured for gateway calls."""
    return httpx.AsyncClient(
        base_url=get_gateway_url(),
        headers=get_auth_headers(),
        timeout=timeout,
        # The gateway is an internal service. macOS proxy auto-discovery can
        # otherwise route localhost/Docker hostnames through a desktop proxy
        # and turn a healthy service into an empty 502 response.
        trust_env=False,
    )


async def get_oauth_token(service: str) -> dict[str, Any]:
    """Resolve a per-user OAuth access token via the gateway."""
    endpoint = _SERVICE_TOKEN_ENDPOINTS.get(service)
    if not endpoint:
        raise ValueError(f"Unsupported OAuth service: {service}")

    if service == "google":
        # Broad Google auth — request all common Workspace services at once
        payload: dict[str, Any] = {"services": ["gmail", "google_calendar"]}
    elif service in _GOOGLE_SCOPED_SERVICES:
        # Per-service token check
        payload = {"service": service}
    else:
        payload = {"service": service}

    async with gateway_client(timeout=30.0) as client:
        resp = await client.post(endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid OAuth token response from gateway")
    return data


def format_auth_required_result(service: str) -> str:
    """Compact auth-required payload returned to the LLM — no URL, no JWT."""
    return json.dumps(
        {
            "status": "auth_required",
            "service": service,
            "message": (
                f"Call gateway_auth(action='get_auth_url', service='{service}') "
                "to get the authorization link, then send it to the user."
            ),
        }
    )


class GatewayAuthTool(Tool):
    """Auth and credential management for gateway services."""

    @property
    def name(self) -> str:
        return "gateway_auth"

    @property
    def description(self) -> str:
        return (
            "Manage auth and credentials for gateway services. "
            "action='get_auth_url' — get OAuth URL (google, notion); "
            "action='fetch_credentials' — retrieve stored credentials (google, x); "
            "action='store_credentials' — save credentials for a service (x). "
            "For x cookies: pass auth_token and ct0. For x login: pass username and password."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get_auth_url", "fetch_credentials", "store_credentials"],
                    "description": (
                        "'get_auth_url' — OAuth flow (google, notion); "
                        "'fetch_credentials' — retrieve stored creds; "
                        "'store_credentials' — save username/password (x only)"
                    ),
                },
                "service": {
                    "type": "string",
                    "description": "Service name: 'google', 'notion', or 'x'",
                },
                "auth_token": {
                    "type": "string",
                    "description": "X auth_token cookie value (store_credentials, preferred over username/password)",
                },
                "ct0": {
                    "type": "string",
                    "description": "X ct0 CSRF cookie value (store_credentials, required with auth_token)",
                },
                "username": {
                    "type": "string",
                    "description": "Username for store_credentials (x, legacy fallback)",
                },
                "password": {
                    "type": "string",
                    "description": "Password for store_credentials (x, legacy fallback)",
                },
                "totp_secret": {
                    "type": "string",
                    "description": "Optional TOTP secret for 2FA (store_credentials, x)",
                },
            },
            "required": ["action", "service"],
        }

    async def execute(self, action: str, service: str, **kwargs: Any) -> str:
        if action == "fetch_credentials":
            try:
                creds = await fetch_credentials(service)
                return json.dumps(creds)
            except Exception as exc:
                return json.dumps({"error": f"Failed to fetch credentials for {service}: {exc}"})

        if action == "store_credentials":
            # Pass through any non-empty kwargs — gateway validates required fields.
            # Supports: auth_token+ct0 (cookie-based, preferred) or username+password (legacy).
            data: dict[str, Any] = {k: str(v).strip() for k, v in kwargs.items() if v and str(v).strip()}
            if not data:
                return json.dumps({"error": "No credential fields provided for store_credentials"})
            try:
                result = await store_credentials(service, **data)
                return json.dumps(result)
            except Exception as exc:
                return json.dumps({"error": f"Failed to store credentials for {service}: {exc}"})

        if action != "get_auth_url":
            return json.dumps({"error": f"Unknown action: {action}"})

        try:
            payload = await get_oauth_token(service)
        except Exception as exc:
            return json.dumps({"error": f"Failed to fetch auth URL for {service}: {exc}"})

        if payload.get("status") == "auth_required":
            auth_url = payload.get("auth_url", "")
            if not auth_url:
                return json.dumps({"error": "Gateway returned auth_required but no auth_url"})
            return json.dumps({"auth_url": auth_url, "service": service})

        if payload.get("status") == "not_configured":
            # For Google scoped services, not_configured means no token exists yet (never authorized).
            # Auto-escalate to broad Google auth to start the initial OAuth flow.
            if service in _GOOGLE_SCOPED_SERVICES:
                try:
                    broad_payload = await get_oauth_token("google")
                except Exception as exc:
                    return json.dumps({"error": f"Failed to fetch auth URL for google: {exc}"})
                if broad_payload.get("status") == "auth_required":
                    auth_url = broad_payload.get("auth_url", "")
                    if not auth_url:
                        return json.dumps({"error": "Gateway returned auth_required but no auth_url"})
                    return json.dumps({"auth_url": auth_url, "service": "google"})
                if broad_payload.get("status") in ("authorized", "already_authorized"):
                    # Broad Google auth exists but per-service token is still missing —
                    # report as authorized so the caller retries the gws operation.
                    return json.dumps({"status": "already_authorized", "service": "google"})
            return json.dumps(
                {
                    "status": "not_configured",
                    "service": payload.get("service", service),
                    "reason": payload.get("reason", "gateway_not_configured"),
                }
            )

        # Already authorized
        return json.dumps({"status": "already_authorized", "service": service})
