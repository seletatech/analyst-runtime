"""Firecrawl-backed search, scrape, and browser tools."""

from __future__ import annotations

import base64
import json
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.gateway_auth import gateway_client
from nanobot.utils.helpers import ensure_dir, safe_filename

DEFAULT_SEARCH_LIMIT = 5
DEFAULT_BROWSER_STEPS = 12
DEFAULT_BROWSER_FAILURES = 3
DEFAULT_BROWSER_TIMEOUT_MS = 90_000


def _workspace_path() -> Path:
    return Path(os.environ.get("WORKSPACE_PATH", "/workspace")).expanduser().resolve()


def _relative_to_workspace(path: Path) -> str:
    workspace = _workspace_path()
    try:
        return str(path.resolve().relative_to(workspace))
    except ValueError:
        return str(path.resolve())


def _artifact_root(task_id: str) -> Path:
    return ensure_dir(_workspace_path() / "artifacts" / "firecrawl" / safe_filename(task_id))


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _gateway_ready() -> bool:
    required = ["GATEWAY_JWT_TOKEN", "SANDBOX_ID", "PROJECT_ID", "OWNER_ID"]
    return all(os.environ.get(name, "").strip() for name in required)


def _gateway_not_configured(task_id: str, incomplete_reason: str) -> str:
    return json.dumps(
        {
            "success": False,
            "status": "not_configured",
            "task_id": task_id,
            "error": (
                "Firecrawl gateway is not configured. Set GATEWAY_URL, GATEWAY_JWT_TOKEN, "
                "SANDBOX_ID, PROJECT_ID, and OWNER_ID in the sandbox runtime."
            ),
            "incomplete_reason": incomplete_reason,
        },
        ensure_ascii=False,
    )


