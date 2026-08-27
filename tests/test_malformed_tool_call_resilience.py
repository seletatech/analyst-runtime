"""Regression tests for malformed tool call resilience in the agent loop.

Kimi k2.5 emits "tooluse_XYZ({})" calls with the tool-use ID as the tool name
(instead of an actual tool name) as transition pauses between real work batches.

Previous behaviour: abort after 2 consecutive all-malformed iterations.
This killed long tasks (e.g. 14-day calendar build) after only 1 day of work.

Fixed behaviour:
- Dynamic consecutive threshold: 2 if no real work done yet, 5 once real work
  has started.
- Hard cap: abort after 8 total all-malformed iterations.
- All-malformed iterations are NOT persisted to the session JSONL — persisting
  them poisoned next-turn history and caused subsequent "continue" turns to
  immediately fail with more malformed calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.manager import Session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MALFORMED_ID = "tooluse_TjnevAm1YHeRfEJrFoN4He"


def _malformed_response() -> LLMResponse:
    """One all-malformed tool call (ID used as name — Kimi k2.5 pattern)."""
    return LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCallRequest(id=MALFORMED_ID, name=MALFORMED_ID, arguments={})
        ],
    )


def _real_response(tool_name: str = "exec", args: dict | None = None) -> LLMResponse:
    """One legitimate tool call."""
    return LLMResponse(
        content="",
        finish_reason="tool_calls",
        tool_calls=[
            ToolCallRequest(id="call-real-1", name=tool_name, arguments=args or {})
        ],
    )


def _final_response(text: str = "Done.") -> LLMResponse:
    return LLMResponse(content=text, finish_reason="stop")


# ---------------------------------------------------------------------------
# Fake provider builder
# ---------------------------------------------------------------------------

class ScriptedProvider(LLMProvider):
    """Returns pre-scripted LLMResponse objects in order."""

    def __init__(self, script: Sequence[LLMResponse]) -> None:
        super().__init__()
        self._script = list(script)
        self._idx = 0
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7):
        self.calls += 1
        if self._idx < len(self._script):
            resp = self._script[self._idx]
            self._idx += 1
            return resp
        # Fallback: stop the loop
        return _final_response("(script exhausted)")

    def get_default_model(self) -> str:
        return "fake-model"


# ---------------------------------------------------------------------------
# Session JSONL persistence tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_malformed_iterations_not_persisted_to_session(tmp_path: Path) -> None:
    """All-malformed iterations must NOT appear in session history.

    Regression: persisting malformed tool_use → "Tool not found" sequences
    poisoned the history reconstructed on the next user turn, causing the model
    to immediately generate more malformed calls on "continue".
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    provider = ScriptedProvider([
        _malformed_response(),   # all-malformed iteration — must NOT be saved
        _malformed_response(),   # second (abort fires, still must NOT be saved)
    ])

    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    session = Session(key="telegram:test-1")

    messages = agent.context.build_messages(
        history=[],
        current_message="do something",
        channel="web",
        chat_id="test-1",
    )

    await agent._run_agent_loop(
        initial_messages=messages,
        session=session,
        request_uuid="req-1",
    )

    history = session.get_history()
    # Only the user_input event is expected — no assistant / tool messages from
    # the malformed iterations should appear in reconstructed history.
    roles = [m["role"] for m in history]
    assert "tool" not in roles, (
        f"Malformed tool_result should not appear in history, got: {roles}"
    )
    # No assistant message with tool_calls either
    for msg in history:
        if msg.get("role") == "assistant":
            assert not msg.get("tool_calls"), (
                "Malformed llm_response must not be persisted to session JSONL"
            )


@pytest.mark.asyncio
async def test_real_tool_results_are_still_persisted(tmp_path: Path) -> None:
    """Real (non-malformed) tool results must still appear in session history."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    provider = ScriptedProvider([
        _real_response("read_file", {"path": "/workspace/memory/MEMORY.md"}),
        _final_response("All done."),
    ])

    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    session = Session(key="telegram:test-2")

    messages = agent.context.build_messages(
        history=[],
        current_message="read my memory",
        channel="web",
        chat_id="test-2",
    )

    await agent._run_agent_loop(
        initial_messages=messages,
        session=session,
        request_uuid="req-2",
    )

    history = session.get_history()
    roles = [m["role"] for m in history]
    assert "tool" in roles, "Real tool_result must be persisted to session JSONL"


# ---------------------------------------------------------------------------
# Abort threshold tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stuck_model_aborts_after_2_consecutive_malformed(tmp_path: Path) -> None:
    """With no real work done, abort after exactly 2 consecutive all-malformed."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Feed 10 malformed responses — loop must abort well before exhausting them.
    provider = ScriptedProvider([_malformed_response()] * 10)

    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    session = Session(key="telegram:test-3")

    messages = agent.context.build_messages(
        history=[], current_message="go", channel="web", chat_id="test-3",
    )

    await agent._run_agent_loop(
        initial_messages=messages, session=session, request_uuid="req-3",
    )

    # Should abort after 2 calls: 2 consecutive all-malformed, zero real work.
    assert provider.calls == 2, (
        f"Expected abort after 2 malformed calls (no real work), got {provider.calls}"
    )


