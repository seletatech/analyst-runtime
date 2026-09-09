import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_has_no_vercel_ai_sdk_dependency_or_import() -> None:
    package_json = json.loads((ROOT / "bridge" / "package.json").read_text())
    declared = {
        **package_json.get("dependencies", {}),
        **package_json.get("devDependencies", {}),
    }
    assert "ai" not in declared
    assert not any(name.startswith("@ai-sdk/") for name in declared)

    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in {".js", ".mjs", ".ts", ".tsx"}:
            continue
        if any(part in {"node_modules", ".venv", "tests"} for part in path.parts):
            continue
        source = path.read_text(errors="ignore")
        if 'from "ai"' in source or "from 'ai'" in source or "@ai-sdk/" in source:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
