"""Staging E2E test fixtures.

Requires these env vars (set by scripts/staging/run-e2e.sh):
    E2E_SANDBOX_ID           — sandbox UUID
    E2E_SANDBOX_INTERNAL_SECRET — bearer token for /internal/* endpoints
    E2E_API_URL              — http://localhost:8000
    E2E_WORKSPACE_PATH       — host path to sandbox workspace
    E2E_CONTAINER_NAME       — analyst_runtime-{sandbox_id[:12]}

After each test, call sb.save_result(session_id, test_name, message) to write
a markdown summary to tests/e2e/results/YYYY-MM-DD/<test_name>.md for human review.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest


class StagingSandbox:
    def __init__(self) -> None:
        self.sandbox_id = os.environ["E2E_SANDBOX_ID"]
        self.secret = os.environ["E2E_SANDBOX_INTERNAL_SECRET"]
        self.api_url = os.environ.get("E2E_API_URL", "http://localhost:8000")
        self.workspace = Path(os.environ["E2E_WORKSPACE_PATH"])
        self.container = os.environ.get("E2E_CONTAINER_NAME", f"analyst_runtime-{self.sandbox_id[:12]}")

    def send(self, content: str, session_id: str, timeout: float = 5.0) -> None:
        """Inject a message fire-and-forget. Does not wait for response."""
        import httpx as _httpx
        try:
            _httpx.post(
                f"{self.api_url}/internal/sandbox/{self.sandbox_id}/send",
                headers={"Authorization": f"Bearer {self.secret}"},
                json={"content": content, "session_id": session_id},
                timeout=timeout,
            )
        except Exception as exc:
            # Log but don't fail — fire-and-forget; timeout is expected, connection
            # errors should surface as TimeoutError in wait_for_final_response
            import logging as _logging
            _logging.getLogger(__name__).warning("E2E send failed: %s", exc)

    def _session_path(self, session_id: str) -> Path:
        """Return path to session JSONL file for a web-channel session."""
        safe_key = f"web_{session_id}".replace(":", "_")
        return self.workspace / "sessions" / f"{safe_key}.jsonl"

    def wait_for_final_response(self, session_id: str, timeout: float = 120.0) -> str:
        """Poll session JSONL until a final_response event appears. Returns content.

        Returns the first non-empty final_response content, or empty string if content is null.
        For multi-turn responses, use wait_for_last_final_response instead.
        """
        path = self._session_path(session_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                for line in path.read_text().splitlines():
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("type") == "final_response":
                        # Use `or ""` to handle null content values
                        return ev.get("content") or ""
            time.sleep(1.0)
        raise TimeoutError(
            f"No final_response in session {session_id} after {timeout}s. "
            f"Session file: {path} exists={path.exists()}"
        )

    def wait_for_last_final_response(
        self,
        session_id: str,
        timeout: float = 120.0,
        idle_seconds: float = 10.0,
    ) -> str:
        """Wait until the session has been idle for idle_seconds, then return last final_response.

        Use this for multi-turn agent tasks where the agent sends intermediate progress
        messages (each creating a final_response event) before finishing.
        Returns the last non-empty final_response seen, or "" if none found.
        """
        path = self._session_path(session_id)
        deadline = time.monotonic() + timeout
        last_size = -1
        last_change_time = time.monotonic()
        last_final: str = ""

        while time.monotonic() < deadline:
            if path.exists():
                current_size = path.stat().st_size
                if current_size != last_size:
                    last_size = current_size
                    last_change_time = time.monotonic()
                    # Re-read all final_response events, keep the last one
                    for line in path.read_text().splitlines():
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if ev.get("type") == "final_response":
                            last_final = ev.get("content") or ""
                elif last_size > 0 and (time.monotonic() - last_change_time) >= idle_seconds:
                    # File hasn't changed for idle_seconds — agent is done
                    return last_final
            time.sleep(1.0)

        # Timeout — return whatever we have
        if last_final:
            return last_final
        raise TimeoutError(
            f"No stable final_response in session {session_id} after {timeout}s. "
            f"Session file: {path} exists={path.exists()}"
        )

    def read_tool_calls(self, session_id: str) -> list[dict[str, Any]]:
        """Return ordered list of tool calls from session JSONL.

        Each entry: {"tool_name": str, "tool_input": dict, "content": str}
        """
        path = self._session_path(session_id)
        if not path.exists():
            return []
        calls = []
        for line in path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "tool_result":
                calls.append({
                    "tool_name": ev.get("tool_name", ""),
                    "tool_input": ev.get("tool_input", {}),
                    "content": ev.get("content", ""),
                })
        return calls

    def wait_for_cron_session(self, timeout: float = 30.0) -> Path | None:
        """Wait for a cron-triggered session JSONL to appear. Returns path or None."""
        return self.wait_for_new_cron_session(set(), timeout=timeout)

    def wait_for_new_cron_session(self, existing: set[str], timeout: float = 30.0) -> Path | None:
        """Wait for a cron session JSONL that wasn't in `existing`. Returns path or None."""
        sessions_dir = self.workspace / "sessions"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sessions_dir.exists():
                new = [
                    f for f in sessions_dir.glob("cron_*.jsonl")
                    if f.name not in existing
                ]
                if new:
                    return new[0]
            time.sleep(1.0)
        return None

    def container_logs(self, tail: int = 100) -> str:
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(tail), self.container],
                capture_output=True, text=True, timeout=10,
            )
            return result.stdout + result.stderr
        except Exception:
            return ""

    def cleanup_session(self, session_id: str) -> None:
        """Remove session JSONL before test to prevent stale history."""
        path = self._session_path(session_id)
        if path.exists():
            path.unlink()

    def cleanup_cron_jobs(self) -> None:
        """Clear cron jobs by overwriting jobs.json with an empty store.

        The CronService monitors file mtime and reloads when changed — writing
        {"jobs":[]} causes it to reload on the next timer tick and stop scheduling
        new firings. Already-queued inbound messages from prior firings will still
        be processed, but no new firings will be added to the queue.
        """
        # Write the empty store in the same format CronService expects
        empty_store = '{"version":1,"jobs":[]}'
        result = subprocess.run(
            ["docker", "exec", self.container, "sh", "-c",
             f"mkdir -p /workspace/.analyst-runtime/cron && echo '{empty_store}' > /workspace/.analyst-runtime/cron/jobs.json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            # Fallback: try host-side write
            jobs_file = self.workspace / ".analyst-runtime" / "cron" / "jobs.json"
            jobs_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                jobs_file.write_text(empty_store, encoding="utf-8")
            except PermissionError:
                pass  # best effort
        # Wait for the CronService timer to tick and reload the empty file.
        # A 10-second reminder fires every ~10s; after mtime change the service
        # reloads on the next tick and stops scheduling. 15s covers one full cycle.
        time.sleep(15.0)

    def save_result(
        self,
        session_id: str,
        test_name: str,
        message: str,
        status: str = "UNKNOWN",
    ) -> Path:
        """Write a markdown summary of the session for human behavior review.

        Output: tests/e2e/results/YYYY-MM-DD/<test_name>.md
        Returns the path written.
        """
        results_dir = Path(__file__).parent / "results" / datetime.date.today().isoformat()
        results_dir.mkdir(parents=True, exist_ok=True)
        out = results_dir / f"{test_name}.md"

        path = self._session_path(session_id)
        lines: list[str] = [
            f"# {test_name}",
            f"**Date:** {datetime.datetime.now().isoformat(timespec='seconds')}",
            f"**Session ID:** {session_id}",
            f"**Status:** {status}",
            f"",
            f"## Message",
            f"```",
            message,
            f"```",
            f"",
            f"## Tool Calls (in order)",
        ]

        if not path.exists():
            lines.append("_No session file found — agent may not have started_")
        else:
            tool_calls: list[dict[str, Any]] = []
            final_response = ""
            user_inputs: list[str] = []

            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = ev.get("type")
                if t == "user_input":
                    user_inputs.append(ev.get("content", "")[:200])
                elif t == "tool_result":
                    tool_calls.append({
                        "tool": ev.get("tool_name", ""),
                        "result": ev.get("content", "")[:200],
                    })
                elif t == "final_response":
                    final_response = ev.get("content", "")

            if tool_calls:
                for i, tc in enumerate(tool_calls, 1):
                    lines.append(f"{i}. `{tc['tool']}` → {tc['result'][:150]}")
            else:
                lines.append("_No tool calls recorded_")

            lines += [
                "",
                "## Final Response",
                "```",
                (final_response[:800] if final_response else "_no final_response event_"),
                "```",
            ]

        out.write_text("\n".join(lines), encoding="utf-8")
        return out


@pytest.fixture
def sb() -> StagingSandbox:
    required = ["E2E_SANDBOX_ID", "E2E_SANDBOX_INTERNAL_SECRET", "E2E_WORKSPACE_PATH"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        pytest.skip(f"Staging env vars not set: {missing}. Run via scripts/staging/run-e2e.sh")
    return StagingSandbox()
