"""Tests for HeartbeatService and _is_heartbeat_empty."""
from __future__ import annotations

import asyncio
from pathlib import Path

from analyst_runtime.heartbeat.service import (
    DEFAULT_HEARTBEAT_INTERVAL_S,
    HEARTBEAT_OK_TOKEN,
    HEARTBEAT_PROMPT,
    HeartbeatService,
    _is_heartbeat_empty,
)

# ---------------------------------------------------------------------------
# _is_heartbeat_empty
# ---------------------------------------------------------------------------

class TestIsHeartbeatEmpty:

    def test_none_is_empty(self) -> None:
        assert _is_heartbeat_empty(None) is True

    def test_empty_string_is_empty(self) -> None:
        assert _is_heartbeat_empty("") is True

    def test_no_active_tasks_section_is_empty(self) -> None:
        content = "# HEARTBEAT\n\nSome boilerplate text.\n"
        assert _is_heartbeat_empty(content) is True

    def test_active_tasks_with_only_empty_checkboxes_is_empty(self) -> None:
        content = "## Active Tasks\n\n- [ ]\n* [ ]\n"
        assert _is_heartbeat_empty(content) is True

    def test_active_tasks_with_text_is_not_empty(self) -> None:
        content = "## Active Tasks\n\n- Write a report on X\n"
        assert _is_heartbeat_empty(content) is False

    def test_active_tasks_with_completed_checkbox_is_not_empty(self) -> None:
        content = "## Active Tasks\n\n- [x] Done task\n"
        assert _is_heartbeat_empty(content) is False

    def test_content_outside_active_tasks_is_ignored(self) -> None:
        content = "## Some Other Section\n\nDo important things\n\n## Active Tasks\n\n"
        assert _is_heartbeat_empty(content) is True

    def test_html_comment_in_active_tasks_is_ignored(self) -> None:
        content = "## Active Tasks\n\n<!-- Add tasks here -->\n"
        assert _is_heartbeat_empty(content) is True

    def test_mixed_sections_only_checks_active_tasks(self) -> None:
        content = (
            "## Notes\n\nRemember to do X\n\n"
            "## Active Tasks\n\n- [ ]\n\n"
            "## Completed\n\nDone Y\n"
        )
        assert _is_heartbeat_empty(content) is True

    def test_active_tasks_case_insensitive(self) -> None:
        """Section heading match is case-insensitive."""
        content = "## active tasks\n\n- Do something\n"
        assert _is_heartbeat_empty(content) is False

    def test_empty_lines_in_active_tasks_are_ignored(self) -> None:
        content = "## Active Tasks\n\n\n\n"
        assert _is_heartbeat_empty(content) is True


# ---------------------------------------------------------------------------
# HeartbeatService constants
# ---------------------------------------------------------------------------

def test_default_interval_is_one_hour() -> None:
    assert DEFAULT_HEARTBEAT_INTERVAL_S == 3600


def test_heartbeat_ok_token_defined() -> None:
    assert HEARTBEAT_OK_TOKEN == "HEARTBEAT_OK"


def test_heartbeat_prompt_mentions_heartbeat_file() -> None:
    assert "HEARTBEAT.md" in HEARTBEAT_PROMPT


# ---------------------------------------------------------------------------
# HeartbeatService._tick
# ---------------------------------------------------------------------------

class TestHeartbeatServiceTick:

    def test_tick_skips_when_no_heartbeat_file(self, tmp_path: Path) -> None:
        calls: list[str] = []

        async def callback(prompt: str) -> str:
            calls.append(prompt)
            return "response"

        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=callback)
        asyncio.run(svc._tick())

        assert calls == []  # no file → no LLM call

    def test_tick_skips_when_heartbeat_empty(self, tmp_path: Path) -> None:
        (tmp_path / "HEARTBEAT.md").write_text("## Active Tasks\n\n- [ ]\n")
        calls: list[str] = []

        async def callback(prompt: str) -> str:
            calls.append(prompt)
            return "response"

        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=callback)
        asyncio.run(svc._tick())

        assert calls == []

    def test_tick_calls_callback_when_tasks_present(self, tmp_path: Path) -> None:
        (tmp_path / "HEARTBEAT.md").write_text(
            "## Active Tasks\n\n- Send weekly summary\n"
        )
        calls: list[str] = []

        async def callback(prompt: str) -> str:
            calls.append(prompt)
            return "done"

        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=callback)
        asyncio.run(svc._tick())

        assert calls == [HEARTBEAT_PROMPT]

    def test_tick_no_crash_when_no_callback(self, tmp_path: Path) -> None:
        (tmp_path / "HEARTBEAT.md").write_text(
            "## Active Tasks\n\n- Something\n"
        )
        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=None)
        asyncio.run(svc._tick())  # must not raise

    def test_tick_no_crash_when_callback_raises(self, tmp_path: Path) -> None:
        (tmp_path / "HEARTBEAT.md").write_text(
            "## Active Tasks\n\n- Something\n"
        )

        async def bad_callback(prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=bad_callback)
        asyncio.run(svc._tick())  # error must be swallowed, not propagated


# ---------------------------------------------------------------------------
# HeartbeatService.trigger_now
# ---------------------------------------------------------------------------

class TestHeartbeatServiceTriggerNow:

    def test_trigger_now_calls_callback_immediately(self, tmp_path: Path) -> None:
        calls: list[str] = []

        async def callback(prompt: str) -> str:
            calls.append(prompt)
            return "triggered"

        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=callback)
        result = asyncio.run(svc.trigger_now())

        assert result == "triggered"
        assert calls == [HEARTBEAT_PROMPT]

    def test_trigger_now_returns_none_without_callback(self, tmp_path: Path) -> None:
        svc = HeartbeatService(workspace=tmp_path, on_heartbeat=None)
        result = asyncio.run(svc.trigger_now())
        assert result is None


# ---------------------------------------------------------------------------
# HeartbeatService enabled/disabled
# ---------------------------------------------------------------------------

class TestHeartbeatServiceLifecycle:

    def test_disabled_service_does_not_start_loop(self, tmp_path: Path) -> None:
        svc = HeartbeatService(workspace=tmp_path, enabled=False)
        asyncio.run(svc.start())
        assert svc._task is None  # no asyncio task created

    def test_stop_cancels_running_task(self, tmp_path: Path) -> None:
        async def run():
            svc = HeartbeatService(workspace=tmp_path, interval_s=9999)
            await svc.start()
            assert svc._task is not None
            svc.stop()
            assert svc._task is None

        asyncio.run(run())

    def test_heartbeat_file_path_is_workspace_slash_heartbeat(
        self, tmp_path: Path
    ) -> None:
        svc = HeartbeatService(workspace=tmp_path)
        assert svc.heartbeat_file == tmp_path / "HEARTBEAT.md"
