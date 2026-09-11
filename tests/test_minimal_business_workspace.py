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
        "workspace/README.md",
        "workspace/SOUL.md",
        "workspace/bin/analyze_pqc_defect_loss.py",
        "workspace/skills/xlsx/capture_evidence.py",
        "workspace/skills/xlsx/LICENSE",
        "workspace/skills/xlsx/SKILL.md",
        "workspace/workspace.json",
        "workspace/data/README.md",
    }
    assert (WORKSPACE / "skills/xlsx/SKILL.md").is_file()
    xlsx_skill = (WORKSPACE / "skills/xlsx/SKILL.md").read_text(encoding="utf-8")
    assert "A match only locates an anchor" in xlsx_skill
    assert "Never load an entire worksheet" in xlsx_skill
    assert "Fact | Raw value | File | Sheet!Cell | Scope" in xlsx_skill
    assert "verbatim into the user-visible answer" in xlsx_skill
    assert "never derive a date from an identifier" in xlsx_skill
    assert "Do not create an artifact unless the user requested one" in xlsx_skill
    assert "product-named inspection/shipment reports" in xlsx_skill
    assert "make this the first data command" in xlsx_skill
    assert "do not run `excel-mcp` or this collector again" in xlsx_skill
    assert "another conversation cannot leak" in xlsx_skill
    assert "tmp_analysis/xlsx-evidence.md" in xlsx_skill
    assert "skills/xlsx/capture_evidence.py" in xlsx_skill
    assert "one raw value and one exact cell per row" in xlsx_skill
    assert "Do not locate headers" in xlsx_skill
    assert "`filter_rows`" in xlsx_skill
    collector = (WORKSPACE / "skills/xlsx/capture_evidence.py").read_text(encoding="utf-8")
    assert '"has_header": False' in collector
    assert "every measurement" in xlsx_skill
    assert "absent from the final evidence table" in xlsx_skill
    assert "captures a labeled `日期`" in xlsx_skill
    assert "`report-level` / `报告级`" in xlsx_skill
    assert "complete identifier verbatim" in xlsx_skill
    assert "append the complete ledger" in xlsx_skill
    assert "aggregate or multi-record analysis, never paste the full ledger" in xlsx_skill
    assert "final answer must call" in xlsx_skill
    assert not (WORKSPACE / "bin/xlsxsearch.py").exists()
    assert not (WORKSPACE / "bin/xlsxread.py").exists()
    assert {path for path in tracked if path.endswith(".py")} == {
        "workspace/bin/analyze_pqc_defect_loss.py",
        "workspace/skills/xlsx/capture_evidence.py",
    }


def test_xlsx_evidence_capture_keeps_each_cell_separate(tmp_path: Path) -> None:
    source = tmp_path / "hit.json"
    ledger = tmp_path / "evidence.md"
    source.write_text(
        json.dumps(
            {
                "filepath": "/workspace/book.xlsx",
                "matches": [
                    {
                        "sheet_name": "PQC",
                        "cell": "F242",
                        "value": "batch-1",
                        "context": [
                            {"cell": "AB242", "value": 1077},
                            {"cell": "AC242", "value": 1096},
                            {"cell": "AD242", "value": 1088},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ledger.write_text("Fact | Raw value | File | Sheet!Cell | Scope\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(WORKSPACE / "skills/xlsx/capture_evidence.py"),
            str(source),
            str(ledger),
            "record",
        ],
        check=True,
    )

    evidence = ledger.read_text(encoding="utf-8")
    for cell, value in (
        ("F242", "batch-1"),
        ("AB242", "1077"),
        ("AC242", "1096"),
        ("AD242", "1088"),
    ):
        assert f"PQC!{cell} | {value}" in evidence


def test_xlsx_evidence_capture_maps_filter_columns_to_cells(tmp_path: Path) -> None:
    source = tmp_path / "row.json"
    ledger = tmp_path / "evidence.md"
    source.write_text(
        json.dumps(
            {
                "filepath": "/workspace/book.xlsx",
                "sheet_name": "PQC",
                "range": "A242:AE242",
                "rows": [
                    {
                        "column_6": "batch-1",
                        "column_22": "100.00%",
                        "column_28": 1077,
                        "column_29": 1096,
                        "column_30": 1088,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ledger.write_text("Fact | Raw value | File | Sheet!Cell | Scope\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(WORKSPACE / "skills/xlsx/capture_evidence.py"),
            str(source),
            str(ledger),
            "record",
        ],
        check=True,
    )

    evidence = ledger.read_text(encoding="utf-8")
    for cell, value in (
        ("F242", "batch-1"),
        ("V242", "100.00%"),
        ("AB242", "1077"),
        ("AC242", "1096"),
        ("AD242", "1088"),
    ):
        assert f"PQC!{cell} | {value}" in evidence


def test_xlsx_evidence_capture_keeps_only_matched_repeated_group(tmp_path: Path) -> None:
    source = tmp_path / "row.json"
    ledger = tmp_path / "evidence.md"
    source.write_text(
        json.dumps(
            {
                "filepath": "/workspace/book.xlsx",
                "sheet_name": "OQC",
                "range": "A7:AM7",
                "rows": [
                    {
                        "column_27": "another-batch",
                        "column_35": "batch-1",
                        "column_36": "无接头",
                        "column_37": "shipment-1",
                        "column_39": "next-batch",
                    }
                ],
                "_column_bounds": [35, 38],
                "_headers": {"AI": "批号", "AJ": "外观", "AK": "出货批号"},
                "_header_row": 1,
                "_report_level_rule": True,
            }
        ),
        encoding="utf-8",
    )
    ledger.write_text("Fact | Raw value | File | Sheet!Cell | Scope\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(WORKSPACE / "skills/xlsx/capture_evidence.py"),
            str(source),
            str(ledger),
            "record",
        ],
        check=True,
    )

    evidence = ledger.read_text(encoding="utf-8")
    assert "OQC!AI7 | batch-1" in evidence
    assert "OQC!AJ7 | 无接头" in evidence
    assert "OQC!AK7 | shipment-1" in evidence
    assert "OQC!AI1 | 批号" in evidence
    assert "报告级" in evidence
    assert "another-batch" not in evidence
    assert "next-batch" not in evidence


def test_xlsx_evidence_capture_accepts_cli_prefix(tmp_path: Path) -> None:
    source = tmp_path / "hit.json"
    ledger = tmp_path / "evidence.md"
    source.write_text(
        'notice from cli\n{"filepath":"/workspace/book.xlsx","matches":'
        '[{"sheet_name":"OQC","cell":"P5","value":"04-07-26"}]}\n',
        encoding="utf-8",
    )
    ledger.write_text("Fact | Raw value | File | Sheet!Cell | Scope\n", encoding="utf-8")

    subprocess.run(
        [
            "python3",
            str(WORKSPACE / "skills/xlsx/capture_evidence.py"),
            str(source),
            str(ledger),
            "record",
        ],
        check=True,
    )

    assert "OQC!P5 | 04-07-26" in ledger.read_text(encoding="utf-8")


def test_prompt_is_management_focused_without_fixed_metric_checklist() -> None:
    prompt = "\n".join(
        (WORKSPACE / name).read_text(encoding="utf-8") for name in ("SOUL.md", "AGENTS.md")
    )
    for required in ("管理决策", "业务结果", "异常", "行动"):
        assert required in prompt
    assert "最终回答前必须重新读取" in prompt
    assert "在检索 `data/` 前先读取并执行 `skills/xlsx/SKILL.md`" in prompt
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
