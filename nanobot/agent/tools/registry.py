"""Tool registry for dynamic tool management."""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool

# Environment variable names whose values should be masked in error output.
_SECRET_ENV_NAMES = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|APP_PASSWORD)", re.IGNORECASE
)

# Default timeout for non-shell tools (seconds).
_DEFAULT_TOOL_TIMEOUT = 30
# Timeout for shell tool (seconds) — shells have their own internal timeout
# but this acts as an outer safety net.
# 180s: allows Chromium first-launch (60-90s) + X.com SPA load in resource-constrained ECS.
_SHELL_TOOL_TIMEOUT = 180


def _mask_secrets(text: str) -> str:
    """Replace values of known secret env vars with '***' in *text*."""
    for name, value in os.environ.items():
        if _SECRET_ENV_NAMES.search(name) and value and len(value) >= 6:
            text = text.replace(value, "***")
    return text


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def pre_execute(self, tool_calls: list) -> "dict[str, str | None]":
        """
        Pre-flight authorization check for all tool calls.

        Runs check_auth() on every tool concurrently.  For each call that needs
        authorization the first occurrence per service gets a compact
        ``auth_required`` payload; subsequent calls to the same service get a
        ``skipped`` payload.  Calls that are authorized get ``None`` (proceed).

        Args:
            tool_calls: List of tool-call objects (must have .id and .name).

        Returns:
            Dict mapping tool_call_id -> compact JSON string | None.
        """
        async def _check(tc):
            tool = self._tools.get(tc.name)
            if tool is None:
                return None
            try:
                return await tool.check_auth()
            except Exception as exc:
                logger.warning("check_auth failed for tool '%s': %s", tc.name, exc)
                return None  # Fail open — let execute() handle it

        auth_results = await asyncio.gather(*[_check(tc) for tc in tool_calls])

        seen_services: set[str] = set()
        results: dict[str, str | None] = {}
        for tc, auth_result in zip(tool_calls, auth_results):
            if auth_result is None:
                results[tc.id] = None
            else:
                service = auth_result.get("service", tc.name)
                if service not in seen_services:
                    seen_services.add(service)
                    results[tc.id] = json.dumps(auth_result)
                else:
                    results[tc.id] = json.dumps({
                        "status": "skipped",
                        "service": service,
                        "reason": f"Authorization pending for {service}; retry after authorization completes.",
                    })
        return results

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        Execute a tool by name with given parameters.

        Args:
            name: Tool name.
            params: Tool parameters.

        Returns:
            Tool execution result as string.

        Raises:
            KeyError: If tool not found.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found"

        try:
            errors = tool.validate_params(params)
            if errors:
                message = f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors)
                if name in {"write_file", "append_file"} and any("missing required content" in error for error in errors):
                    message += (
                        " Large generated files may have been truncated. "
                        "Do not switch to exec or shell redirection. "
                        "Retry with append_file in chunks under 8000 characters, then use patch_file for targeted repairs."
                    )
                return message

            timeout = _SHELL_TOOL_TIMEOUT if name == "exec" else _DEFAULT_TOOL_TIMEOUT
            try:
                async with asyncio.timeout(timeout):
                    return await tool.execute(**params)
            except TimeoutError:
                logger.warning("Tool '%s' timed out after %ds", name, timeout)
                return f"Error: Tool '{name}' timed out after {timeout}s"
        except Exception as e:
            masked = _mask_secrets(str(e))
            return f"Error executing {name}: {masked}"

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


_ADAPTERS_MANIFEST = Path(__file__).parent / "adapters.yaml"


def _build_adapter_kwargs(
    args_spec: dict,
    env: dict[str, str],
    workspace: Path,
) -> dict:
    """Resolve constructor kwargs from an adapter manifest entry."""
    kwargs: dict = {}
    for param, spec in (args_spec or {}).items():
        if "env" in spec:
            kwargs[param] = env.get(spec["env"])
        elif spec.get("workspace"):
            kwargs[param] = workspace
        elif "static" in spec:
            kwargs[param] = spec["static"]
    return kwargs


def register_integration_tools(
    registry: "ToolRegistry",
    env: dict[str, str] | None = None,
    workspace: Path | None = None,
    manifest: Path | None = None,
) -> None:
    """
    Register all integration tool adapters declared in adapters.yaml.

    Adding a new tool requires only two files:
      1. A Python Tool subclass in nanobot/agent/tools/
      2. An entry in nanobot/agent/tools/adapters.yaml

    No changes to loop.py or this function are needed.
    """
    import importlib

    import yaml

    if env is None:
        env = dict(os.environ)

    workspace_path = workspace or Path.cwd()
    manifest_path = manifest or _ADAPTERS_MANIFEST

    try:
        with open(manifest_path) as fh:
            spec = yaml.safe_load(fh)
    except Exception as exc:
        logger.error("Failed to load adapter manifest %s: %s", manifest_path, exc)
        return

    for entry in spec.get("adapters", []):
        adapter_id = entry.get("id", "<unknown>")
        try:
            mod = importlib.import_module(entry["module"])
            cls = getattr(mod, entry["class"])
            kwargs = _build_adapter_kwargs(entry.get("args", {}), env, workspace_path)
            registry.register(cls(**kwargs))
        except Exception as exc:
            logger.warning("Adapter '%s' failed to register: %s", adapter_id, exc)
