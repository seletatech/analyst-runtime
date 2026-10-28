import io
import json
from zipfile import ZipFile

import pytest

from analyst_runtime.agent.tools.spreadsheets import analyze_workbook, inspect_workbook


def book():
    b = io.BytesIO()
    with ZipFile(b, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Data" sheetId="1" r:id="r1"/><sheet name="Hidden" state="hidden" sheetId="2" r:id="r2"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Target="worksheets/sheet2.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Batch</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>B1</t></is></c><c r="B2"><v>100</v></c><c r="C2"><f>B2*0.5</f><v>99</v></c></row><row r="3" hidden="1"><c r="A3" t="inlineStr"><is><t>B2</t></is></c><c r="B3"><v>100</v></c><c r="C3"><v>0</v></c></row><row r="4"><c r="A4" t="inlineStr"><is><t>B3</t></is></c><c r="B4"><v>100</v></c><c r="C4"><f>UNKNOWN(B4)</f><v>100</v></c></row></sheetData><mergeCells><mergeCell ref="A1:C1"/></mergeCells></worksheet>',
        )
        z.writestr(
            "xl/worksheets/sheet2.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1"><v>17</v></c></row></sheetData></worksheet>',
        )
    return b.getvalue()


def plan():
    return {
        "sheets": {
            "Data": {
                "headers": [1],
                "group_by": ["A"],
                "metrics": [
                    {
                        "name": "Yield",
                        "operation": "ratio",
                        "numerator": "C",
                        "denominator": "B",
                        "unit": "fraction",
                    }
                ],
            }
        }
    }


def test_full_inventory_hidden_formula_and_exact_row_accounting():
    inventory = inspect_workbook(book())
    assert len(inventory["sheets"]) == 2
    assert inventory["sheets"][0]["hidden_rows"] == [3]
    result = analyze_workbook(book(), plan())
    assert result["counts"] == {"computed": 2, "pending": 2, "excluded": 1}
    assert len(result["ledger"]) == 5
    assert result["groups"][0]["metrics"]["Yield"] == 0.5
    assert result["groups"][1]["metrics"]["Yield"] == 0
    assert any(r["sheet"] == "Hidden" and r["status"] == "pending" for r in result["ledger"])
    assert any("C4" in r.get("reason", "") for r in result["ledger"])
    assert result["whole_population_complete"] is False


def test_no_plan_keeps_every_row_pending_and_invalid_plan_rejected():
    r = analyze_workbook(book(), {"sheets": {}})
    assert r["counts"] == {"computed": 0, "pending": 5, "excluded": 0}
    p = plan()
    p["sheets"]["Data"]["exclude"] = [{"row": 2}]
    with pytest.raises(ValueError):
        analyze_workbook(book(), p)
    p = plan()
    p["sheets"]["Data"]["metrics"][0]["numerator"] = "Z"
    r = analyze_workbook(book(), p)
    assert r["counts"]["pending"] == 4


@pytest.mark.asyncio
async def test_authorized_read_gate_binds_input_and_export_and_conversation(tmp_path):
    import hashlib

    from analyst_runtime.agent.tools.filesystem import ReadFileTool

    file = tmp_path / "uploads/user/conversation/version/new.xlsx"
    file.parent.mkdir(parents=True)
    file.write_bytes(book())
    (file.parent / ".manifest.json").write_text(
        json.dumps(
            dict(
                schema_version="analyst-runtime-workspace-upload/v1",
                user_id="user",
                conversation_id="conversation",
                version="version",
                workspace_path=str(file),
                content_sha256=hashlib.sha256(book()).hexdigest(),
            )
        )
    )
    tool = ReadFileTool(allowed_dir=tmp_path, audit_results=True)
    tool.set_conversation_context("wrong")
    tool.begin_spreadsheet_turn([{"role": "user", "content": f"统计 {file}"}])
    assert json.loads(await tool.execute(str(file), spreadsheet_plan=plan()))["status"] == "denied"
    assert tool.spreadsheet_delivery()[1]
    tool.set_conversation_context("conversation")
    r = json.loads(await tool.execute(str(file), spreadsheet_plan=plan()))
    assert r["counts"]["pending"] == 2
    report, hint = tool.spreadsheet_delivery()
    assert hint is None and "C4" in report and "Hidden" in report
    assert len(tool.spreadsheet_artifacts()) == 2
    artifact = next(
        __import__("pathlib").Path(p) for p in tool.spreadsheet_artifacts() if p.endswith(".csv")
    )
    artifact.write_text("forged")
    assert tool.spreadsheet_delivery()[1]
    await tool.execute(str(file), spreadsheet_plan=plan())
    file.write_bytes(b"changed input")
    assert tool.spreadsheet_delivery()[1]


def test_filters_account_for_missing_values_and_do_not_delete_zero():
    from analyst_runtime.agent.tools.spreadsheets import passes_filters

    cells = {"A2": {"value": None}, "A3": {"value": 0}, "A4": {"value": "2026/09/18"}}
    with pytest.raises(ValueError, match="A2"):
        passes_filters(
            cells,
            2,
            [
                {
                    "column": "A",
                    "operation": "date_range",
                    "start": "2026-09-01",
                    "end_exclusive": "2026-10-01",
                }
            ],
        )
    assert passes_filters(cells, 3, [{"column": "A", "operation": "eq", "value": 0}])
    assert passes_filters(
        cells,
        4,
        [
            {
                "column": "A",
                "operation": "date_range",
                "start": "2026-09-01",
                "end_exclusive": "2026-10-01",
            }
        ],
    )
    assert not passes_filters(
        cells,
        4,
        [
            {
                "column": "A",
                "operation": "date_range",
                "start": "2026-10-01",
                "end_exclusive": "2026-11-01",
            }
        ],
    )


def test_formula_cache_never_replaces_unsupported_zero_or_cross_row_formula():
    from analyst_runtime.agent.tools.spreadsheets import number

    cells = {
        "A2": {"value": 10},
        "B2": {"formula": 'IFERROR(A2*2,"-")', "cached": "999"},
        "C2": {"formula": "SUM(A2:B2)"},
        "D2": {"formula": "A3", "cached": "10"},
        "E2": {"formula": "1/0", "cached": "0"},
    }
    assert number(cells, "B2") == 20
    assert number(cells, "C2") == 30
    with pytest.raises(ValueError):
        number(cells, "D2")
    with pytest.raises(ValueError):
        number(cells, "E2")
