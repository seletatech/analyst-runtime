"""Helpers for keeping tool-call names safe across providers and session history."""

from __future__ import annotations

import re
from typing import Any

_VALID_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")


def sanitize_tool_name(name: str, fallback: str = "invalid_tool_call") -> str:
    """Return a Bedrock-safe tool name."""
    raw = (name or "").strip()
    if not raw:
        return fallback
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw):
        return raw

    match = _VALID_TOOL_NAME_RE.search(raw)
    if not match:
        return fallback
    return match.group(0)[:64]


def sanitize_openai_tool_calls(tool_calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return OpenAI-format tool calls with safe function names."""
    sanitized: list[dict[str, Any]] = []
    for tool_call in tool_calls or []:
        function = tool_call.get("function")
        if not isinstance(function, dict):
            sanitized.append(tool_call)
            continue
        sanitized.append(
            {
                **tool_call,
                "function": {
                    **function,
                    "name": sanitize_tool_name(str(function.get("name", ""))),
                },
            }
        )
    return sanitized
