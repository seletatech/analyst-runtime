import analyst_runtime.agent.skills as skills_module
from analyst_runtime.agent.skills import SkillsLoader, get_skill_read_roots


def test_only_workspace_and_builtin_skills_are_loaded(tmp_path, monkeypatch) -> None:
    workspace_skill = tmp_path / "workspace" / "skills" / "demo" / "SKILL.md"
    builtin_skill = tmp_path / "builtin" / "demo" / "SKILL.md"
    private_skill = tmp_path / "mesu" / "skills" / "private" / "SKILL.md"
    workspace_skill.parent.mkdir(parents=True)
    builtin_skill.parent.mkdir(parents=True)
    private_skill.parent.mkdir(parents=True)
    workspace_skill.write_text("workspace")
    builtin_skill.write_text("builtin")
    private_skill.write_text("private")
    monkeypatch.setattr(skills_module, "MESU_SKILLS_DIR", private_skill.parents[1], raising=False)

    loader = SkillsLoader(tmp_path / "workspace", builtin_skills_dir=tmp_path / "builtin")

    assert loader.load_skill("demo") == "workspace"
    assert loader.list_skills(filter_unavailable=False) == [
        {"name": "demo", "path": str(workspace_skill), "source": "workspace"}
    ]
    assert private_skill.parents[1] not in get_skill_read_roots(tmp_path / "workspace")
