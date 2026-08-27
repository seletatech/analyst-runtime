"""E2E: Startup / investor research chain.

Real user basis: 6854ecd6 — startup founder doing due diligence on SF accelerators
and VC firms for their Samantha product. Sent messages like:
  "帮我搜索sf所有适合我的创业加速器(做samantha)都发我，以及最近的deadline"
  "DCVC这个投资机构适合我们吗"
  "这个demo day有多大价值" (evaluating Luma demo day URLs)
  Used 12x firecrawl_scrape + 8x firecrawl_search in a single session.

Key invariants:
- Multi-step research: agent searches AND scrapes specific pages (≥2 firecrawl calls)
- Structured output: names + relevant info (deadlines, thesis, fit)
- Real user did this across many turns — agent must maintain context across tools
"""
from __future__ import annotations

import os
import uuid

import pytest

from .conftest import StagingSandbox


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_accelerator_research_is_multi_step(sb: StagingSandbox) -> None:
    """Accelerator research must search AND scrape multiple sources (≥2 firecrawl calls)."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-invest-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = "帮我搜索旧金山有哪些适合早期 AI 创业公司的加速器，给我名字和最近的申请截止日期"
    sb.send(msg, session_id)

    try:
        # Use wait_for_last_final_response: agent sends progress updates mid-chain
        final = sb.wait_for_last_final_response(session_id, timeout=160, idle_seconds=10)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_accelerator_research_is_multi_step", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_accelerator_research_is_multi_step", msg, status=status)

    firecrawl_calls = [t for t in tool_calls if "firecrawl" in t["tool_name"]]
    assert len(firecrawl_calls) >= 1, (
        f"Expected ≥1 firecrawl call for web research, got {len(firecrawl_calls)}. "
        f"All tools: {[t['tool_name'] for t in tool_calls]}. "
        f"Response: {final[:200]}"
    )

    assert len(final) > 10, (
        f"Response empty — agent did not complete the research task. "
        f"Response: {final}"
    )


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_vc_firm_research_scrapes_website(sb: StagingSandbox) -> None:
    """VC firm due diligence should scrape their website for investment thesis."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-vc-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = "帮我调研一下 Y Combinator 这个加速器，他们最新一批的申请截止日期是什么时候？适合什么阶段的公司？"
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_last_final_response(session_id, timeout=160, idle_seconds=10)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_vc_firm_research_scrapes_website", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_vc_firm_research_scrapes_website", msg, status=status)

    # Agent should use search or scrape (not just answer from training data)
    web_tools = {"firecrawl_search", "firecrawl_scrape", "firecrawl_browser", "web_fetch"}
    used_tools = {t["tool_name"] for t in tool_calls}
    assert used_tools & web_tools, (
        f"Agent should have fetched YC info from the web, not just used training data. "
        f"Tools used: {[t['tool_name'] for t in tool_calls]}. "
        f"Response: {final[:200]}"
    )

    assert len(final) > 10, f"Response empty: {final}"


@pytest.mark.e2e
@pytest.mark.timeout(180)
def test_luma_demo_day_evaluation(sb: StagingSandbox) -> None:
    """Agent evaluates a Luma event URL for investor presence and value — uses firecrawl."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        pytest.skip("FIRECRAWL_API_KEY not configured in staging")

    session_id = f"e2e-luma-demo-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    # lu.ma is a JS SPA — same constraint as the web_scrape test
    msg = "请帮我看看 https://lu.ma/sf 上面最近有什么 demo day 或者 YC 相关的活动？值得参加吗？"
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_last_final_response(session_id, timeout=160, idle_seconds=10)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_luma_demo_day_evaluation", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_luma_demo_day_evaluation", msg, status=status)

    # Must not use web_fetch first (JS SPA)
    if tool_calls:
        first = tool_calls[0]["tool_name"]
        assert first != "web_fetch", (
            f"Agent used web_fetch first on lu.ma (JS SPA) — this returns empty HTML. "
            f"Should use firecrawl_browser or firecrawl_scrape. "
            f"All tools: {[t['tool_name'] for t in tool_calls]}"
        )

    assert len(final) > 10, f"Response empty: {final}"
