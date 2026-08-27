import json
import shutil
from pathlib import Path

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.profiles.manufacturing.memory import (
    CONFIRMED_SEMANTICS_END,
    CONFIRMED_SEMANTICS_START,
)
from nanobot.profiles.runtime import RuntimeProfiles
from nanobot.workspace import WorkspaceConfiguration

TEMPLATES = Path(__file__).resolve().parents[1] / "workspaces"


def test_workspace_without_configuration_uses_no_runtime_profiles(tmp_path: Path) -> None:
    configuration = WorkspaceConfiguration.load(tmp_path)

    assert configuration.runtime_profiles == ()


def test_workspace_configuration_selects_runtime_profiles(tmp_path: Path) -> None:
    (tmp_path / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_profiles": [
                    "manufacturing-semantics",
                    "manufacturing-semantics",
                ],
            }
        ),
        encoding="utf-8",
    )

    configuration = WorkspaceConfiguration.load(tmp_path)

    assert configuration.runtime_profiles == ("manufacturing-semantics",)


@pytest.mark.parametrize("schema_version", [None, 2, "1"])
def test_workspace_configuration_rejects_unsupported_schema_version(
    tmp_path: Path,
    schema_version: object,
) -> None:
    (tmp_path / "workspace.json").write_text(
        json.dumps({"schema_version": schema_version, "runtime_profiles": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema_version"):
        WorkspaceConfiguration.load(tmp_path)


def test_disabling_profile_with_persisted_state_fails_closed(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "MEMORY.md").write_text(
        f"generic\n\n{CONFIRMED_SEMANTICS_START}\nstate\n{CONFIRMED_SEMANTICS_END}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manufacturing-semantics.*persisted state"):
        RuntimeProfiles(tmp_path, WorkspaceConfiguration())


def test_workspace_configuration_selects_trusted_gateway_identity(tmp_path: Path) -> None:
    (tmp_path / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_profiles": [],
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
