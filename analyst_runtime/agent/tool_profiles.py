"""Profile-aware construction of Analyst Runtime tool registries."""

from pathlib import Path

from analyst_runtime.agent.skills import get_skill_read_roots
from analyst_runtime.agent.tools.filesystem import (
    AppendFileTool,
    EditFileTool,
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
)
from analyst_runtime.agent.tools.firecrawl import (
    FirecrawlBrowserTool,
    FirecrawlScrapeTool,
    FirecrawlSearchTool,
)
from analyst_runtime.agent.tools.registry import ToolRegistry
from analyst_runtime.agent.tools.shell import ExecTool
from analyst_runtime.agent.tools.web import WebFetchTool
from analyst_runtime.config.schema import ExecToolConfig


def register_workspace_analysis_tools(
    tools: ToolRegistry,
    *,
    workspace: Path,
    exec_config: ExecToolConfig,
    restrict_to_workspace: bool,
    audit_reads: bool = False,
) -> None:
    """Register the shared file, exec, and retrieval tools for an analysis agent."""
    allowed_dir = workspace if restrict_to_workspace else None
    allowed_read_dirs = get_skill_read_roots(workspace) if restrict_to_workspace else None
    tools.register(
        ReadFileTool(
            allowed_dir=allowed_dir,
            allowed_dirs=allowed_read_dirs,
            audit_results=audit_reads,
        )
    )
    tools.register(WriteFileTool(allowed_dir=allowed_dir))
    tools.register(AppendFileTool(allowed_dir=allowed_dir))
    tools.register(PatchFileTool(allowed_dir=allowed_dir))
    tools.register(EditFileTool(allowed_dir=allowed_dir))
    tools.register(ListDirTool(allowed_dir=allowed_dir, allowed_dirs=allowed_read_dirs))
    tools.register(
        ExecTool(
            working_dir=str(workspace),
            timeout=exec_config.timeout,
            restrict_to_workspace=restrict_to_workspace,
        )
    )
    tools.register(WebFetchTool())
    tools.register(FirecrawlSearchTool())
    tools.register(FirecrawlScrapeTool())
    tools.register(FirecrawlBrowserTool())
