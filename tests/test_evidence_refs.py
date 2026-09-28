import hashlib
import json
from pathlib import Path

from analyst_runtime.agent.evidence_refs import collect_evidence_refs


def test_only_successful_existing_data_files_are_reported(tmp_path: Path) -> None:
    page = tmp_path / "data/production-records/2025-10/2025.10 HUD复卷生产记录表/page-0002.json"
    book = tmp_path / "data/品质部内部资料/品质报表/结果.xlsx"
    stale = tmp_path / "data/品质部内部资料/品质报表/旧结果.xlsx"
    page.parent.mkdir(parents=True)
    book.parent.mkdir(parents=True)
    page.write_text("{}")
    book.write_bytes(b"xlsx")
    stale.write_bytes(b"xlsx")
    artifact = {
        "schema_version": "linghui-pqc-defect-loss/v1",
        "status": "complete",
        "request": {},
        "population": {},
        "source_coverage": {"workbooks": [{"path": "品质报表/结果.xlsx"}]},
    }
    digest = hashlib.sha256(json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    analysis_id = f"pqc-defect-loss:{digest}"
    artifact["analysis_id"] = analysis_id
    artifact_file = tmp_path / "artifacts/analyses/pqc-defect-loss" / f"{digest}.json"
    artifact_file.parent.mkdir(parents=True)
    artifact_file.write_text(json.dumps(artifact))
    events = [
        {"type": "tool_result", "status": "completed", "tool_name": "read_file", "tool_input": {"path": str(page)}, "content": "{}"},
        {"type": "tool_result", "status": "completed", "tool_name": "exec", "tool_input": {"command": "python bin/analyze_pqc_defect_loss.py"}, "content": f"analysis_id={analysis_id}"},
        {"type": "tool_result", "status": "completed", "tool_name": "exec", "tool_input": {"command": "cat tmp_analysis/xlsx-evidence.md"}, "content": f"Fact | Raw value | File\n批号 | A | {stale} | Sheet1!A1"},
        {"type": "tool_result", "status": "failed", "tool_name": "read_file", "tool_input": {"path": str(book)}, "content": "error"},
        {"type": "tool_result", "status": "completed", "tool_name": "list_dir", "tool_input": {"path": str(page.parent)}, "content": str(page)},
    ]
    assert collect_evidence_refs(events, tmp_path) == [
        {"kind": "ocr", "path": str(page.relative_to(tmp_path))},
        {"kind": "excel", "path": str(book.relative_to(tmp_path))},
    ]
    assert collect_evidence_refs([], tmp_path, f"**analysis_id**: `{analysis_id}`") == [
        {"kind": "excel", "path": str(book.relative_to(tmp_path))},
    ]
    assert collect_evidence_refs([], tmp_path, f"[打开](/data?path={stale.relative_to(tmp_path)})") == []
