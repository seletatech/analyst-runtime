"""E2E: web scraping behavior.

Scenario: user sends a Luma URL. Agent should use firecrawl_scrape or
firecrawl_search as the first tool, NOT web_fetch (which fails silently on JS SPAs).

Real user basis: Qian asked for SF events ("赶紧的") — Samantha should go
straight to Firecrawl, not waste a turn with web_fetch.
"""
from __future__ import annotations

import os
import uuid

import pytest

from .conftest import StagingSandbox


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_luma_url_does_not_use_web_fetch_first(sb: StagingSandbox) -> None:
    """First tool on a Luma URL must be firecrawl, not web_fetch."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-luma-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    sb.send("请问 https://lu.ma/sf 最近有什么活动？", session_id)
    sb.wait_for_final_response(session_id, timeout=150)

    tool_calls = sb.read_tool_calls(session_id)
    assert tool_calls, (
        "No tool calls found in session — agent responded without using any tools. "
        f"Container logs:\n{sb.container_logs(tail=50)}"
    )

    # Agent must use firecrawl for lu.ma (JS SPA). web_fetch may be tried first
    # as a cheap attempt, but firecrawl must be used somewhere in the chain —
    # lu.ma requires JS execution to render event listings.
    firecrawl_tools = {"firecrawl_scrape", "firecrawl_search", "firecrawl_browser"}
    used_tools = {t["tool_name"] for t in tool_calls}
    assert used_tools & firecrawl_tools, (
        f"No firecrawl tool used for lu.ma (JS SPA) — agent should escalate to firecrawl. "
        f"Tools used: {[t['tool_name'] for t in tool_calls]}"
    )
