"""Tests for Google credentials sync at sandbox startup.

Design: at sandbox start, fetch credentials from the gateway and write them to
the gws credentials file. This means the model never needs to manage auth state —
gws works immediately if the user has authorized Google, and only fails (gracefully)
if they haven't.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import nanobot.cli.commands as commands


# ---------------------------------------------------------------------------
# Helpers — fake httpx.Client for sync requests
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        return self._body


class _FakeClient:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses
        self.requests: list[str] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def get(self, path: str) -> _FakeResponse:
        self.requests.append(path)
        return self._responses.get(path, _FakeResponse(404))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

VALID_CREDS = {
    "type": "authorized_user",
    "client_id": "fake-client-id",
    "client_secret": "fake-secret",
    "refresh_token": "fake-refresh-token",
}


def test_sync_writes_credentials_when_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the gateway returns valid creds, write them to the gws credentials file."""
    fake_client = _FakeClient({
        "/tools/google/oauth/credentials": _FakeResponse(200, VALID_CREDS),
    })
    monkeypatch.setattr(commands, "_make_http_client", lambda **kw: fake_client)

    commands._sync_google_credentials(
        gateway_url="http://gateway:8000",
        gateway_jwt="test-jwt",
        workspace=tmp_path,
    )

    creds_path = tmp_path / ".nanobot" / "oauth" / "gws" / "credentials.json"
    assert creds_path.exists(), "credentials.json should be written"
    written = json.loads(creds_path.read_text())
    assert written == VALID_CREDS


def test_sync_skips_when_not_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the gateway returns 404, silently skip — new user, not yet authorized."""
    fake_client = _FakeClient({
        "/tools/google/oauth/credentials": _FakeResponse(404),
    })
    monkeypatch.setattr(commands, "_make_http_client", lambda **kw: fake_client)

    commands._sync_google_credentials(
        gateway_url="http://gateway:8000",
        gateway_jwt="test-jwt",
        workspace=tmp_path,
    )

    creds_path = tmp_path / ".nanobot" / "oauth" / "gws" / "credentials.json"
    assert not creds_path.exists(), "credentials.json should NOT be written for new user"


def test_sync_skips_on_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the gateway is unreachable, silently skip — don't crash sandbox startup."""
    def _raising_client(**kw: Any) -> Any:
        raise ConnectionError("gateway unreachable")

    monkeypatch.setattr(commands, "_make_http_client", _raising_client)

    # Must not raise
    commands._sync_google_credentials(
        gateway_url="http://gateway:8000",
        gateway_jwt="test-jwt",
        workspace=tmp_path,
    )

    creds_path = tmp_path / ".nanobot" / "oauth" / "gws" / "credentials.json"
    assert not creds_path.exists()


def test_sync_skips_when_response_missing_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Incomplete credentials (no refresh_token) should not be written — unusable for gws."""
    incomplete = {"type": "authorized_user", "client_id": "x", "client_secret": "y"}
    fake_client = _FakeClient({
        "/tools/google/oauth/credentials": _FakeResponse(200, incomplete),
    })
    monkeypatch.setattr(commands, "_make_http_client", lambda **kw: fake_client)

    commands._sync_google_credentials(
        gateway_url="http://gateway:8000",
        gateway_jwt="test-jwt",
        workspace=tmp_path,
    )

    creds_path = tmp_path / ".nanobot" / "oauth" / "gws" / "credentials.json"
    assert not creds_path.exists(), "incomplete creds must not be written"


def test_sync_overwrites_stale_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each startup overwrites the file — keeps creds in sync with Supabase (Option B)."""
    stale = {**VALID_CREDS, "refresh_token": "old-token"}
    creds_path = tmp_path / ".nanobot" / "oauth" / "gws" / "credentials.json"
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text(json.dumps(stale))

    fresh = {**VALID_CREDS, "refresh_token": "fresh-token"}
    fake_client = _FakeClient({
        "/tools/google/oauth/credentials": _FakeResponse(200, fresh),
    })
    monkeypatch.setattr(commands, "_make_http_client", lambda **kw: fake_client)

    commands._sync_google_credentials(
        gateway_url="http://gateway:8000",
        gateway_jwt="test-jwt",
        workspace=tmp_path,
    )

    written = json.loads(creds_path.read_text())
    assert written["refresh_token"] == "fresh-token", "stale file should be overwritten"


def test_sync_sends_auth_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth headers (JWT + sandbox identity) must be passed to the gateway request."""
    captured_kwargs: dict[str, Any] = {}

    class _CapturingClient(_FakeClient):
        pass

    def _capturing_factory(**kw: Any) -> _FakeClient:
        captured_kwargs.update(kw)
        return _FakeClient({
            "/tools/google/oauth/credentials": _FakeResponse(200, VALID_CREDS),
        })

    monkeypatch.setattr(commands, "_make_http_client", _capturing_factory)

    commands._sync_google_credentials(
        gateway_url="http://gateway:8000",
        gateway_jwt="my-jwt-token",
        workspace=tmp_path,
        sandbox_id="sandbox-abc",
        owner_id="owner-xyz",
    )

    headers = captured_kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer my-jwt-token"
    assert headers.get("X-Sandbox-Id") == "sandbox-abc"
    assert headers.get("X-Owner-Id") == "owner-xyz"