@pytest.mark.asyncio
async def test_model_with_real_work_tolerates_malformed_pauses(tmp_path: Path) -> None:
    """After real work, the loop must survive 2 consecutive malformed and continue."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Script: real → 2 malformed → real → done text (triggers second-chance) → final answer
    # The loop injects one continuation prompt when the model returns text-only mid-task
    # (finish_reason=stop, tools_used, no tool_calls). The model then confirms it's done.
    provider = ScriptedProvider([
        _real_response("read_file", {"path": "/workspace/memory/MEMORY.md"}),
        _malformed_response(),
        _malformed_response(),
        _real_response("read_file", {"path": "/workspace/memory/MEMORY.md"}),
        _final_response("Finished."),   # triggers mid-task continuation prompt
        _final_response("Finished."),   # second-chance: model confirms done, loop exits
    ])

    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    session = Session(key="telegram:test-4")

    messages = agent.context.build_messages(
        history=[], current_message="do multi-step task", channel="web", chat_id="test-4",
    )

    content, tools_used = await agent._run_agent_loop(
        initial_messages=messages, session=session, request_uuid="req-4",
    )

    # All 6 responses must have been consumed — the loop survived the 2-malformed pause,
    # plus one mid-task continuation prompt for the first text-only stop.
    assert provider.calls == 6, (
        f"Loop aborted too early: expected 6 LLM calls, got {provider.calls}"
    )
    assert content == "Finished.", f"Unexpected final content: {content!r}"


@pytest.mark.asyncio
async def test_complete_report_after_tool_use_is_not_replaced_by_second_answer(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = """## 一句话结论
数据不足，但已完成证据核验。

## 可确认事实
直接配对样本为 0，当前不能估计方向。

## 探索性推测
候选机制仍需批次连接键验证；这是低置信度推测，不是事实。
"""
    provider = ScriptedProvider(
        [
            _real_response("read_file", {"path": "/workspace/analysis.py"}),
            _final_response(report),
            _final_response("这段摘要不应该替换完整报告。"),
        ]
    )
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    content, _tools = await agent._run_agent_loop(
        initial_messages=[{"role": "user", "content": "分析数据"}],
    )

    assert provider.calls == 2
    assert content == report.strip()


@pytest.mark.asyncio
async def test_summary_that_references_missing_previous_report_is_recovered(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    incomplete = "任务已完成。最终交付摘要如下，完整报告已在上一条消息中交付。"
    complete = """# 老板报告
## 一句话结论
完整结论。
## 可确认事实
完整明细表。
## 缺失数据与反证条件
数据限制。
## 老板可能还没注意到
一个数据盲区。
## 下一步
执行动作。
"""
    provider = ScriptedProvider(
        [
            _real_response("read_file", {"path": "/workspace/analysis.py"}),
            _final_response(incomplete),
            _final_response(complete),
        ]
    )
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    content, _tools = await agent._run_agent_loop(
        initial_messages=[{"role": "user", "content": "生成老板报告"}],
    )

    assert provider.calls == 3
    assert content == complete.strip()


def test_incomplete_delivery_detector_distinguishes_full_report() -> None:
    assert AgentLoop._looks_like_incomplete_delivery(
        "最终交付摘要：完整报告已在上一条消息中交付。"
    )
    assert AgentLoop._looks_like_incomplete_delivery(
        "报告正文（含月度明细表、盲区分析与下一步排程）已在上一条消息完整给出。"
    )
    assert not AgentLoop._looks_like_incomplete_delivery(
        "## 一句话结论\n结论。\n## 可确认事实\n事实。\n"
        "## 缺失数据与反证条件\n限制。\n## 老板可能还没注意到\n盲区。\n"
        "## 下一步\n行动。"
    )


@pytest.mark.asyncio
async def test_hard_cap_aborts_after_8_total_malformed(tmp_path: Path) -> None:
    """Hard cap: abort after 8 total all-malformed iterations, regardless of real work."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Alternate: real, malformed, real, malformed, ... so consecutive never hits 5,
    # but total malformed accumulates to 8.
    script: list[LLMResponse] = []
    for _ in range(20):
        script.append(_real_response("read_file", {"path": "/workspace/memory/MEMORY.md"}))
        script.append(_malformed_response())

    provider = ScriptedProvider(script)

    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    session = Session(key="telegram:test-5")

    messages = agent.context.build_messages(
        history=[], current_message="long task", channel="web", chat_id="test-5",
    )

    await agent._run_agent_loop(
        initial_messages=messages, session=session, request_uuid="req-5",
    )

    # 8 total malformed → abort after 8 real + 8 malformed = 16 calls, but
    # the cap triggers at 8 total malformed so at most 8+8=16 LLM calls.
    # Real work resets consecutive but NOT total, so the cap fires at total=8.
    assert provider.calls <= 16, (
        f"Hard cap should abort by 16 calls, got {provider.calls}"
    )
    # Must have hit exactly 8 total malformed (paired with 8 real = 16 calls).
    assert provider.calls == 16, (
        f"Expected exactly 16 calls (8 real + 8 malformed before hard cap), "
        f"got {provider.calls}"
    )
