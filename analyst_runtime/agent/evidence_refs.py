"""Small, validated file references from one turn's successful tool results."""

import re
from pathlib import Path
from typing import Any
from analyst_runtime.agent.analysis_context import AnalysisArtifactError, AnalysisArtifactStore

OCR_PATH = re.compile(
    r"data/production-records/\d{4}-\d{2}/[^\n\r\"'<>|`]+?/page-\d{4}\.json"
)
EXCEL_PATH = re.compile(r"data/[^\n\r\"'<>|`]+?\.xlsx?\b", re.IGNORECASE)


def collect_evidence_refs(events: list[dict[str, Any]], workspace: Path, answer: str = "") -> list[dict[str, str]]:
    data_root = (workspace / "data").resolve()
    found: dict[str, dict[str, str]] = {}
    artifacts = AnalysisArtifactStore(workspace)

    def add(path: str) -> None:
        for kind, pattern in (("ocr", OCR_PATH), ("excel", EXCEL_PATH)):
            for match in pattern.finditer(path):
                relative = match.group().rstrip(" ,.;)]}")
                target = (workspace / relative).resolve()
                if target.is_relative_to(data_root) and target.is_file():
                    found[relative] = {"kind": kind, "path": relative}

    def add_artifact(analysis_id: str) -> None:
        try:
            artifact = artifacts.load(analysis_id)
        except AnalysisArtifactError:
            return
        coverage = artifact.get("source_coverage")
        if isinstance(coverage, dict):
            for item in coverage.get("workbooks", []):
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    path = item["path"]
                    if artifact.get("schema_version") == "linghui-pqc-defect-loss/v1":
                        # ponytail: this artifact stores paths below 品质部内部资料; add schema mappings only when needed.
                        path = f"data/品质部内部资料/{path}"
                    add(path)

    for event in events:
        if event.get("type") != "tool_result" or event.get("status") != "completed":
            continue
        name = event.get("tool_name")
        inputs = event.get("tool_input") or {}
        if not isinstance(inputs, dict):
            continue
        if name == "read_file":
            add(str(inputs.get("path") or ""))
        elif name == "exec":
            pointer = str(event.get("content") or "")
            if pointer.startswith("analysis_id="):
                add_artifact(pointer.removeprefix("analysis_id="))
    # A cited, content-verified artifact can be reused without a tool call this turn.
    # ponytail: only recorded reads and artifact coverage are attributable; script-internal reads need a source manifest.
    for analysis_id in re.findall(r"analysis_id[^\n]{0,32}?([a-z][a-z0-9-]*:[0-9a-f]{64})", answer):
        add_artifact(analysis_id)
    return list(found.values())
