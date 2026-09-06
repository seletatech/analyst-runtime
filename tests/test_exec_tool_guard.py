"""Tests for ExecTool._guard_command path-restriction logic.

The guard must block absolute paths that exist outside the workspace,
but must NOT block commands that contain URL path components (e.g.
https://example.com/some/path) or other non-existent path-like strings
embedded in argument values like --description or --summary.
"""

from __future__ import annotations

import json
import sys

import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.agent.tools.shell import ExecTool


@pytest.fixture()
def tool(tmp_path):
    """ExecTool with workspace restriction enabled, workspace = tmp_path."""
    return ExecTool(
        working_dir=str(tmp_path),
        restrict_to_workspace=True,
    )


# ---------------------------------------------------------------------------
# Should NOT be blocked
# ---------------------------------------------------------------------------


def test_simple_command_passes(tool):
    assert tool._guard_command("ls -la", str(tool.working_dir)) is None


def test_gws_calendar_insert_no_description_passes(tool):
    cmd = 'gws calendar +insert --summary "Meeting" --start "2026-04-02T10:00:00+08:00" --end "2026-04-02T11:00:00+08:00"'
    assert tool._guard_command(cmd, str(tool.working_dir)) is None


def test_gws_calendar_insert_with_url_in_description_passes(tool):
    """URL path components in --description must not be treated as filesystem paths."""
    cmd = (
        'gws calendar +insert --summary "ByteDance Interview" '
        '--start "2026-04-02T15:00:00+08:00" --end "2026-04-02T16:00:00+08:00" '
        '--description "Interview link: https://t.zijieimg.com/K9fImd7LblA/"'
    )
    assert tool._guard_command(cmd, str(tool.working_dir)) is None


def test_gws_with_multiline_description_containing_url_passes(tool):
    """Newline + URL path in description must not trigger the guard."""
    cmd = (
        'gws calendar +insert --summary "ByteDance Interview" '
        '--start "2026-04-02T15:00:00+08:00" --end "2026-04-02T16:00:00+08:00" '
        '--description "Video interview\n\nLink: https://t.zijieimg.com/K9fImd7LblA/\n\nPrepare resume."'
    )
    assert tool._guard_command(cmd, str(tool.working_dir)) is None


def test_nonexistent_path_segment_after_newline_in_description_passes(tool):
    """A slash-prefixed word after a newline in --description must not be blocked.

    The real production failure: the ByteDance interview description contained
    a line that started with '/' (e.g. '/zoom available' or a path-like label).
    The path regex matched it as an absolute path, and since it doesn't exist on
    disk the guard fired — but non-existent paths in description text are not
    filesystem access attempts and must be allowed.
    """
    cmd = (
        'gws calendar +insert --summary "ByteDance Interview" '
        '--start "2026-04-02T15:00:00+08:00" --end "2026-04-02T16:00:00+08:00" '
        '--description "Video interview\n/zoom link available\nhttps://example.com/meeting"'
    )
    assert tool._guard_command(cmd, str(tool.working_dir)) is None


def test_path_inside_workspace_passes(tool, tmp_path):
    nested = tmp_path / "notes.txt"
    nested.touch()
    cmd = f"cat {nested}"
    assert tool._guard_command(cmd, str(tmp_path)) is None


def test_july_monthly_reconciliation_curl_passes(tool):
    command = "curl -fsS 'http://api:8000/api/analysis/monthly-event-reconciliation?month=2025-07'"

    assert tool._guard_command(command, str(tool.working_dir)) is None


@pytest.mark.parametrize("command", ["set -e", "export RESULT_PATH=report.json"])
def test_non_dumping_shell_setup_commands_pass(tool, command):
    assert tool._guard_command(command, str(tool.working_dir)) is None


