"""Tests for tool_use / tool_result pairing in session history.

Regression: llm_response events with tool_calls were persisted to the session
but tool_result events were not (for normal, non-malformed tool calls). On the
next turn, get_history() reconstructed a history with orphaned tool_use blocks
that Bedrock rejected:
  "tool_use ids were found without tool_result blocks immediately after"
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from analyst_runtime.agent.context import ContextBuilder
from analyst_runtime.agent.steering import SteeringCoordinator, SteerKey
from analyst_runtime.bus.events import InboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.session.manager import Session, SessionManager


def _make_tool_call(tc_id: str, name: str = "some_tool") -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_fresh_process_can_import_save_and_restore_session(tmp_path: Path) -> None:
    code = """
import sys
from pathlib import Path
from analyst_runtime.session.manager import SessionManager

workspace = Path(sys.argv[1])
manager = SessionManager(workspace)
session = manager.get_or_create("test:fresh-import")
session.add_event({"type": "user_input", "content": "fresh process input"})
manager.save(session)
restored = SessionManager(workspace).get_or_create(session.key)
assert [event["content"] for event in restored.events] == ["fresh process input"]
print("fresh-import-save-restore-ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "fresh-import-save-restore-ok\n"


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


@pytest.mark.parametrize("association", ["legacy", "linked", "linked-id-reuse"])
def test_partial_tool_batch_keeps_trace_without_orphaning_history(
    tmp_path: Path,
    association: str,
) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("test:partial-batch")
    session.add_event({"type": "user_input", "content": "previous task"})
    session.add_event(
        {
            "type": "llm_response",
            "content": "",
            "tool_calls": [_make_tool_call("previous")],
        }
    )
    session.add_event(
        {
            "type": "tool_result",
            "tool_use_id": "previous",
            "tool_name": "some_tool",
            "content": "previous completed result",
        }
    )
    session.add_event({"type": "final_response", "content": "previous answer"})
    session.add_event({"type": "user_input", "content": "interrupted task"})
    session.add_event(
        {
            "type": "llm_response",
            "content": "",
            "tool_calls": [_make_tool_call("partial-a"), _make_tool_call("partial-b")],
        }
    )
    session.add_event(
        {
            "type": "tool_result",
            "tool_use_id": "partial-a",
            "tool_name": "some_tool",
            "content": "A completed before B was interrupted",
        }
    )
    if association != "legacy":
        session.events[-2]["uuid"] = "first-assistant"
        session.events[-1]["source_assistant_uuid"] = "first-assistant"
    if association == "linked-id-reuse":
        session.add_event(
            {
                "type": "llm_response",
                "uuid": "second-assistant",
                "content": "",
                "tool_calls": [_make_tool_call("partial-b")],
            }
        )
        session.add_event(
            {
                "type": "tool_result",
                "tool_use_id": "partial-b",
                "source_assistant_uuid": "second-assistant",
                "tool_name": "some_tool",
                "content": "B belongs to a later completed batch",
            }
        )
    manager.save(session)

    restored = SessionManager(tmp_path).get_or_create(session.key)
    history = restored.get_history()

    roles = ["user", "assistant", "tool", "assistant", "user"]
    if association == "linked-id-reuse":
        roles.extend(["assistant", "tool"])
    assert [message["role"] for message in history] == roles
    assert history[2]["tool_call_id"] == "previous"
    assert history[2]["content"] == "previous completed result"
    if association == "linked-id-reuse":
        assert [call["id"] for call in history[-2]["tool_calls"]] == ["partial-b"]
        assert history[-1]["content"] == "B belongs to a later completed batch"
    assert restored.events == session.events
    assert any(event.get("tool_use_id") == "partial-a" for event in restored.events)


def test_failed_save_preserves_last_disk_snapshot_and_cleans_temporary_file(
    tmp_path: Path,
) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("test:atomic-save")
    session.add_event({"type": "user_input", "content": "last durable input"})
    manager.save(session)
    directory = tmp_path / "sessions"
    snapshot = next(directory.glob("*.jsonl"))
    previous_bytes = snapshot.read_bytes()
    previous_files = {path.name for path in directory.iterdir()}

    steer = InboundMessage(
        channel="test",
        sender_id="synthetic",
        chat_id="atomic-save",
        content="apply this instruction",
        run_id="atomic-run",
        metadata={"steer_id": "uncommitted-steer"},
    )
    key = SteerKey.from_message(steer)
    assert key is not None
    coordinator = SteeringCoordinator(
        bus=MessageBus(), sessions=manager, context=ContextBuilder(tmp_path)
    )
    session.metadata["applied_steer_keys"] = [key.storage_id()]
    session.add_event({"type": "user_input", "content": object()})
    with pytest.raises(TypeError):
        manager.save(session)

    assert coordinator.was_applied(steer) is False
    assert snapshot.read_bytes() == previous_bytes
    assert {path.name for path in directory.iterdir()} == previous_files
    restored = SessionManager(tmp_path).get_or_create(session.key)
    assert [event["content"] for event in restored.events] == ["last durable input"]
