"""Tests for the declarative adapter registry (adapters.yaml + register_integration_tools).

Covers:
- _build_adapter_kwargs: env-var resolution, workspace injection, static values
- register_integration_tools: reads manifest, instantiates tools, registers them
- Graceful skip when a module or class is not found
- Missing manifest handled without crashing
- The live adapters.yaml registers at least the Composio tools
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from nanobot.agent.tools.registry import (
    ToolRegistry,
    _build_adapter_kwargs,
    register_integration_tools,
)


# ---------------------------------------------------------------------------
# _build_adapter_kwargs
# ---------------------------------------------------------------------------

def test_env_arg_resolved_from_env_dict() -> None:
    spec = {"api_key": {"env": "MY_API_KEY"}}
    kwargs = _build_adapter_kwargs(spec, env={"MY_API_KEY": "secret-123"}, workspace=Path("/ws"))
    assert kwargs == {"api_key": "secret-123"}


def test_env_arg_is_none_when_var_absent() -> None:
    spec = {"api_key": {"env": "MISSING_VAR"}}
    kwargs = _build_adapter_kwargs(spec, env={}, workspace=Path("/ws"))
    assert kwargs == {"api_key": None}


def test_workspace_arg_injected() -> None:
    ws = Path("/sandbox/workspace")
    spec = {"workspace": {"workspace": True}}
    kwargs = _build_adapter_kwargs(spec, env={}, workspace=ws)
    assert kwargs == {"workspace": ws}


def test_static_arg_used_as_is() -> None:
    spec = {"channel": {"static": "cli"}}
    kwargs = _build_adapter_kwargs(spec, env={}, workspace=Path("/ws"))
    assert kwargs == {"channel": "cli"}


def test_empty_args_spec_returns_empty_dict() -> None:
    assert _build_adapter_kwargs({}, env={"X": "y"}, workspace=Path("/ws")) == {}


# ---------------------------------------------------------------------------
# register_integration_tools — manifest loading
# ---------------------------------------------------------------------------

def _make_manifest(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "adapters.yaml"
    p.write_text(textwrap.dedent(content))
    return p


def test_tool_from_manifest_is_registered(tmp_path: Path) -> None:
    """A tool declared in the manifest gets registered in the ToolRegistry."""
    manifest = _make_manifest(tmp_path, """
        adapters:
          - id: notion
            class: NotionTool
            module: nanobot.agent.tools.notion
            args:
              api_key: {env: NOTION_API_KEY}
    """)
    registry = ToolRegistry()
    register_integration_tools(registry, env={"NOTION_API_KEY": "k"}, manifest=manifest)
    assert "notion" in registry


def test_multiple_tools_from_manifest_registered(tmp_path: Path) -> None:
    """All entries in the manifest are registered."""
    manifest = _make_manifest(tmp_path, """
        adapters:
          - id: gateway_auth
            class: GatewayAuthTool
            module: nanobot.agent.tools.gateway_auth
          - id: notion
            class: NotionTool
            module: nanobot.agent.tools.notion
            args:
              api_key: {env: NOTION_API_KEY}
    """)
    registry = ToolRegistry()
    register_integration_tools(registry, env={}, manifest=manifest)
    assert len(registry) == 2


def test_bad_module_skipped_gracefully(tmp_path: Path) -> None:
    """An entry with a missing module is skipped; other entries still register."""
    manifest = _make_manifest(tmp_path, """
        adapters:
          - id: bad
            class: DoesNotExist
            module: nanobot.agent.tools.does_not_exist_xyzzy
          - id: gateway_auth
            class: GatewayAuthTool
            module: nanobot.agent.tools.gateway_auth
    """)
    registry = ToolRegistry()
    register_integration_tools(registry, env={}, manifest=manifest)
    # bad entry skipped, gateway_auth still registered
    assert "gateway_auth" in registry
    assert len(registry) == 1


def test_missing_manifest_does_not_raise(tmp_path: Path) -> None:
    """register_integration_tools does not crash when the manifest file is absent."""
    registry = ToolRegistry()
    register_integration_tools(
        registry,
        env={},
        manifest=tmp_path / "nonexistent.yaml",
    )
    assert len(registry) == 0


# ---------------------------------------------------------------------------
# Live adapters.yaml sanity check
# ---------------------------------------------------------------------------

def test_live_manifest_registers_composio_tools(tmp_path: Path) -> None:
    """The real adapters.yaml includes all three Composio tools."""
    registry = ToolRegistry()
    register_integration_tools(
        registry,
        env={"COMPOSIO_API_KEY": "test-key"},
        workspace=tmp_path,
    )
    assert "composio_search_tools" in registry
    assert "composio_manage_connections" in registry
    assert "composio_execute_tools" in registry


def test_live_manifest_registers_notion_and_gateway(tmp_path: Path) -> None:
    """The real adapters.yaml includes core tools like gateway_auth and notion."""
    registry = ToolRegistry()
    register_integration_tools(registry, env={}, workspace=tmp_path)
    assert "gateway_auth" in registry
    assert "notion" in registry
