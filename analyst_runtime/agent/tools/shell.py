"""Shell execution tool."""

import asyncio
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from analyst_runtime.agent.tools.base import Tool

# Secret env vars whose *values* are scrubbed from all tool output.
# This is defence-in-depth: even if a new bypass of deny_patterns is found,
# the actual secret value never reaches the LLM or CloudWatch logs.
_SECRET_ENV_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLOUDFLARED_TUNNEL_TOKEN",
        "DEEPSEEK_API_KEY",
        "GATEWAY_JWT_TOKEN",
        "ANALYST_RUNTIME_GATEWAY_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "SANDBOX_INTERNAL_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_TOKEN",
        "WHATSAPP_BRIDGE_TOKEN",
    }
)

# The shell tool must never inherit provider, gateway, channel, or deployment
# credentials from the long-running agent process.  These are the only parent
# environment values needed by ordinary command-line programs.
_EXEC_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TERM",
        "TZ",
    }
)

# Keep this aligned with AgentLoop's inline tool-result budget.  The previous
# 10,000-character cap cut otherwise valid JSON responses before the agent loop
# could validate their release gate and attach safe artifact evidence.
MAX_TOOL_OUTPUT_CHARS = 16_000


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\|\s*rm\b",  # pipe to rm
            r";\s*rm\b",  # semicolon then rm
            r"&&\s*rm\b",  # && then rm
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"(?:^|[;&|]\s*)format\b",  # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
            r"\$\(",  # command substitution $(cmd)
            r"\$\(\(",  # arithmetic expansion $((expr))
            r"`[^`]*`",  # backtick substitution `cmd`
            r"\$\{",  # variable expansion ${cmd}
            r"\beval\b",  # eval builtin
            r"\bexec\b",  # exec builtin
            # --- env-dumping commands (secrets exfiltration risk) ---
            r"\bprintenv\b",  # printenv [VAR]
            r"(?<![a-zA-Z0-9_])env\b(?!\w)",  # standalone `env` command
            r"/proc/(?:self|[0-9]+)/environ",  # /proc/self/environ
            r"\bset(?=\s*(?:[;&|#\"']|$))",  # `set` with no options/arguments
            r"\bexport(?=\s*(?:[;&|#\"']|$))",  # `export` with no arguments
            r"\bexport\s+-[a-z]*p[a-z]*\b",  # export -p (including combined flags)
            r"\bdeclare\s+-[a-z]*x[a-z]*\b",  # declare -x / -xp
            r"\bcompgen\s+-[a-z]*e[a-z]*\b",  # compgen -e
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        try:
            process_group_options = (
                {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                if os.name == "nt"
                else {"start_new_session": True}
            )
            process_options = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": cwd,
                "env": self._subprocess_env(cwd),
                **process_group_options,
            }
            sandbox_profile = self._macos_memory_sandbox_profile(cwd)
            if sandbox_profile is not None:
                process = await asyncio.create_subprocess_exec(
                    "/usr/bin/sandbox-exec",
                    "-p",
                    sandbox_profile,
                    "/bin/sh",
                    "-c",
                    command,
                    **process_options,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    **process_options,
                )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                await self._terminate_process_tree(process)
                return f"Error: Command timed out after {self.timeout} seconds"
            except asyncio.CancelledError:
                await self._terminate_process_tree(process)
                raise

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            if process.returncode != 0:
                output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            # Truncate very long output
            max_len = MAX_TOOL_OUTPUT_CHARS
            if len(result) > max_len:
                result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

            return self._scrub_output(result)

        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        if os.name == "nt":
            try:
                terminator = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await terminator.wait()
            except OSError:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return

        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            if os.name == "nt":
                process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    return
            await asyncio.wait_for(process.wait(), timeout=2.0)

    def _subprocess_env(self, cwd: str) -> dict[str, str]:
        """Build the minimal non-secret environment exposed to executed commands."""
        env = {key: value for key in _EXEC_ENV_ALLOWLIST if (value := os.environ.get(key))}
        env.setdefault("PATH", os.defpath)

        workspace = Path(self.working_dir or cwd).resolve()
        temp_dir = workspace / "tmp_analysis"
        env["HOME"] = str(workspace)
        env["TMPDIR"] = str(temp_dir if temp_dir.is_dir() else workspace)
        return env

    def _scrub_output(self, output: str) -> str:
        """Redact injected secret values from output before returning to the LLM."""
        for key in _SECRET_ENV_VARS:
            val = os.environ.get(key, "")
            if val:
                output = output.replace(val, f"[REDACTED:{key}]")
        return output

    def _macos_memory_sandbox_profile(self, cwd: str) -> str | None:
        """Return a macOS sandbox profile that makes managed memory read-only."""
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            return None
        workspace = Path(self.working_dir or cwd).resolve()
        memory_file = (workspace / "memory" / "MEMORY.md").resolve()
        memory_dir = memory_file.parent
        confirmation_journal = (memory_dir / "confirmation-intents").resolve()

        def literal(path: Path) -> str:
            return f"(literal {quoted(path)})"

        def quoted(path: Path) -> str:
            escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'

        return (
            "(version 1)"
            "(allow default)"
            f"(deny file-write* {literal(memory_file)})"
            f"(deny file-write* (subpath {quoted(confirmation_journal)}))"
            f"(deny file-write-unlink {literal(memory_dir)})"
        )

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        if self._targets_managed_memory_write(cmd, cwd):
            return "Error: Command blocked by safety guard (managed memory is read-only)"

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()
            workspace_path = Path(self.working_dir or os.getcwd()).resolve()
            if cwd_path != workspace_path and workspace_path not in cwd_path.parents:
                return "Error: Command blocked by safety guard (working dir outside workspace)"

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            # Only match absolute paths — avoid false positives on relative
            # paths like ".venv/bin/python" where "/bin/python" would be
            # incorrectly extracted by the old pattern.
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)

            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if (
                    p.is_absolute()
                    and p.exists()
                    and workspace_path not in p.parents
                    and p != workspace_path
                ):
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    def _targets_managed_memory_write(self, command: str, cwd: str) -> bool:
        """Recognize obvious writes to project-managed semantic memory."""
        workspace = Path(self.working_dir or cwd).resolve()
        managed = workspace / "memory" / "MEMORY.md"
        try:
            relative = managed.relative_to(Path(cwd).resolve())
        except ValueError:
            relative = managed
        unquoted = command.replace('"', "").replace("'", "")
        relative_text = str(relative).replace("\\", "/")
        targets = {
            str(managed).replace("\\", "/"),
            relative_text,
            f"./{relative_text}",
        }
        normalized = unquoted.replace("\\", "/")
        journal_targets = {
            str(workspace / "memory" / "confirmation-intents").replace("\\", "/"),
            "memory/confirmation-intents",
            "./memory/confirmation-intents",
        }
        journal_is_referenced = any(target in normalized for target in journal_targets)
        journal_redirection = journal_is_referenced and bool(
            re.search(r">>?(?:\s*)[^\n;&|]*memory/confirmation-intents", normalized)
        )
        if journal_redirection:
            return True
        if any(
            re.search(rf">>?(?:\s*){re.escape(target)}(?:\s|$|[;&|])", normalized)
            for target in targets
        ):
            return True
        if not journal_is_referenced and not any(target in normalized for target in targets):
            return False
        write_intents = (
            r"\bopen\s*\([^)]*,\s*(?:[wax]|r\+)",
            r"\b(?:write_text|write_bytes|writefile|appendfile|unlink|remove|rename|replace)\s*\(",
            r"\b(?:tee|truncate|touch|rm|unlink|shred|sponge|chmod|chown|cp|mv|install|ln)\b",
            r"\b(?:sed|perl)\b[^\n]*(?:-i|-pi)\b",
        )
        has_write_intent = any(
            re.search(pattern, normalized, re.IGNORECASE) for pattern in write_intents
        )
        return has_write_intent and (
            journal_is_referenced or any(target in normalized for target in targets)
        )
