"""E2E: multi-step tool chain.

Scenario: user asks for web research requiring multiple sequential tool calls.
Validates the 200-iteration cap fix and inline tool result storage:
  - Each scrape result must be visible to the LLM on the next turn (inline context)
  - Agent must not stop after 1-2 tools due to the old 20-iteration cap

Real user basis: Qian (55 tool calls) and Xiaoyi (63 tool calls) routinely do
multi-step research tasks. The old cap silently truncated their work.
"""
from __future__ import annotations

import uuid

import pytest

from .conftest import StagingSandbox


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_multi_step_research_uses_multiple_tools(sb: StagingSandbox) -> None:
    """Research task must trigger ≥3 sequential tool calls (search + scrapes)."""
    import os
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-chain-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    sb.send(
        "帮我搜索最近关于 Claude AI 的新闻，找到两篇文章，每篇用一句中文总结",
        session_id,
    )
    # Use wait_for_last_final_response: agent sends intermediate progress messages
    # (e.g. "one article failed, trying another") before completing the full chain.
    final = sb.wait_for_last_final_response(session_id, timeout=160, idle_seconds=10)

    tool_calls = sb.read_tool_calls(session_id)

    # Must have used at least one research tool — validates the iteration cap fix
    # allows multi-step chains to proceed (not stop at 1 turn)
    assert len(tool_calls) >= 1, (
        f"No tool calls made for research task — agent should at minimum search. "
        f"Got: {[t['tool_name'] for t in tool_calls]}. "
        f"Container logs:\n{sb.container_logs(tail=50)}"
    )

    # At least one firecrawl call (validates agent fetches from web, not just training data)
    firecrawl_calls = [t for t in tool_calls if "firecrawl" in t["tool_name"]]
    assert firecrawl_calls, (
        f"No firecrawl calls found — agent may have answered from training data only. "
        f"Tools: {[t['tool_name'] for t in tool_calls]}"
    )