@pytest.mark.asyncio
async def test_exec_receives_only_non_secret_allowlisted_environment(tool, monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_JWT_TOKEN", "gateway-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("ANALYST_RUNTIME_TEST_UNLISTED", "ordinary-parent-value")

    result = await tool.execute(
        "printf 'path=%s\\nsecret=%s\\nprovider=%s\\nunlisted=%s\\nhome=%s\\ntmpdir=%s\\n' "
        '"$PATH" "$GATEWAY_JWT_TOKEN" "$OPENAI_API_KEY" "$ANALYST_RUNTIME_TEST_UNLISTED" '
        '"$HOME" "$TMPDIR"'
    )

    assert "path=" in result
    assert "secret=\n" in result
    assert "provider=\n" in result
    assert "unlisted=\n" in result
    assert f"home={tmp_path}\n" in result
    assert f"tmpdir={tmp_path}\n" in result
    assert "gateway-secret" not in result
    assert "provider-secret" not in result
    assert "ordinary-parent-value" not in result


@pytest.mark.asyncio
async def test_exec_preserves_sealed_json_large_enough_for_artifact_evidence(tool):
    artifact_id = "monthly-event-reconciliation:2025-07:925eff63c622"
    command = (
        "python3 -c 'import json; print(json.dumps({"
        f'"artifact_id":"{artifact_id}",'
        '"release_gate":{"passed":True},"padding":"x"*10400}))\''
    )

    result = await tool.execute(command)

    assert len(result) > 10_000
    assert "... (truncated" not in result
    assert json.loads(result)["artifact_id"] == artifact_id
    assert AgentLoop._tool_result_evidence(result) == f"artifact_id={artifact_id}"


@pytest.mark.asyncio
async def test_exec_extracts_sealed_evidence_from_safely_truncated_json(tool):
    artifact_id = "monthly-event-reconciliation:2025-07:925eff63c622"
    command = (
        "python3 -c 'import json; print(json.dumps({"
        f'"artifact_id":"{artifact_id}",'
        '"release_gate":{"passed":True},"padding":"x"*30000}))\''
    )

    result = await tool.execute(command)

    assert "... (truncated" in result
    assert AgentLoop._tool_result_evidence(result) == f"artifact_id={artifact_id}"


def test_exec_extracts_case_neutral_pqc_analysis_identity() -> None:
    analysis_id = "pqc-defect-loss:" + "a" * 64
    result = json.dumps(
        {
            "schema_version": "linghui-pqc-defect-loss/v1",
            "status": "complete",
            "analysis_id": analysis_id,
            "population": {"final_disposition_net_loss_m": 1},
        }
    )

    assert AgentLoop._tool_result_evidence(result) == f"analysis_id={analysis_id}"


@pytest.mark.asyncio
async def test_exec_preserves_pqc_analysis_identity_when_result_is_truncated(tool) -> None:
    analysis_id = "pqc-defect-loss:" + "b" * 64
    command = (
        "python3 -c 'import json; print(json.dumps({"
        f'"analysis_id":"{analysis_id}",'
        '"schema_version":"linghui-pqc-defect-loss/v1",'
        '"status":"complete","padding":"x"*30000}))\''
    )

    result = await tool.execute(command)

    assert "... (truncated" in result
    assert AgentLoop._tool_result_evidence(result) == f"analysis_id={analysis_id}"


def test_output_scrubs_provider_and_channel_credentials(tool, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("ZAI_API_KEY", "bigmodel-secret")

    scrubbed = tool._scrub_output("deepseek-secret nvidia-secret telegram-secret bigmodel-secret")

    assert "deepseek-secret" not in scrubbed
    assert "nvidia-secret" not in scrubbed
    assert "telegram-secret" not in scrubbed
    assert "bigmodel-secret" not in scrubbed
    assert "[REDACTED:DEEPSEEK_API_KEY]" in scrubbed
    assert "[REDACTED:NVIDIA_API_KEY]" in scrubbed
    assert "[REDACTED:TELEGRAM_BOT_TOKEN]" in scrubbed
    assert "[REDACTED:ZAI_API_KEY]" in scrubbed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "set",
        "set | head -1",
        "export",
        "export -p",
        "declare -x",
        "declare -xp",
        "compgen -e",
    ],
)
async def test_environment_dump_builtins_are_blocked(tool, command):
    result = await tool.execute(command)

    assert result == "Error: Command blocked by safety guard (dangerous pattern detected)"


