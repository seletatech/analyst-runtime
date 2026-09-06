from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = REPO_ROOT / "workspace"


def _is_linghui_workspace() -> bool:
    config_path = WORKSPACE / "workspace.json"
    if not config_path.is_file():
        return False
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return config.get("trusted_gateway", {}).get("project_id") == "linghui-ai-suite"


pytestmark = pytest.mark.skipif(
    not _is_linghui_workspace(),
    reason="requires the consuming Linghui product workspace",
)
DATA_ROOT = WORKSPACE / "data"
HYDRATED_BUSINESS_DATA = all(
    path.exists()
    for path in (
        DATA_ROOT / "manifest.json",
        DATA_ROOT / "procurement",
        DATA_ROOT / "quality",
        DATA_ROOT / "2026-06" / "manifest.json",
    )
)

FORBIDDEN_DATA_TERMS = {
    "ambiguous",
    "artifact",
    "blank",
    "confidence",
    "crossed_out",
    "needs_review",
    "provenance",
    "provider",
    "release_id",
    "rescan_recommended",
    "review_packet",
    "review_status",
    "risk_level",
    "risk_reasons",
    "schema_version",
    "source_manifest",
    "value_id",
}


def _walk_json(value: object, path: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.casefold()
            assert not any(term in normalized for term in FORBIDDEN_DATA_TERMS), (
                f"internal key {key!r} remains in {path}"
            )
            _walk_json(child, path)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, path)
    elif isinstance(value, str):
        normalized = value.casefold()
        assert not any(term in normalized for term in FORBIDDEN_DATA_TERMS), (
            f"internal value {value!r} remains in {path}"
        )


def test_workspace_contains_only_minimal_prompt_and_business_data() -> None:
    entries = {path.name for path in WORKSPACE.iterdir()}
    assert {"AGENTS.md", "SOUL.md", "workspace.json", "data"}.issubset(entries)
    tracked = set(
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "workspace"],
            cwd=REPO_ROOT,
            text=True,
        ).splitlines()
    )
    assert tracked == {
        "workspace/.gitignore",
        "workspace/AGENTS.md",
        "workspace/SOUL.md",
        "workspace/bin/analyze_pqc_defect_loss.py",
        "workspace/workspace.json",
        "workspace/data/README.md",
    }
    assert not (WORKSPACE / "skills").exists()
    assert list(WORKSPACE.rglob("*.py")) == [WORKSPACE / "bin/analyze_pqc_defect_loss.py"]


def test_prompt_is_management_focused_without_fixed_metric_checklist() -> None:
    prompt = "\n".join(
        (WORKSPACE / name).read_text(encoding="utf-8") for name in ("SOUL.md", "AGENTS.md")
    )
    for required in ("管理决策", "业务结果", "异常", "行动"):
        assert required in prompt
    for forbidden in (
        "monthly-event-reconciliation",
        "metric_contract",
        "release gate",
        "review_packet",
        "source_manifest",
        "needs_review",
        "rescan_recommended",
    ):
        assert forbidden not in prompt


@pytest.mark.skipif(
    not HYDRATED_BUSINESS_DATA,
    reason="sealed business snapshot is not hydrated in this checkout",
)
def test_production_data_contains_only_business_json() -> None:
    production_roots = [
        path
        for path in DATA_ROOT.iterdir()
        if path.is_dir() and len(path.name) == 7 and path.name[4] == "-"
    ]
    files = [path for root in production_roots for path in root.rglob("*") if path.is_file()]
    files.append(DATA_ROOT / "manifest.json")
    assert files
    assert all(path.suffix == ".json" for path in files)

    for path in files:
        _walk_json(json.loads(path.read_text(encoding="utf-8")), path)


@pytest.mark.skipif(
    not HYDRATED_BUSINESS_DATA,
    reason="sealed business snapshot is not hydrated in this checkout",
)
def test_raw_supplemental_sources_are_available() -> None:
    manifest = json.loads((DATA_ROOT / "manifest.json").read_text(encoding="utf-8"))
    source_directories = {item["directory"] for item in manifest["supplemental_sources"]}
    assert source_directories == {"procurement", "quality"}
    assert any((DATA_ROOT / "procurement").rglob("*"))
    assert any((DATA_ROOT / "quality").rglob("*"))


@pytest.mark.skipif(
    not HYDRATED_BUSINESS_DATA,
    reason="sealed business snapshot is not hydrated in this checkout",
)
def test_june_is_directly_navigable_by_month_and_document() -> None:
    month_root = DATA_ROOT / "2026-06"
    manifest = json.loads((month_root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["month"] == "2026-06"
    assert manifest["document_count"] == 2
    assert manifest["page_count"] == 187

    page_files = list(month_root.glob("*/page-*.json"))
    assert len(page_files) == 187
