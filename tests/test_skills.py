from analyst_runtime.agent.skills import SkillsLoader


def test_workspace_skill_overrides_builtin(tmp_path) -> None:
    workspace_skill = tmp_path / "workspace" / "skills" / "demo" / "SKILL.md"
    builtin_skill = tmp_path / "builtin" / "demo" / "SKILL.md"
    workspace_skill.parent.mkdir(parents=True)
    builtin_skill.parent.mkdir(parents=True)
    workspace_skill.write_text("workspace")
    builtin_skill.write_text("builtin")

    loader = SkillsLoader(tmp_path / "workspace", builtin_skills_dir=tmp_path / "builtin")

    assert loader.load_skill("demo") == "workspace"
    assert loader.list_skills(filter_unavailable=False) == [
        {"name": "demo", "path": str(workspace_skill), "source": "workspace"}
    ]
