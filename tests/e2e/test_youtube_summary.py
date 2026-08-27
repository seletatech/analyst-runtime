"""E2E: YouTube video summary — full chain completion.

Real user basis: log 2026-04-07 — agent stopped mid-task at iteration 6 with
finish_reason=stop, text-only "我找到了一些YouTube视频摘要工具的相关信息，但我还没找到这个
特定视频的内容。让我直接搜索这个视频ID相关的信息。" instead of completing the task.

Root cause: Kimi k2.5 emitted finish_reason=stop with text-only "let me do X"
announcement (no tool call) — loop exited prematurely. Fixed in loop.py by injecting
one continuation prompt when tools_used is non-empty and finish_reason=stop
with text but no tool calls.

This test monitors that the agent:
1. Actually completes the summary task (doesn't stop mid-chain)
2. Uses whatever tools it deems fit — exec, yt-dlp, youtube tool, firecrawl, etc.
   No tool restrictions. If the agent wants to install yt-dlp and use it, that's fine.

Results saved to tests/e2e/results/ for behavioral review.
"""
from __future__ import annotations

import uuid

import pytest

from .conftest import StagingSandbox

_YT_URL = "https://www.youtube.com/watch?v=CnZAx8AZTSo"
_TEST_NAME = "test_youtube_summary_completes_chain"


@pytest.mark.e2e
@pytest.mark.timeout(300)
def test_youtube_summary_completes_chain(sb: StagingSandbox) -> None:
    """Agent should complete a YouTube summary, not stop mid-chain with planning text."""
    session_id = f"e2e-ytsummary-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = f"请帮我总结这个YouTube视频的内容，用中文给我一个完整的摘要：{_YT_URL}"
    sb.send(msg, session_id)

    try:
        # Long idle window: agent may install tools (yt-dlp), run exec chains, etc.
        final = sb.wait_for_last_final_response(session_id, timeout=270, idle_seconds=15)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, _TEST_NAME, msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, _TEST_NAME, msg, status=status)

    # Agent must have used at least one tool — pure knowledge answer is not acceptable
    # for a current YouTube video with specific content.
    assert tool_calls, (
        f"Agent returned answer with no tool calls — should attempt to fetch video info. "
        f"Response: {final[:300]}"
    )

    # The fix regression: final response must NOT be a mid-task planning statement.
    # These phrases indicate the agent stopped before completing the task.
    incomplete_signals = [
        "让我继续",   # "let me continue"
        "让我直接",   # "let me directly"
        "让我搜索",   # "let me search"
        "let me search",
        "let me try",
        "i'll search",
        "i will search",
        "but i haven't",
        "还没找到",    # "haven't found yet"
        "继续搜索",    # "continue searching"
    ]
    final_lower = final.lower()
    mid_task_stop = any(s in final_lower for s in incomplete_signals)
    if mid_task_stop:
        # Save the signal that caused failure for debugging
        triggered = [s for s in incomplete_signals if s in final_lower]
        sb.save_result(session_id, _TEST_NAME, msg, status="MID_TASK_STOP")
        assert not mid_task_stop, (
            f"Agent stopped mid-task (regression of finish_reason=stop fix). "
            f"Triggered by: {triggered}. "
            f"Response: {final[:400]}"
        )

    # Response must be substantive — a real summary, not an error or empty response
    assert len(final) > 50, (
        f"Response too short to be a real summary: {final!r}"
    )
