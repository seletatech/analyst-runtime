"""Regression tests for tool result storage in session history.

Bug: _save_tool_result stored opaque reference strings like
  "[tool result (N chars) saved to sessions/tool-results/xxx.txt — use read_file to access]"
in session events. On subsequent turns, get_history() returned these references
instead of real content, causing the LLM to:
  1. Think work was already done based on "evidence" of file saves
  2. Not call tools, just text-respond with summaries of phantom work
  3. Silently fail on multi-step tasks that required cross-turn context

Fix: short results are stored inline verbatim; long results store the first
_INLINE_RESULT_CHARS characters inline (so the LLM has real context), with the
full result archived to disk.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from analyst_runtime.agent.loop import AgentLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_loop(workspace: Path) -> AgentLoop:
    """Instantiate AgentLoop with minimal stubs (no real LLM/bus needed)."""
    from unittest.mock import MagicMock

    bus = MagicMock()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop


# ---------------------------------------------------------------------------
# _save_tool_result behaviour
# ---------------------------------------------------------------------------

class TestSaveToolResult:
    def test_short_result_returned_inline(self, tmp_path: Path) -> None:
        """Results under the threshold must be returned as-is, no file written."""
        loop = _make_loop(tmp_path)
        short = "ok: 42 results found"
        out = loop._save_tool_result("tc_001", "search", short)

        assert out == short, "Short result must be returned verbatim"
        # No file should have been written
        tool_results_dir = tmp_path / "sessions" / "tool-results"
        assert not tool_results_dir.exists() or not list(tool_results_dir.glob("*.txt"))

    def test_long_result_preview_is_inline(self, tmp_path: Path) -> None:
        """Long results must have meaningful content inline, not an opaque reference."""
        loop = _make_loop(tmp_path)
        long_result = "x" * (AgentLoop._INLINE_RESULT_CHARS + 500)
        out = loop._save_tool_result("tc_002", "exec", long_result)

        # Must start with actual content, not a reference-only string
        assert out.startswith("x"), (
            "Long result must begin with inline content, not a reference string"
        )
        # Must contain the truncation marker
        assert "chars total" in out, "Truncation marker missing from long result"
        # Full archive file must exist
        archive = tmp_path / "sessions" / "tool-results" / "tc_002.txt"
        assert archive.exists(), "Archive file should be written for long results"
        assert archive.read_text() == long_result, "Archive must contain full result"

    def test_opaque_reference_is_not_returned(self, tmp_path: Path) -> None:
        """The old opaque-only reference format must NOT be produced.

        Regression guard: if only a reference is stored, the LLM sees
        '[tool result saved to ... use read_file to access]' without any
        real content, and incorrectly assumes the work is done.
        """
        loop = _make_loop(tmp_path)
        long_result = "A" * (AgentLoop._INLINE_RESULT_CHARS + 1)
        out = loop._save_tool_result("tc_003", "web_fetch", long_result)

        # Must NOT be only a reference string
        assert not out.startswith("["), (
            "Opaque-only reference returned — LLM won't have context in subsequent turns"
        )
        # Must not contain "use read_file to access" as the entire message
        assert "use read_file to access" not in out, (
            "Old opaque reference format detected — breaks cross-turn tool chains"
        )

    def test_error_result_stored_inline(self, tmp_path: Path) -> None:
        """Error strings (short) must be stored inline so LLM sees the error."""
        loop = _make_loop(tmp_path)
        error = "Error: Tool 'exec' timed out after 30s"
        out = loop._save_tool_result("tc_004", "exec", error)

        assert out == error, "Short error results must be inline for cross-turn visibility"
