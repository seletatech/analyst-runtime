"""Tests for tool_use / tool_result pairing in session history.

Regression: llm_response events with tool_calls were persisted to the session
but tool_result events were not (for normal, non-malformed tool calls). On the
next turn, get_history() reconstructed a history with orphaned tool_use blocks
that Bedrock rejected:
  "tool_use ids were found without tool_result blocks immediately after"
"""
from __future__ import annotations

from analyst_runtime.session.manager import Session


def _make_tool_call(tc_id: str, name: str = "some_tool") -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


# ---------------------------------------------------------------------------
# get_history() — orphaned tool_use handling
# ---------------------------------------------------------------------------

def test_get_history_skips_orphaned_tool_use() -> None:
    """llm_response with tool_calls but no following tool_result must be excluded.

    Regression: the session stored llm_response+tool_calls without a tool_result,
    so get_history() produced an invalid sequence that Bedrock rejected.
    """
    session = Session(key="test:user1")
    session.add_event({"type": "user_input", "content": "send me a whatsapp"})
    session.add_event({
        "type": "llm_response",
        "content": "",
        "tool_calls": [_make_tool_call("tc_abc123")],
    })
    # No tool_result event — the orphan condition
    session.add_event({"type": "final_response", "content": "Done!"})

    history = session.get_history()

    # Validate: no assistant message in history should have tool_calls
    # without an immediately-following tool message.
    for i, msg in enumerate(history):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            nxt = history[i + 1] if i + 1 < len(history) else None
            assert nxt is not None and nxt.get("role") == "tool", (
                f"Orphaned tool_use at index {i}: next message is {nxt!r}"
            )


def test_get_history_includes_paired_tool_use_and_result() -> None:
    """When tool_result events ARE present they must appear in history."""
    session = Session(key="test:user1")
    session.add_event({"type": "user_input", "content": "call a tool"})
    session.add_event({
        "type": "llm_response",
        "content": "",
        "tool_calls": [_make_tool_call("tc_xyz")],
    })
    session.add_event({
        "type": "tool_result",
        "tool_use_id": "tc_xyz",
        "tool_name": "some_tool",
        "content": "tool output",
    })
    session.add_event({"type": "final_response", "content": "All done."})

    history = session.get_history()

    roles = [m["role"] for m in history]
    assert "tool" in roles, "tool_result message missing from history"
    # Verify correct sequence: user → assistant(tool_calls) → tool → assistant
    assert roles == ["user", "assistant", "tool", "assistant"], (
        f"Unexpected message sequence: {roles}"
    )