@pytest.mark.asyncio
async def test_restricted_exec_rejects_working_directory_outside_workspace(tool, tmp_path):
    outside = tmp_path.parent

    result = await tool.execute("pwd", working_dir=str(outside))

    assert result == "Error: Command blocked by safety guard (working dir outside workspace)"


@pytest.mark.asyncio
async def test_exec_cannot_overwrite_managed_long_term_memory_with_redirection(
    tool,
    tmp_path,
):
    memory_file = tmp_path / "memory" / "MEMORY.md"
    memory_file.parent.mkdir()
    memory_file.write_text("confirmed semantics", encoding="utf-8")

    result = await tool.execute("printf hacked > memory/MEMORY.md")

    assert result == "Error: Command blocked by safety guard (managed memory is read-only)"
    assert memory_file.read_text(encoding="utf-8") == "confirmed semantics"


@pytest.mark.asyncio
async def test_exec_cannot_overwrite_managed_long_term_memory_through_python_open(
    tool,
    tmp_path,
):
    memory_file = tmp_path / "memory" / "MEMORY.md"
    memory_file.parent.mkdir()
    memory_file.write_text("confirmed semantics", encoding="utf-8")

    result = await tool.execute("python3 -c \"open('memory/MEMORY.md', 'w').write('hacked')\"")

    assert result == "Error: Command blocked by safety guard (managed memory is read-only)"
    assert memory_file.read_text(encoding="utf-8") == "confirmed semantics"


@pytest.mark.asyncio
async def test_exec_cannot_overwrite_managed_long_term_memory_through_tee(
    tool,
    tmp_path,
):
    memory_file = tmp_path / "memory" / "MEMORY.md"
    memory_file.parent.mkdir()
    memory_file.write_text("confirmed semantics", encoding="utf-8")

    result = await tool.execute("printf hacked | tee memory/MEMORY.md")

    assert result == "Error: Command blocked by safety guard (managed memory is read-only)"
    assert memory_file.read_text(encoding="utf-8") == "confirmed semantics"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox boundary")
async def test_exec_macos_sandbox_keeps_managed_memory_read_only_for_dynamic_paths(
    tool,
    tmp_path,
):
    memory_file = tmp_path / "memory" / "MEMORY.md"
    memory_file.parent.mkdir()
    memory_file.write_text("confirmed semantics", encoding="utf-8")

    result = await tool.execute(
        "python3 -c \"p='memory/'+'MEMORY.md'; open(p, 'w').write('hacked')\""
    )

    assert "Exit code" in result
    assert memory_file.read_text(encoding="utf-8") == "confirmed semantics"


@pytest.mark.asyncio
async def test_exec_can_read_managed_long_term_memory(tool, tmp_path):
    memory_file = tmp_path / "memory" / "MEMORY.md"
    memory_file.parent.mkdir()
    memory_file.write_text("confirmed semantics", encoding="utf-8")

    result = await tool.execute("cat memory/MEMORY.md")

    assert result == "confirmed semantics"


# ---------------------------------------------------------------------------
# Should be blocked
# ---------------------------------------------------------------------------


def test_existing_path_outside_workspace_blocked(tool, tmp_path):
    """/etc/passwd exists and is outside workspace — must be blocked."""
    result = tool._guard_command("cat /etc/passwd", str(tmp_path))
    assert result is not None
    assert "outside working dir" in result


def test_traversal_blocked(tool, tmp_path):
    result = tool._guard_command("cat ../../etc/passwd", str(tmp_path))
    assert result is not None
    assert "path traversal" in result