async def _firecrawl_gateway_request(
    path: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    async with gateway_client(timeout=timeout_s) as client:
        response = await client.post(path, json=payload)
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid Firecrawl gateway response shape")
    return data


def _resolve_workspace_file(path_str: str) -> Path:
    workspace = _workspace_path()
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.expanduser().resolve()
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError("Path must live inside the workspace")
    return resolved


def _load_cookie_profile(
    cookie_profile: str | None,
    cookie_file: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    if cookie_file:
        source = _resolve_workspace_file(cookie_file)
    elif cookie_profile:
        source = _resolve_workspace_file(f"browser-cookies/{safe_filename(cookie_profile)}.json")
    else:
        return [], None

    if not source.exists():
        raise FileNotFoundError(f"Cookie file not found: {source}")

    raw = json.loads(source.read_text(encoding="utf-8"))
    cookies = raw.get("cookies") if isinstance(raw, dict) else raw
    if not isinstance(cookies, list):
        raise ValueError("Cookie file must contain a JSON array or an object with a 'cookies' array")
    return cookies, _relative_to_workspace(source)


def _summarize_hits(query: str, raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        source_hits = raw.get("web", [])
    elif isinstance(raw, list):
        source_hits = raw
    else:
        source_hits = []

    hits: list[dict[str, Any]] = []
    for index, item in enumerate(source_hits, start=1):
        if not isinstance(item, dict):
            continue
        hits.append(
            {
                "title": item.get("title") or item.get("metadata", {}).get("title") or "",
                "url": item.get("url") or item.get("sourceURL") or "",
                "snippet": item.get("description") or item.get("snippet") or "",
                "rank": index,
                "query": query,
            }
        )
    return hits


def _extract_page_content(scrape_payload: dict[str, Any]) -> tuple[str, list[str]]:
    ordered_keys = ["markdown", "summary", "html", "rawHtml"]
    available_keys = [key for key in ordered_keys if scrape_payload.get(key)]
    for key in ordered_keys:
        value = scrape_payload.get(key)
        if isinstance(value, str) and value.strip():
            return value, available_keys
    return "", available_keys


def _browser_budget(budget: dict[str, Any] | None) -> dict[str, int]:
    budget = budget or {}
    return {
        "max_steps": int(budget.get("max_steps", DEFAULT_BROWSER_STEPS)),
        "max_failures": int(budget.get("max_failures", DEFAULT_BROWSER_FAILURES)),
        "max_duration_ms": int(budget.get("max_duration_ms", DEFAULT_BROWSER_TIMEOUT_MS)),
    }


def _storage_state_from_cookies(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cookies": cookies,
        "origins": [],
    }


def _normalize_browser_command(command: str) -> str:
    normalized = command.strip()
    if normalized.startswith("agent-browser "):
        normalized = normalized[len("agent-browser ") :].strip()
    return normalized


def _build_browser_code(
    start_url: str,
    commands: list[str],
    storage_state: dict[str, Any] | None,
) -> str:
    normalized_commands = [_normalize_browser_command(command) for command in commands if command.strip()]
    command_specs = [
        {
            "label": f"command_{index:02d}",
            "command": f"agent-browser {command}",
        }
        for index, command in enumerate(normalized_commands, start=1)
    ]
    storage_state_json = json.dumps(storage_state or {}, ensure_ascii=False, indent=2)
    quoted_start_url = shlex.quote(start_url)
    command_spec_json = json.dumps(command_specs, ensure_ascii=False)

    lines = [
        "set -euo pipefail",
        'WORKDIR="$(mktemp -d)"',
        'export WORKDIR',
        'mkdir -p "$WORKDIR/logs" "$WORKDIR/snapshots"',
        'FAILED_LABEL=""',
        'FAILED_COMMAND=""',
        'LAST_SNAPSHOT=""',
        "",
        "run_and_capture() {",
        '  local label="$1"',
        '  local command="$2"',
        '  local stdout_file="$WORKDIR/logs/${label}.stdout"',
        '  local stderr_file="$WORKDIR/logs/${label}.stderr"',
        "  set +e",
        '  bash -lc "$command" >"$stdout_file" 2>"$stderr_file"',
        "  local exit_code=$?",
        "  set -e",
        '  printf "%s" "$exit_code" >"$WORKDIR/logs/${label}.exit"',
        '  if [ "$exit_code" -ne 0 ]; then',
        '    FAILED_LABEL="$label"',
        '    FAILED_COMMAND="$command"',
        '    return "$exit_code"',
        "  fi",
        "}",
        "",
        "take_snapshot() {",
        '  local label="$1"',
        '  local snapshot_file="$WORKDIR/snapshots/${label}.json"',
        "  set +e",
        '  agent-browser snapshot -i --json >"$snapshot_file" 2>"$WORKDIR/logs/${label}.snapshot.stderr"',
        "  local exit_code=$?",
        "  set -e",
        '  printf "%s" "$exit_code" >"$WORKDIR/logs/${label}.snapshot.exit"',
        '  if [ "$exit_code" -eq 0 ]; then',
        '    LAST_SNAPSHOT="$snapshot_file"',
        "  else",
        '    FAILED_LABEL="${label}.snapshot"',
        '    FAILED_COMMAND="agent-browser snapshot -i --json"',
        '    return "$exit_code"',
        "  fi",
        "}",
        "",
        "cat <<'JSON' > \"$WORKDIR/auth-state.json\"",
        storage_state_json,
        "JSON",
        "",
        "if [ -s \"$WORKDIR/auth-state.json\" ] && [ \"$(cat \"$WORKDIR/auth-state.json\")\" != \"{}\" ]; then",
        '  if ! run_and_capture "state_load" \'agent-browser state load "$WORKDIR/auth-state.json"\'; then',
        "    :",
        "  fi",
        "fi",
        "",
        'if [ -z "$FAILED_LABEL" ]; then',
        f'  if ! run_and_capture "open" "agent-browser open {quoted_start_url}"; then',
        "    :",
        "  fi",
        "fi",
        'if [ -z "$FAILED_LABEL" ]; then',
        '  if ! take_snapshot "open"; then',
        "    :",
        "  fi",
        "fi",
    ]

    for spec in command_specs:
        quoted_command = shlex.quote(spec["command"])
        lines.extend(
            [
                'if [ -z "$FAILED_LABEL" ]; then',
                f'  if ! run_and_capture "{spec["label"]}" {quoted_command}; then',
                "    :",
                "  fi",
                "fi",
                'if [ -z "$FAILED_LABEL" ]; then',
                f'  if ! take_snapshot "{spec["label"]}"; then',
                "    :",
                "  fi",
                "fi",
            ]
        )

    lines.extend(
        [
            "",
            "set +e",
            'agent-browser get url --json >"$WORKDIR/logs/final_url.stdout" 2>"$WORKDIR/logs/final_url.stderr"',
            'printf "%s" "$?" >"$WORKDIR/logs/final_url.exit"',
            'agent-browser get title --json >"$WORKDIR/logs/final_title.stdout" 2>"$WORKDIR/logs/final_title.stderr"',
            'printf "%s" "$?" >"$WORKDIR/logs/final_title.exit"',
            'agent-browser screenshot "$WORKDIR/final.png" >"$WORKDIR/logs/final_screenshot.stdout" 2>"$WORKDIR/logs/final_screenshot.stderr"',
            'printf "%s" "$?" >"$WORKDIR/logs/final_screenshot.exit"',
            "set -e",
            "",
            "node <<'NODE'",
            "const fs = require('fs');",
            "const path = process.env.WORKDIR;",
            f"const commandSpecs = {command_spec_json};",
            "function readText(filePath) {",
            "  try { return fs.readFileSync(filePath, 'utf8'); } catch { return ''; }",
            "}",
            "function readExit(filePath) {",
            "  const value = readText(filePath).trim();",
            "  return value ? Number(value) : null;",
            "}",
            "function readJson(filePath) {",
            "  const raw = readText(filePath).trim();",
            "  if (!raw) return null;",
            "  try { return JSON.parse(raw); } catch { return raw; }",
            "}",
            "const commandLog = commandSpecs.map((spec) => ({",
            "  label: spec.label,",
            "  command: spec.command.replace(/^agent-browser\\s+/, ''),",
            "  exitCode: readExit(`${path}/logs/${spec.label}.exit`),",
            "  stdout: readText(`${path}/logs/${spec.label}.stdout`).trim(),",
            "  stderr: readText(`${path}/logs/${spec.label}.stderr`).trim(),",
            "}));",
            "const finalUrlPayload = readJson(`${path}/logs/final_url.stdout`);",
            "const finalTitlePayload = readJson(`${path}/logs/final_title.stdout`);",
            "const finalSnapshot = process.env.LAST_SNAPSHOT ? readJson(process.env.LAST_SNAPSHOT) : null;",
            "let screenshot = null;",
            "try {",
            "  const data = fs.readFileSync(`${path}/final.png`);",
            "  screenshot = { data: data.toString('base64') };",
            "} catch {}",
            "const result = {",
            "  url: finalUrlPayload?.data ?? finalUrlPayload?.url ?? finalUrlPayload ?? null,",
            "  title: finalTitlePayload?.data ?? finalTitlePayload?.title ?? finalTitlePayload ?? null,",
            "  commandLog,",
            "  finalSnapshot,",
            "  screenshot,",
            "  failedLabel: process.env.FAILED_LABEL || null,",
            "  failedCommand: process.env.FAILED_COMMAND || null,",
            "};",
            "console.log(JSON.stringify(result));",
            "NODE",
        ]
    )

    return "\n".join(lines)


def _browser_execute_error(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    stderr = payload.get("stderr")
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip()
    exit_code = payload.get("exitCode", payload.get("exit_code"))
    if isinstance(exit_code, int) and exit_code != 0:
        return f"Browser execute failed with exit code {exit_code}"
    return None


def _parse_browser_result(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("result")
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str):
        candidate = candidate.strip()
        if candidate:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    stdout = payload.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        last_line = stdout.strip().splitlines()[-1]
        try:
            parsed = json.loads(last_line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


async def _report_firecrawl_event(payload: dict[str, Any]) -> None:
    if not os.environ.get("GATEWAY_JWT_TOKEN"):
        return
    try:
        async with gateway_client(timeout=15.0) as client:
            await client.post("/tools/firecrawl/report", json=payload)
    except Exception:
        return


class FirecrawlSearchTool(Tool):
    """Search the web via Firecrawl and persist normalized results to workspace artifacts."""

    @property
    def name(self) -> str:
        return "firecrawl_search"

    @property
    def description(self) -> str:
        return (
            "Search the web with Firecrawl. Use this first for research tasks, then "
            "follow promising hits with firecrawl_scrape or firecrawl_browser using the same task_id."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query", "minLength": 2},
                "task_id": {"type": "string", "description": "Stable ID to group all Firecrawl artifacts for one user task"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": DEFAULT_SEARCH_LIMIT},
                "location": {"type": "string", "description": "Optional geo-targeted location string"},
                "country": {"type": "string", "description": "ISO country code", "default": "US"},
                "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 120000, "default": 60000},
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        task_id: str = "",
        limit: int = DEFAULT_SEARCH_LIMIT,
        location: str = "",
        country: str = "US",
        timeout_ms: int = 60_000,
        **kwargs: Any,
    ) -> str:
        started_at = time.perf_counter()
        task_id = task_id or f"search-{uuid.uuid4().hex[:10]}"
        artifact_dir = _artifact_root(task_id)
        if not _gateway_ready():
            return _gateway_not_configured(task_id, "search_gateway_not_configured")

        try:
            response = await _firecrawl_gateway_request(
                "/tools/firecrawl/search",
                {
                    "query": query,
                    "limit": limit,
                    "location": location or None,
                    "country": country,
                    "timeout_ms": timeout_ms,
                },
                # Leave transport headroom beyond Firecrawl's own timeout so
                # the gateway can return its terminal response to NanoBot.
                timeout_s=max(15.0, timeout_ms / 1000 + 10.0),
            )
            hits = _summarize_hits(query, response.get("data", {}))
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            artifact_path = artifact_dir / "search.json"
            payload = {
                "task_id": task_id,
                "query": query,
                "metrics": {
                    "duration_ms": duration_ms,
                    "request_count": 1,
                    "returned_count": len(hits),
                },
                "hits": hits,
                "raw": response,
            }
            _json_write(artifact_path, payload)
            await _report_firecrawl_event(
                {
                    "stage": "search",
                    "status": "success",
                    "task_id": task_id,
                    "summary": {"query": query},
                    "metrics": {
                        "request_count": 1,
                        "returned_count": len(hits),
                        "duration_ms": duration_ms,
                    },
                    "evidence_refs": [_relative_to_workspace(artifact_path)],
                }
            )
            return json.dumps(
                {
                    "success": True,
                    "task_id": task_id,
                    "hits": hits,
                    "count": len(hits),
                    "artifact_path": _relative_to_workspace(artifact_path),
                    "metrics": payload["metrics"],
                },
                ensure_ascii=False,
            )
        except (httpx.HTTPError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            await _report_firecrawl_event(
                {
                    "stage": "search",
                    "status": "error",
                    "task_id": task_id,
                    "summary": {"query": query},
                    "metrics": {"duration_ms": duration_ms},
                    "evidence_refs": [],
                    "error": str(exc),
                    "incomplete_reason": "search_failed",
                }
            )
            return json.dumps(
                {
                    "success": False,
                    "task_id": task_id,
                    "error": str(exc),
                    "incomplete_reason": "search_failed",
                },
                ensure_ascii=False,
            )


class FirecrawlScrapeTool(Tool):
    """Fetch a page with Firecrawl scrape before escalating to browser automation."""

    @property
    def name(self) -> str:
        return "firecrawl_scrape"

    @property
    def description(self) -> str:
        return (
            "Scrape a known URL with Firecrawl. Use this before firecrawl_browser whenever static extraction may be enough."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to retrieve"},
                "task_id": {"type": "string", "description": "Stable task grouping ID"},
                "formats": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Requested Firecrawl formats, e.g. ['markdown', 'html']",
                },
                "only_main_content": {"type": "boolean", "description": "Prefer main content only", "default": True},
                "wait_for_ms": {"type": "integer", "minimum": 0, "maximum": 120000, "default": 0},
                "timeout_ms": {"type": "integer", "minimum": 1000, "maximum": 120000, "default": 30000},
                "actions": {
                    "type": "array",
                    "description": "Optional Firecrawl scrape actions for light interaction before extraction",
                    "items": {"type": "object"},
                },
            },
            "required": ["url"],
        }

    async def execute(
        self,
        url: str,
        task_id: str = "",
        formats: list[str] | None = None,
        only_main_content: bool = True,
        wait_for_ms: int = 0,
        timeout_ms: int = 30_000,
        actions: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> str:
        started_at = time.perf_counter()
        task_id = task_id or f"scrape-{uuid.uuid4().hex[:10]}"
        artifact_dir = ensure_dir(_artifact_root(task_id) / "scrape")
        slug = safe_filename(Path(url).name or url.replace("://", "_"))[:80] or "page"
        artifact_path = artifact_dir / f"{slug}.json"
        if not _gateway_ready():
            return _gateway_not_configured(task_id, "scrape_gateway_not_configured")

        try:
            response = await _firecrawl_gateway_request(
                "/tools/firecrawl/scrape",
                {
                    "url": url,
                    "formats": formats or ["markdown"],
                    "only_main_content": only_main_content,
                    "wait_for_ms": wait_for_ms,
                    "timeout_ms": timeout_ms,
                    "actions": actions,
                },
                timeout_s=max(15.0, timeout_ms / 1000 + 10.0),
            )
            data = response.get("data", {})
            content, available_formats = _extract_page_content(data if isinstance(data, dict) else {})
            title = ""
            if isinstance(data, dict):
                title = data.get("metadata", {}).get("title") or data.get("title") or ""
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            incomplete_reason = None if content else "missing_content"
            completeness = "complete" if content else "partial"
            normalized = {
                "url": url,
                "mode": "scrape",
                "title": title,
                "content": content,
                "available_formats": available_formats,
                "evidence_refs": [
                    {"kind": "url", "value": url},
                    {"kind": "artifact", "value": _relative_to_workspace(artifact_path)},
                ],
                "missing_fields": [] if content else ["content"],
                "incomplete_reason": incomplete_reason,
            }
            _json_write(
                artifact_path,
                {
                    "task_id": task_id,
                    "page": normalized,
                    "metrics": {"duration_ms": duration_ms},
                    "raw": response,
                },
            )
            await _report_firecrawl_event(
                {
                    "stage": "scrape",
                    "status": "success" if content else "partial",
                    "task_id": task_id,
                    "summary": {"url": url},
                    "metrics": {"duration_ms": duration_ms, "page_count": 1},
                    "evidence_refs": [_relative_to_workspace(artifact_path)],
                    "incomplete_reason": incomplete_reason,
                }
            )
            return json.dumps(
                {
                    "success": True,
                    "task_id": task_id,
                    "page": normalized,
                    "completeness": completeness,
                    "incomplete_reason": incomplete_reason,
                    "artifact_path": _relative_to_workspace(artifact_path),
                    "metrics": {"duration_ms": duration_ms},
                },
                ensure_ascii=False,
            )
        except (httpx.HTTPError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            await _report_firecrawl_event(
                {
                    "stage": "scrape",
                    "status": "error",
                    "task_id": task_id,
                    "summary": {"url": url},
                    "metrics": {"duration_ms": duration_ms},
                    "evidence_refs": [],
                    "error": str(exc),
                    "incomplete_reason": "scrape_failed",
                }
            )
            return json.dumps(
                {
                    "success": False,
                    "task_id": task_id,
                    "error": str(exc),
                    "incomplete_reason": "scrape_failed",
                },
                ensure_ascii=False,
            )


class FirecrawlBrowserTool(Tool):
    """Run a multi-step browser workflow in a disposable Firecrawl browser session."""

    @property
    def name(self) -> str:
        return "firecrawl_browser"

    @property
    def description(self) -> str:
        return (
            "Run Firecrawl browser automation in bash mode with the preinstalled agent-browser CLI. "
            "Use command fragments such as `find role button click --name \"Continue\"` or "
            "`find label \"Email\" fill \"user@example.com\"` after scrape is insufficient."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Short task label for artifacts and logs", "minLength": 3},
                "task_id": {"type": "string", "description": "Stable task grouping ID reused across Firecrawl calls"},
                "start_url": {"type": "string", "description": "Initial URL to open"},
                "commands": {
                    "type": "array",
                    "description": (
                        "Ordered agent-browser command fragments run after the tool opens start_url. "
                        "Do not include the `agent-browser` prefix or the initial `open` command."
                    ),
                    "items": {"type": "string"},
                },
                "intent": {
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string"},
                        "required_output": {"type": "string"},
                        "allowed_side_effects": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "needs_auth": {"type": "boolean"},
                        "done_when": {"type": "string"},
                        "risk_level": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    "required": [
                        "objective",
                        "required_output",
                        "allowed_side_effects",
                        "needs_auth",
                        "done_when",
                    ],
                },
                "budget": {
                    "type": "object",
                    "properties": {
                        "max_steps": {"type": "integer"},
                        "max_failures": {"type": "integer"},
                        "max_duration_ms": {"type": "integer"},
                    },
                },
                "ttl_seconds": {"type": "integer", "minimum": 60, "maximum": 1800, "default": 600},
                "cookie_profile": {"type": "string", "description": "Workspace cookie profile name from browser-cookies/<name>.json"},
                "cookie_file": {"type": "string", "description": "Workspace-relative path to a cookie JSON file"},
            },
            "required": ["task", "start_url", "intent"],
        }

    async def execute(
        self,
        task: str,
        start_url: str,
        intent: dict[str, Any],
        commands: list[str] | None = None,
        task_id: str = "",
        budget: dict[str, Any] | None = None,
        ttl_seconds: int = 600,
        cookie_profile: str = "",
        cookie_file: str = "",
        **kwargs: Any,
    ) -> str:
        started_at = time.perf_counter()
        task_id = task_id or f"browser-{uuid.uuid4().hex[:10]}"
        artifact_dir = ensure_dir(_artifact_root(task_id) / "browser")
        screenshots_dir = ensure_dir(artifact_dir / "screenshots")
        snapshots_dir = ensure_dir(artifact_dir / "snapshots")
        limits = _browser_budget(budget)
        session_id = ""
        commands = commands or []
        if not _gateway_ready():
            return _gateway_not_configured(task_id, "browser_gateway_not_configured")

        if kwargs.get("steps"):
            return json.dumps(
                {
                    "success": False,
                    "task_id": task_id,
                    "error": "Legacy browser `steps` are no longer supported. Use bash `commands` instead.",
                    "incomplete_reason": "browser_legacy_steps_not_supported",
                },
                ensure_ascii=False,
            )

        if len(commands) > limits["max_steps"]:
            return json.dumps(
                {
                    "success": False,
                    "task_id": task_id,
                    "error": f"Browser command budget exceeded: {len(commands)} > {limits['max_steps']}",
                    "incomplete_reason": "step_budget_exceeded",
                },
                ensure_ascii=False,
            )

        if bool(intent.get("needs_auth")) and not (cookie_profile or cookie_file):
            await _report_firecrawl_event(
                {
                    "stage": "browser",
                    "status": "error",
                    "task_id": task_id,
                    "summary": {"task": task, "start_url": start_url},
                    "metrics": {"step_count": len(commands)},
                    "evidence_refs": [],
                    "error": "auth_required",
                    "incomplete_reason": "cookie_required",
                }
            )
            return json.dumps(
                {
                    "success": False,
                    "status": "auth_required",
                    "task_id": task_id,
                    "message": "Authenticated browsing requires cookie_profile or cookie_file from workspace/browser-cookies/.",
                    "incomplete_reason": "cookie_required",
                },
                ensure_ascii=False,
            )

        cookies: list[dict[str, Any]] = []
        cookie_source: str | None = None
        if cookie_profile or cookie_file:
            try:
                cookies, cookie_source = _load_cookie_profile(cookie_profile or None, cookie_file or None)
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
                await _report_firecrawl_event(
                    {
                        "stage": "browser",
                        "status": "error",
                        "task_id": task_id,
                        "summary": {"task": task, "start_url": start_url},
                        "metrics": {"step_count": len(commands)},
                        "evidence_refs": [],
                        "incomplete_reason": "cookie_invalid",
                        "error": str(exc),
                    }
                )
                return json.dumps(
                    {
                        "success": False,
                        "status": "auth_required",
                        "task_id": task_id,
                        "message": str(exc),
                        "incomplete_reason": "cookie_invalid",
                    },
                    ensure_ascii=False,
                )

        try:
            storage_state = _storage_state_from_cookies(cookies) if cookies else None
            code = _build_browser_code(
                start_url=start_url,
                commands=commands,
                storage_state=storage_state,
            )
            browser_response = await _firecrawl_gateway_request(
                "/tools/firecrawl/browser",
                {
                    "code": code,
                    "ttl_seconds": ttl_seconds,
                    "language": "bash",
                    "timeout_seconds": max(15, limits["max_duration_ms"] // 1000),
                },
                timeout_s=max(15.0, limits["max_duration_ms"] / 1000 + 15),
            )
            created = browser_response.get("raw_session", {})
            executed = browser_response.get("raw_execute", {})
            session_id = str(created.get("id", ""))
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            stdout_text = executed.get("stdout") if isinstance(executed.get("stdout"), str) else ""
            stderr_text = executed.get("stderr") if isinstance(executed.get("stderr"), str) else ""
            stdout_path = artifact_dir / "stdout.log"
            stderr_path = artifact_dir / "stderr.log"
            stdout_path.write_text(stdout_text, encoding="utf-8")
            stderr_path.write_text(stderr_text, encoding="utf-8")

            execute_error = _browser_execute_error(executed)
            result = _parse_browser_result(executed)
            screenshot_refs: list[dict[str, str]] = []
            evidence_refs = [
                _relative_to_workspace(stdout_path),
                _relative_to_workspace(stderr_path),
            ]

            final_snapshot = result.get("finalSnapshot") if isinstance(result.get("finalSnapshot"), dict) else None
            snapshot_path: Path | None = None
            if final_snapshot is not None:
                snapshot_path = snapshots_dir / "final.json"
                _json_write(snapshot_path, final_snapshot)
                evidence_refs.append(_relative_to_workspace(snapshot_path))

            screenshot = result.get("screenshot") if isinstance(result.get("screenshot"), dict) else None
            if screenshot:
                data = screenshot.get("data")
                if isinstance(data, str) and data:
                    screenshot_path = screenshots_dir / "final.png"
                    screenshot_path.write_bytes(base64.b64decode(data))
                    screenshot_refs.append({"field": "final", "path": _relative_to_workspace(screenshot_path)})
                    evidence_refs.append(_relative_to_workspace(screenshot_path))

            command_log = result.get("commandLog") if isinstance(result.get("commandLog"), list) else []
            command_failures = 0
            for item in command_log:
                if not isinstance(item, dict):
                    continue
                exit_code = item.get("exitCode")
                if isinstance(exit_code, int) and exit_code != 0:
                    command_failures += 1

            snapshot_data = final_snapshot.get("data") if isinstance(final_snapshot, dict) and isinstance(final_snapshot.get("data"), dict) else {}
            snapshot_text = snapshot_data.get("snapshot") if isinstance(snapshot_data.get("snapshot"), str) else ""
            refs = snapshot_data.get("refs") if isinstance(snapshot_data.get("refs"), dict) else {}
            has_evidence = bool(snapshot_text.strip() or screenshot_refs or result.get("title"))
            incomplete_reason = None
            completeness = "complete" if has_evidence else "partial"
            status = "success" if completeness == "complete" else "partial"

            if execute_error:
                incomplete_reason = "browser_execution_failed"
                status = "error"
                completeness = "partial"
            elif command_failures or result.get("failedLabel"):
                incomplete_reason = "browser_command_failed"
                status = "partial"
                completeness = "partial"
            elif duration_ms >= limits["max_duration_ms"]:
                incomplete_reason = "browser_time_budget_exceeded"
                status = "partial"
                completeness = "partial"
            elif not has_evidence:
                incomplete_reason = "browser_no_evidence"

            evidence_summary = {
                "snapshot_excerpt": snapshot_text[:500] if snapshot_text else "",
                "ref_count": len(refs),
                "command_count": len(command_log),
                "command_failure_count": command_failures,
            }

            normalized = {
                "task_id": task_id,
                "task": task,
                "session_id": session_id,
                "start_url": start_url,
                "final_url": result.get("url") or start_url,
                "title": result.get("title"),
                "command_log": command_log,
                "final_snapshot": final_snapshot,
                "screenshots": screenshot_refs,
                "metrics": {
                    "duration_ms": duration_ms,
                    "step_count": len(commands),
                    "failure_count": command_failures,
                    "credits": None,
                    "credits_unavailable": True,
                },
                "intent": {
                    **intent,
                    "cookie_source": cookie_source,
                },
                "evidence_summary": evidence_summary,
                "completeness": completeness,
                "incomplete_reason": incomplete_reason,
            }
            run_path = artifact_dir / "run.json"
            _json_write(
                run_path,
                {
                    "normalized": normalized,
                    "raw_session": created,
                    "raw_execute": executed,
                },
            )
            evidence_refs.insert(0, _relative_to_workspace(run_path))

            if execute_error:
                await _report_firecrawl_event(
                    {
                        "stage": "browser",
                        "status": "error",
                        "task_id": task_id,
                        "summary": {
                            "task": task,
                            "start_url": start_url,
                            "session_id": session_id,
                        },
                        "metrics": {
                            "step_count": len(commands),
                            "failure_count": command_failures,
                            "duration_ms": duration_ms,
                        },
                        "evidence_refs": evidence_refs,
                        "error": execute_error,
                        "incomplete_reason": incomplete_reason,
                    }
                )
                return json.dumps(
                    {
                        "success": False,
                        "task_id": task_id,
                        "error": execute_error,
                        "incomplete_reason": incomplete_reason,
                        "artifact_path": _relative_to_workspace(run_path),
                    },
                    ensure_ascii=False,
                )

            await _report_firecrawl_event(
                {
                    "stage": "browser",
                    "status": status,
                    "task_id": task_id,
                    "summary": {
                        "task": task,
                        "start_url": start_url,
                        "session_id": session_id,
                    },
                    "metrics": {
                        "step_count": len(commands),
                        "failure_count": command_failures,
                        "duration_ms": duration_ms,
                    },
                    "evidence_refs": evidence_refs,
                    "incomplete_reason": incomplete_reason,
                }
            )
            return json.dumps(
                {
                    "success": True,
                    "task_id": task_id,
                    "status": status,
                    "result": normalized,
                    "artifact_path": _relative_to_workspace(run_path),
                },
                ensure_ascii=False,
            )
        except (httpx.HTTPError, ValueError) as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            await _report_firecrawl_event(
                {
                    "stage": "browser",
                    "status": "error",
                    "task_id": task_id,
                    "summary": {
                        "task": task,
                        "start_url": start_url,
                        "session_id": session_id or None,
                    },
                    "metrics": {
                        "step_count": len(commands),
                        "duration_ms": duration_ms,
                    },
                    "evidence_refs": [],
                    "error": str(exc),
                    "incomplete_reason": "browser_failed",
                }
            )
            return json.dumps(
                {
                    "success": False,
                    "task_id": task_id,
                    "error": str(exc),
                    "incomplete_reason": "browser_failed",
                },
                ensure_ascii=False,
            )
