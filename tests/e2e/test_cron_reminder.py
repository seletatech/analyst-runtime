"""E2E: cron reminder creation and firing.

Scenario: user asks to be reminded in a few seconds.
Expected:
  1. Agent calls the cron tool
  2. .analyst-runtime/cron/jobs.json is written to workspace
  3. Cron fires within the interval → sessions/cron_*.jsonl appears

Real user basis: Qian's entire power-user pattern is cron-based scheduled tasks
(daily email digest). If cron creation or firing breaks, she gets nothing.
"""
from __future__ import annotations

import json
import uuid

import pytest

from .conftest import StagingSandbox


@pytest.mark.e2e
@pytest.mark.timeout(120)
def test_reminder_creates_cron_job(sb: StagingSandbox) -> None:
    """Asking for a reminder must invoke the cron tool and write jobs.json."""
    session_id = f"e2e-cron-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)
    # Clear old cron jobs from prior runs to prevent contamination
    sb.cleanup_cron_jobs()

    sb.send("10秒后提醒我喝水，就回复我'时间到了，喝水！'", session_id)
    sb.wait_for_final_response(session_id, timeout=90)

    tool_calls = sb.read_tool_calls(session_id)
    cron_calls = [t for t in tool_calls if t["tool_name"] == "cron"]
    assert cron_calls, (
        f"cron tool was not called. All tool calls: {[t['tool_name'] for t in tool_calls]}. "
        f"Container logs:\n{sb.container_logs(tail=30)}"
    )

    # jobs.json must exist at the correct path
    jobs_file = sb.workspace / ".analyst-runtime" / "cron" / "jobs.json"
    assert jobs_file.exists(), (
        f".analyst-runtime/cron/jobs.json not created after cron tool call. "
        f"Workspace contents: {list((sb.workspace / '.analyst-runtime').iterdir()) if (sb.workspace / '.analyst-runtime').exists() else 'no .analyst-runtime dir'}"
    )

    jobs_data = json.loads(jobs_file.read_text())
    assert jobs_data.get("jobs"), "jobs.json exists but has no jobs"


@pytest.mark.e2e
@pytest.mark.timeout(120)
def test_reminder_fires_and_agent_runs(sb: StagingSandbox) -> None:
    """After cron job is created, it should fire and produce a cron session JSONL."""
    session_id = f"e2e-cronfire-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)
    # NOTE: Do NOT call cleanup_cron_jobs() here — the 15-second sleep would push
    # the message delivery past the reminder's "N seconds from now" target time,
    # causing the cron job to be added with a past-due timestamp and never fire.
    # The test_reminder_creates_cron_job test runs first and does its own cleanup.

    # Snapshot existing cron sessions before the test (can't delete — container owns them)
    sessions_dir = sb.workspace / "sessions"
    existing_cron = set()
    if sessions_dir.exists():
        existing_cron = {f.name for f in sessions_dir.glob("cron_*.jsonl")}

    # Use 30s so the reminder survives any queue processing delay
    sb.send("30秒后提醒我喝水", session_id)
    sb.wait_for_final_response(session_id, timeout=90)

    # Verify cron tool was called
    tool_calls = sb.read_tool_calls(session_id)
    assert any(t["tool_name"] == "cron" for t in tool_calls), (
        f"cron tool not called: {[t['tool_name'] for t in tool_calls]}"
    )

    # Wait for a NEW cron session to appear (30s interval + 30s buffer)
    cron_session = sb.wait_for_new_cron_session(existing_cron, timeout=60)
    assert cron_session is not None, (
        "Cron job did not fire within 60s — the scheduled notification was never sent. "
        f"Container logs:\n{sb.container_logs(tail=50)}"
    )

    # Cron session must have a final_response (agent completed the turn)
    events = []
    for line in cron_session.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    final_responses = [e for e in events if e.get("type") == "final_response"]
    assert final_responses, (
        f"Cron session {cron_session.name} exists but has no final_response event. "
        f"Events: {[e.get('type') for e in events]}"
    )
