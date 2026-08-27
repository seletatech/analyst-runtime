"""E2E: Video content extraction.

Real user basis: 9d6b11a5 — user pastes a BiliBili video URL and asks to scrape
subtitles and summarize the content.
"帮我爬一下这个的字幕然后总结其中的全面信息给我"
(Scrape the subtitles and give me a comprehensive summary)

Agent used: exec (×2), firecrawl_browser, firecrawl_scrape, edit_file, list_dir

Key invariants:
  - Agent does NOT just refuse — it attempts to access the URL with available tools
  - Agent uses at least one tool (exec or firecrawl) to try to fetch content
  - If extraction fails, agent explains what it tried and why it failed
"""
from __future__ import annotations

import os
import uuid

import pytest

from .conftest import StagingSandbox

# BiliBili video used by real user
_BILIBILI_URL = "https://www.bilibili.com/video/BV1719FBcEgG/"


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_bilibili_video_extraction_attempts_tools(sb: StagingSandbox) -> None:
    """Agent should attempt to extract content from a video URL, not just refuse."""
    session_id = f"e2e-bili-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = f"帮我爬一下这个视频的字幕然后总结其中的全面信息给我：{_BILIBILI_URL}"
    sb.send(msg, session_id)

    try:
        # Use wait_for_last_final_response: agent sends progress updates mid-chain
        final = sb.wait_for_last_final_response(session_id, timeout=160, idle_seconds=10)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_bilibili_video_extraction_attempts_tools", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_bilibili_video_extraction_attempts_tools", msg, status=status)

    # Agent must attempt SOMETHING — not just say "I can't do this"
    assert tool_calls, (
        f"Agent made no tool calls for video URL — should attempt exec or firecrawl. "
        f"Response: {final[:300]}"
    )

    # Agent should use at least one active fetching tool
    # web_fetch is acceptable as a first attempt; exec/firecrawl are preferred
    active_tools = {"exec", "firecrawl_browser", "firecrawl_scrape", "firecrawl_search", "web_fetch"}
    used_tools = {t["tool_name"] for t in tool_calls}
    assert used_tools & active_tools, (
        f"Agent only used passive tools for video extraction. "
        f"Tools: {[t['tool_name'] for t in tool_calls]}. "
        f"Expected exec, web_fetch, or firecrawl to fetch video content. "
        f"Response: {final[:200]}"
    )

    assert len(final) > 10, f"Response empty: {final}"


@pytest.mark.e2e
@pytest.mark.timeout(120)
def test_youtube_url_extraction_attempt(sb: StagingSandbox) -> None:
    """YouTube URL — agent should attempt to get video info, not just refuse."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-yt-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = "Can you summarize this YouTube video for me? https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_final_response(session_id, timeout=110)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_youtube_url_extraction_attempt", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_youtube_url_extraction_attempt", msg, status=status)

    # Agent should try SOMETHING
    assert tool_calls or len(final) > 80, (
        f"Agent gave up immediately on YouTube URL. "
        f"Should either attempt extraction or explain what it tried. "
        f"Response: {final}"
    )
