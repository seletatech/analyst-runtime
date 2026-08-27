"""Thin Composio tool-router REST bridge."""

from __future__ import annotations

import json
import os
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx

from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.session.manager import SessionManager
from analyst_runtime.utils.helpers import safe_filename

_DEFAULT_BASE_URL = "https://backend.composio.dev"
_INLINE_RESULT_LIMIT = 1600


class _ComposioBaseTool(Tool):
    def __init__(self, workspace: Path | None = None):
        self._workspace_provided = workspace is not None
        self._workspace = (workspace or Path.cwd()).expanduser()
        self._context: ContextVar[tuple[str, str, str | None]] = ContextVar(
            "composio_tool_context",
            default=("cli", "direct", None),
        )

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        normalized_channel = channel or "cli"
        normalized_chat_id = chat_id or "direct"
        normalized_session_key = session_key.strip() if isinstance(session_key, str) and session_key.strip() else None
        self._context.set((normalized_channel, normalized_chat_id, normalized_session_key))

    @property
    def _channel(self) -> str:
        """Compatibility view of the task-local channel for integrations/tests."""
        return self._context.get()[0]

    @property
    def _chat_id(self) -> str:
        """Compatibility view of the task-local chat id for integrations/tests."""
        return self._context.get()[1]

    def _session_key(self) -> str:
        channel, chat_id, session_key_override = self._context.get()
        return session_key_override or f"{channel}:{chat_id}"

    def _session_manager(self) -> SessionManager:
        return SessionManager(self._workspace)

    def _load_context(self) -> dict[str, str]:
        channel, chat_id, _ = self._context.get()
        return {"channel": channel, "chat_id": chat_id, "session_key": self._session_key()}

    def _load_composio_metadata(self) -> dict[str, Any]:
        session = self._session_manager().get_or_create(self._session_key())
        metadata = session.metadata.get("composio")
        return metadata if isinstance(metadata, dict) else {}

    def _save_composio_metadata(self, metadata: dict[str, Any]) -> None:
        session_manager = self._session_manager()
        session = session_manager.get_or_create(self._session_key())
        session.metadata["composio"] = metadata
        session_manager.save(session)

    def _clear_composio_metadata(self) -> None:
        self._save_composio_metadata({})

    def _composio_credentials_path(self) -> Path:
        workspace_path = self._workspace / ".analyst-runtime" / "composio" / "credentials.json"
        if self._workspace_provided or os.environ.get("WORKSPACE_PATH") or workspace_path.exists():
            return workspace_path
        return Path.home() / ".analyst-runtime" / "composio" / "credentials.json"

    def _load_local_credentials(self) -> dict[str, Any]:
        path = self._composio_credentials_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid Composio credentials file at {path}: {exc}") from exc
        return payload if isinstance(payload, dict) else {}

    def _headers(self) -> dict[str, str]:
        credentials = self._load_local_credentials()
        api_key = str(credentials.get("api_key") or "").strip()
        if not api_key and not self._workspace_provided and not os.environ.get("WORKSPACE_PATH"):
            api_key = os.environ.get("COMPOSIO_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                f"Local Composio credentials are required at {self._composio_credentials_path()}"
            )
        return {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        credentials = self._load_local_credentials()
        local_base_url = str(credentials.get("base_url") or "").strip()
        if local_base_url:
            return local_base_url.rstrip("/")
        return os.environ.get("COMPOSIO_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")

    def _callback_url(self) -> str:
        base_url = os.environ.get("COMPOSIO_CALLBACK_BASE_URL", "").strip().rstrip("/")
        return f"{base_url}/api/integrations/composio/callback" if base_url else ""

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{self._base_url()}{path}",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}

    async def _create_tool_router_session(self) -> str:
        owner_id = os.environ.get("OWNER_ID", "").strip()
        if not owner_id:
            raise ValueError("OWNER_ID is required to create a Composio tool-router session")

        payload: dict[str, Any] = {"user_id": owner_id}
        callback_url = self._callback_url()
        if callback_url:
            payload["manage_connections"] = {"callback_url": callback_url}

        data = await self._post_json("/api/v3/tool_router/session", payload)
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("Composio session creation response missing session_id")

        self._save_composio_metadata({"session_id": session_id})
        return session_id

    def _http_error_details(self, exc: httpx.HTTPStatusError) -> dict[str, Any]:
        try:
            payload = exc.response.json()
        except Exception:
            return {}

        if not isinstance(payload, dict):
            return {}

        error = payload.get("error")
        return error if isinstance(error, dict) else payload

    async def _ensure_tool_router_session(self, *, refresh: bool = False) -> str:
        metadata = self._load_composio_metadata()
        session_id = str(metadata.get("session_id") or "").strip()
        if session_id and not refresh:
            return session_id

        if refresh:
            self._clear_composio_metadata()

        return await self._create_tool_router_session()

    def _is_stale_session_error(self, exc: Exception) -> bool:
        if not isinstance(exc, httpx.HTTPStatusError):
            return False

        if exc.response.status_code not in {404, 410}:
            return False

        error = self._http_error_details(exc)
        status = str(error.get("status") or "").strip().lower()
        slug = str(error.get("slug") or "").strip().lower()

        if status in {"invalid_session", "invalid-session", "session_expired", "session-invalid"}:
            return True

        if slug in {"invalid_session", "invalid-session", "session_expired", "session-invalid"}:
            return True

        return False

    async def _post_tool_router_session(self, session_path: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        session_id = await self._ensure_tool_router_session()
        path = f"/api/v3/tool_router/session/{session_id}{session_path}"

        try:
            return session_id, await self._post_json(path, payload)
        except Exception as exc:
            if not self._is_stale_session_error(exc):
                raise

            session_id = await self._ensure_tool_router_session(refresh=True)
            path = f"/api/v3/tool_router/session/{session_id}{session_path}"
            return session_id, await self._post_json(path, payload)

    def _compact_json(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _compact_tool_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for key in ("tool_slug", "name", "toolkit", "description", "execution_guidance", "difficulty"):
            value = item.get(key)
            if value not in (None, "", [], {}):
                summary[key] = value
        return summary or {"tool_slug": item.get("tool_slug") or item.get("name") or ""}

    def _tool_summary_from_response(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        if isinstance(response.get("tools"), list):
            return [self._compact_tool_summary(item) for item in response["tools"] if isinstance(item, dict)]

        schemas = response.get("tool_schemas") if isinstance(response.get("tool_schemas"), dict) else {}
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()

        for result in response.get("results") or []:
            if not isinstance(result, dict):
                continue
            result_guidance = result.get("execution_guidance") or result.get("use_case")
            toolkits = result.get("toolkits") or []
            primary = list(result.get("primary_tool_slugs") or [])
            related = list(result.get("related_tool_slugs") or [])
            for slug in primary + related:
                if not isinstance(slug, str) or slug in seen:
                    continue
                seen.add(slug)
                schema = schemas.get(slug) if isinstance(schemas, dict) else {}
                tool: dict[str, Any] = {"tool_slug": slug}
                toolkit = (schema or {}).get("toolkit") or (toolkits[0] if toolkits else None)
                description = (schema or {}).get("description") or result.get("use_case")
                if toolkit:
                    tool["toolkit"] = toolkit
                if description:
                    tool["description"] = description
                if result_guidance:
                    tool["guidance"] = result_guidance
                if result.get("difficulty"):
                    tool["difficulty"] = result["difficulty"]
                tools.append(tool)

        if tools:
            return tools

        for slug, schema in (schemas or {}).items():
            if not isinstance(schema, dict):
                continue
            tool = self._compact_tool_summary(schema)
            tool["tool_slug"] = schema.get("tool_slug") or slug
            tools.append(tool)

        return tools

    def _toolkit_statuses(self, response: dict[str, Any]) -> list[dict[str, Any]]:
        compact_statuses: list[dict[str, Any]] = []
        for item in response.get("toolkit_connection_statuses") or []:
            if not isinstance(item, dict):
                continue
            summary: dict[str, Any] = {}
            for key in ("toolkit", "description", "has_active_connection", "account_selection", "status_message"):
                value = item.get(key)
                if value not in (None, "", [], {}):
                    summary[key] = value
            accounts = item.get("accounts") or []
            if accounts:
                summary["accounts"] = [
                    {
                        key: account.get(key)
                        for key in ("id", "alias", "status", "is_default")
                        if account.get(key) is not None
                    }
                    for account in accounts[:3]
                    if isinstance(account, dict)
                ]
            compact_statuses.append(summary)
        return compact_statuses

    def _spill_large_result(self, result: Any, *, tool_name: str) -> str:
        tool_results_dir = self._workspace / "sessions" / "tool-results"
        tool_results_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(result, str):
            content = result
            suffix = ".txt"
        else:
            content = json.dumps(result, ensure_ascii=False, indent=2)
            suffix = ".json"

        filename = f"{safe_filename(tool_name)}-{uuid.uuid4().hex[:10]}{suffix}"
        path = tool_results_dir / filename
        path.write_text(content, encoding="utf-8")
        return f"[saved to sessions/tool-results/{filename}]"

    def _normalize_execute_result(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            if payload.get("error") and not payload.get("data"):
                result: dict[str, Any] = {"error": payload["error"]}
                if payload.get("log_id"):
                    result["log_id"] = payload["log_id"]
                return result

            if "data" in payload or "log_id" in payload:
                result = {}
                if "data" in payload:
                    result["data"] = payload["data"]
                if payload.get("log_id"):
                    result["log_id"] = payload["log_id"]
                if payload.get("error"):
                    result["error"] = payload["error"]
                return result

        return payload

    async def _finalize_result(self, payload: Any, *, tool_name: str) -> str:
        normalized = self._normalize_execute_result(payload)
        rendered = normalized if isinstance(normalized, str) else self._compact_json(normalized)
        if len(rendered) > _INLINE_RESULT_LIMIT:
            return self._spill_large_result(normalized, tool_name=tool_name)
        return rendered


class ComposioSearchToolsTool(_ComposioBaseTool):
    @property
    def name(self) -> str:
        return "composio_search_tools"

    @property
    def description(self) -> str:
        return "Search Composio tool-router tools for a user intent."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "The user intent to search for",
                }
            },
            "required": ["intent"],
        }

    async def execute(self, intent: str, **kwargs: Any) -> str:
        try:
            session_id, response = await self._post_tool_router_session(
                "/search",
                {"queries": [{"use_case": intent}]},
            )
            normalized = {
                "session_id": session_id,
                "tools": self._tool_summary_from_response(response),
                "toolkit_connection_statuses": self._toolkit_statuses(response),
            }
            guidance = response.get("next_steps_guidance") or response.get("guidance")
            if guidance:
                normalized["guidance"] = guidance
            return self._compact_json(normalized)
        except Exception as exc:
            return self._compact_json({"error": str(exc)})


class ComposioManageConnectionsTool(_ComposioBaseTool):
    @property
    def name(self) -> str:
        return "composio_manage_connections"

    @property
    def description(self) -> str:
        return "Create a Composio connect-link session for a toolkit."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "toolkit": {
                    "type": "string",
                    "description": "The toolkit slug to connect",
                }
            },
            "required": ["toolkit"],
        }

    async def execute(self, toolkit: str, **kwargs: Any) -> str:
        try:
            payload: dict[str, Any] = {"toolkit": toolkit}
            callback_url = self._callback_url()
            if callback_url:
                payload["callback_url"] = callback_url
            _, response = await self._post_tool_router_session("/link", payload)
            normalized = {
                "status": response.get("status") or "auth_required",
                "toolkit": toolkit,
                "redirect_url": response.get("redirect_url"),
            }
            if response.get("connected_account_id"):
                normalized["connected_account_id"] = response["connected_account_id"]
            return self._compact_json(normalized)
        except Exception as exc:
            return self._compact_json({"status": "error", "toolkit": toolkit, "error": str(exc)})


class ComposioExecuteToolsTool(_ComposioBaseTool):
    @property
    def name(self) -> str:
        return "composio_execute_tools"

    @property
    def description(self) -> str:
        return "Execute a Composio tool-router tool by slug."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "tool_slug": {
                    "type": "string",
                    "description": "The tool slug to execute",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments passed to the tool",
                    "default": {},
                },
                "account": {
                    "type": "string",
                    "description": "Optional connected account id or alias",
                },
            },
            "required": ["tool_slug", "arguments"],
        }

    async def execute(self, tool_slug: str, arguments: dict[str, Any], account: str | None = None, **kwargs: Any) -> str:
        try:
            payload: dict[str, Any] = {"tool_slug": tool_slug, "arguments": arguments}
            if account:
                payload["account"] = account
            session_id, response = await self._post_tool_router_session(
                "/execute",
                payload,
            )
            return await self._finalize_result(response, tool_name=tool_slug)
        except Exception as exc:
            return self._compact_json({"error": str(exc), "tool_slug": tool_slug})
