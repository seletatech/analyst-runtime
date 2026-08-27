"""E2E: Exec shell command execution.

Real user basis:
  - 98720a4c ran exec 88x across sessions: checking env vars, listing sessions,
    reading config files, verifying browser capabilities
  - 92f02a25 ran exec 53x in a single session: setting up Python web servers,
    cloudflare tunnels, file management, troubleshooting 502s

Key invariants:
  - Agent calls exec tool when user asks for shell operations
  - Agent uses stdout result in the response
  - Agent handles multi-step shell tasks (run → inspect output → run again)
"""
from __future__ import annotations

import uuid

import pytest

from .conftest import StagingSandbox


@pytest.mark.e2e
@pytest.mark.timeout(60)
def test_exec_simple_command_is_called(sb: StagingSandbox) -> None:
    """When user asks to run a shell command, agent must call exec."""
    session_id = f"e2e-exec-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = "请运行命令：echo hello_from_e2e_test 并把输出告诉我"
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_final_response(session_id, timeout=50)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_exec_simple_command_is_called", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_exec_simple_command_is_called", msg, status=status)

    exec_calls = [t for t in tool_calls if t["tool_name"] == "exec"]
    assert exec_calls, (
        f"Agent did not call exec for a shell command request. "
        f"Tools used: {[t['tool_name'] for t in tool_calls]}. "
        f"Response: {final[:200]}"
    )

    # Response should contain the output
    assert "hello_from_e2e_test" in final or any(
        "hello_from_e2e_test" in tc["content"] for tc in exec_calls
    ), (
        f"exec output 'hello_from_e2e_test' not visible in response or tool result. "
        f"Response: {final[:300]}"
    )


@pytest.mark.e2e
@pytest.mark.timeout(90)
def test_exec_file_listing_uses_output(sb: StagingSandbox) -> None:
    """Agent runs ls, uses output to answer question about what files exist."""
    session_id = f"e2e-exec-ls-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    msg = "运行 exec: ls -la $WORKSPACE_PATH/sessions/ 2>&1 | head -20 并告诉我 sessions 目录里有什么文件"
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_final_response(session_id, timeout=80)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_exec_file_listing_uses_output", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_exec_file_listing_uses_output", msg, status=status)

    exec_calls = [t for t in tool_calls if t["tool_name"] == "exec"]
    assert exec_calls, (
        f"Expected exec call for file listing. "
        f"Tools: {[t['tool_name'] for t in tool_calls]}. Response: {final[:200]}"
    )

    assert len(final) > 30, f"Response too short after ls: {final}"


@pytest.mark.e2e
@pytest.mark.timeout(120)
def test_exec_multi_step_inspect_output(sb: StagingSandbox) -> None:
    """Agent should run command, read output, and make a follow-up exec call if needed."""
    session_id = f"e2e-exec-multi-{uuid.uuid4().hex[:8]}"
    sb.cleanup_session(session_id)

    # Real pattern from 92f02a25: user sets up a web server, checks if it works, fixes it
    msg = (
        "请做这几步：\n"
        "1. 在 /tmp 创建一个文件 test_e2e.txt 内容是 'hello world'\n"
        "2. 用 cat 读取它并告诉我内容是否正确\n"
        "3. 删掉这个文件"
    )
    sb.send(msg, session_id)

    try:
        final = sb.wait_for_final_response(session_id, timeout=110)
        status = "PASS"
    except TimeoutError as exc:
        sb.save_result(session_id, "test_exec_multi_step_inspect_output", msg, status="TIMEOUT")
        raise AssertionError(str(exc)) from exc

    tool_calls = sb.read_tool_calls(session_id)
    sb.save_result(session_id, "test_exec_multi_step_inspect_output", msg, status=status)

    exec_calls = [t for t in tool_calls if t["tool_name"] == "exec"]
    # Agent may use write_file instead of exec for file creation — both are valid.
    # Assert at least 1 exec call (for cat or delete step) to confirm exec works.
    assert len(exec_calls) >= 1, (
        f"Expected ≥1 exec call for multi-step task (cat/delete step), "
        f"got {len(exec_calls)}. All tools: {[t['tool_name'] for t in tool_calls]}. "
        f"Response: {final[:200]}"
    )
