"""Tests for ToolRegistry — dispatch, timeouts, error masking, auth pre-flight."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "Returns the message param"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        }

    async def execute(self, message: str, **kwargs: Any) -> str:
        return message


class _BombTool(Tool):
    """Raises an exception on execute."""

    @property
    def name(self) -> str:
        return "bomb"

    @property
    def description(self) -> str:
        return "Always raises"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        raise RuntimeError("Something exploded with secret_value_1234567890abcd")


class _SlowTool(Tool):
    """Sleeps longer than the tool timeout."""

    @property
    def name(self) -> str:
        return "slow"

    @property
    def description(self) -> str:
        return "Always times out"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        await asyncio.sleep(60)
        return "never"


class _AuthRequiredTool(Tool):
    """Returns auth_required for a named service."""

    def __init__(self, service: str) -> None:
        self._service = service

    @property
    def name(self) -> str:
        return f"auth_tool_{self._service}"

    @property
    def description(self) -> str:
        return f"Requires auth for {self._service}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def check_auth(self) -> dict | None:
        return {"status": "auth_required", "service": self._service, "auth_url": "https://auth.example.com"}

    async def execute(self, **kwargs: Any) -> str:
        return "authenticated"


# ---------------------------------------------------------------------------
# ToolRegistry.execute — happy path, errors, timeouts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error() -> None:
    registry = ToolRegistry()
    result = await registry.execute("nonexistent", {})
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_execute_missing_required_param_returns_validation_error() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    result = await registry.execute("echo", {})  # missing `message`
    assert "invalid parameters" in result.lower() or "missing" in result.lower()


@pytest.mark.asyncio
async def test_execute_success_returns_tool_result() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    result = await registry.execute("echo", {"message": "hello"})
    assert result == "hello"


@pytest.mark.asyncio
async def test_execute_exception_masks_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    monkeypatch.setenv("MY_SECRET_KEY", "secret_value_1234567890abcd")

    registry = ToolRegistry()
    registry.register(_BombTool())
    result = await registry.execute("bomb", {})
    assert "secret_value_1234567890abcd" not in result
    assert "***" in result or "error" in result.lower()


@pytest.mark.asyncio
async def test_execute_timeout_returns_timeout_message(monkeypatch: pytest.MonkeyPatch) -> None:
    import nanobot.agent.tools.registry as reg_module
    monkeypatch.setattr(reg_module, "_DEFAULT_TOOL_TIMEOUT", 0.01)  # 10ms

    registry = ToolRegistry()
    registry.register(_SlowTool())
    result = await registry.execute("slow", {})
    assert "timed out" in result.lower()


# ---------------------------------------------------------------------------
# ToolRegistry — contains / len
# ---------------------------------------------------------------------------


def test_registry_contains_and_len() -> None:
    registry = ToolRegistry()
    assert "echo" not in registry
    assert len(registry) == 0

    registry.register(_EchoTool())
    assert "echo" in registry
    assert len(registry) == 1

    registry.unregister("echo")
    assert "echo" not in registry
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# ToolRegistry.pre_execute — auth pre-flight
# ---------------------------------------------------------------------------


class _SimpleToolCall:
    def __init__(self, call_id: str, name: str) -> None:
        self.id = call_id
        self.name = name


@pytest.mark.asyncio
async def test_pre_execute_authorized_tool_returns_none() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())

    results = await registry.pre_execute([_SimpleToolCall("call-1", "echo")])
    assert results["call-1"] is None


@pytest.mark.asyncio
async def test_pre_execute_auth_required_embeds_payload() -> None:
    registry = ToolRegistry()
    registry.register(_AuthRequiredTool("gmail"))

    tc = _SimpleToolCall("call-1", "auth_tool_gmail")
    results = await registry.pre_execute([tc])
    payload = json.loads(results["call-1"])
    assert payload["status"] == "auth_required"
    assert payload["service"] == "gmail"


@pytest.mark.asyncio
async def test_pre_execute_deduplicates_auth_required_per_service() -> None:
    """Second call to the same service gets 'skipped', not a duplicate auth_required."""
    registry = ToolRegistry()
    registry.register(_AuthRequiredTool("gmail"))

    tc1 = _SimpleToolCall("call-1", "auth_tool_gmail")
    tc2 = _SimpleToolCall("call-2", "auth_tool_gmail")
    results = await registry.pre_execute([tc1, tc2])

    first = json.loads(results["call-1"])
    second = json.loads(results["call-2"])
    assert first["status"] == "auth_required"
    assert second["status"] == "skipped"


@pytest.mark.asyncio
async def test_pre_execute_unknown_tool_returns_none() -> None:
    """Unknown tools don't crash pre_execute — fail open."""
    registry = ToolRegistry()
    results = await registry.pre_execute([_SimpleToolCall("call-1", "nonexistent")])
    assert results["call-1"] is None
