import json
import shutil
from pathlib import Path

from analyst_runtime.agent.context import ContextBuilder
from analyst_runtime.workspace import WorkspaceConfiguration

TEMPLATES = Path(__file__).resolve().parents[1] / "workspaces"


def test_workspace_configuration_selects_trusted_gateway_identity(tmp_path: Path) -> None:
    (tmp_path / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "trusted_gateway": {
                    "runtime": "customer-agent",
                    "project_id": "customer-project",
                },
            }
        ),
        encoding="utf-8",
    )

    configuration = WorkspaceConfiguration.load(tmp_path)

    assert configuration.trusted_gateway is not None
    assert configuration.trusted_gateway.runtime == "customer-agent"
    assert configuration.trusted_gateway.project_id == "customer-project"


def test_workspace_templates_select_distinct_system_prompt_roles(tmp_path: Path) -> None:
    analyst_workspace = shutil.copytree(TEMPLATES / "data-analyst", tmp_path / "analyst")
    executive_workspace = shutil.copytree(TEMPLATES / "executive", tmp_path / "executive")
    analyst_prompt = ContextBuilder(analyst_workspace, minimal=True).build_system_prompt()
    executive_prompt = ContextBuilder(executive_workspace, minimal=True).build_system_prompt()

    assert "你是一名数据分析师" in analyst_prompt
    assert "你是一名企业经营者" not in analyst_prompt
    assert "你是一名企业经营者" in executive_prompt
    assert "你是一名数据分析师" not in executive_prompt
