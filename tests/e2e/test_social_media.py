"""E2E: Twitter/X.com URL scraping.

Real user basis: 98720a4c spent 21 sessions testing different ways to read tweet content.
f947c29f ran 2 dedicated browser-login test sessions for X.com.

Key invariant: X.com is a JS SPA. web_fetch returns empty HTML or 403.
Agent must use firecrawl_browser or firecrawl_scrape, never web_fetch as the first call.
"""
from __future__ import annotations

import os
import uuid

import pytest

from .conftest import StagingSandbox

_TWEET_URL = "https://x.com/gabrielchua/status/2037549841572901168"


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_twitter_x_url_uses_firecrawl(sb: StagingSandbox) -> None:
    """Agent must use firecrawl (not web_fetch) for an X.com tweet URL."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-xtweet-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = f"请读取这条推文的内容：{_TWEET_URL}"
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_final_response(session_id, timeout=150)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_twitter_x_url_uses_firecrawl", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_twitter_x_url_uses_firecrawl", msg, status=status)

    assert tool_calls, (
        f"No tool calls made — agent should have used firecrawl to access X.com. "
        f"Response: {final[:200]}"
    )

    # Agent must use firecrawl somewhere in the chain — X.com is a JS SPA.
    # web_fetch may be tried first as a cheap attempt, but firecrawl must follow
    # if web_fetch returns empty/blocked content.
    firecrawl_tools = {"firecrawl_browser", "firecrawl_scrape", "firecrawl_search"}
    used_tools = {t["tool_name"] for t in tool_calls}
    assert used_tools & firecrawl_tools, (
        f"No firecrawl tool called for X.com URL — agent should escalate to firecrawl. "
        f"Tools used: {[t['tool_name'] for t in tool_calls]}. "
        f"Response: {final[:200]}"
    )


