"""Bounded workbook inventory and row-accounted arithmetic, inside read_file only."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from zipfile import ZipFile

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MAX_CELLS = 250000
MAX_ROWS = 50000


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def workbook(content):
    with ZipFile(io.BytesIO(content)) as z:
        entries = z.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 200 * 1024 * 1024:
            raise ValueError(
                "Workbook exceeds 10000 members / 200 MiB expanded limit; no complete statistics available"
            )
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or any(
            e.flag_bits & 1 or e.file_size > 50 * 1024 * 1024 for e in entries
        ):
            raise ValueError("Encrypted, duplicate or oversized workbook members")

        def xml(name):
            raw = z.read(name)
            if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                raise ValueError("XML entities forbidden")
            return ET.fromstring(raw)

        strings = []
        if "xl/sharedStrings.xml" in names:
            strings = ["".join(n.itertext()) for si in xml("xl/sharedStrings.xml") for n in [si]]
        relations = {
            r.attrib["Id"]: r.attrib["Target"]
            for r in xml("xl/_rels/workbook.xml.rels")
            if r.attrib.get("TargetMode") != "External"
        }
        result = []
        total = 0
        sheet_names = set()
        for sheet in xml("xl/workbook.xml").find("s:sheets", NS):
            if sheet.attrib["name"] in sheet_names:
                raise ValueError("Duplicate worksheet name")
            sheet_names.add(sheet.attrib["name"])
            rid = sheet.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            target = relations.get(rid, "")
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if ".." in PurePosixPath(member).parts or not member.startswith("xl/"):
                raise ValueError("Unsafe worksheet target")
            tree = xml(member)
            cells = {}
            rows = {}
            hidden = []
            masters = {}
            for row in tree.findall("s:sheetData/s:row", NS):
                row_id = int(row.attrib["r"])
                current = {}
                if row_id in rows or not 1 <= row_id <= 1048576:
                    raise ValueError("Duplicate/invalid source row")
                if row.attrib.get("hidden") == "1":
                    hidden.append(row_id)
                for cell in row.findall("s:c", NS):
                    address = cell.attrib["r"]
                    if not re.fullmatch(r"[A-Z]{1,3}" + str(row_id), address) or address in cells:
                        raise ValueError("Duplicate/invalid source cell")
                    v = cell.findtext("s:v", None, NS)
                    kind = cell.attrib.get("t", "n")
                    f = cell.find("s:f", NS)
                    if f is not None:
                        formula = f.text
                        if f.attrib.get("t") == "shared":
                            index = f.attrib.get("si")
                            if formula:
                                masters[index] = (address, formula)
                            elif index in masters:
                                base, template = masters[index]
                                delta = row_id - int(re.search(r"\d+", base).group())
                                # Shared columns must be the same: other translations stay unresolved.
                                if (
                                    re.match("[A-Z]+", base).group()
                                    == re.match("[A-Z]+", address).group()
                                ):
                                    formula = re.sub(
                                        r"(\$?[A-Z]+)(\$?)(\d+)",
                                        lambda m: m[0] if m[2] else m[1] + str(int(m[3]) + delta),
                                        template,
                                    )
                        item = {"formula": formula, "cached": v}
                    elif kind == "s":
                        item = {"value": strings[int(v)] if v is not None else None}
                    elif kind == "inlineStr":
                        item = {
                            "value": "".join(cell.find("s:is", NS).itertext())
                            if cell.find("s:is", NS) is not None
                            else ""
                        }
                    elif kind in {"str", "e", "b", "d"}:
                        item = {"value": v, "type": kind}
                    else:
                        try:
                            item = {"value": float(v) if v is not None else None}
                        except ValueError:
                            item = {"value": v}
                    if item.get("value") not in (None, "") or "formula" in item:
                        cells[address] = item
                        current[address] = item
                        total += 1
                        if total > MAX_CELLS:
                            raise ValueError(
                                "Workbook exceeds 250000 populated cells; split the file; no complete statistics available"
                            )
                if current:
                    rows[row_id] = current
                if len(rows) > MAX_ROWS:
                    raise ValueError("Worksheet exceeds 50000 populated rows; split the file")
            result.append(
                {
                    "name": sheet.attrib["name"],
                    "state": sheet.attrib.get("state", "visible"),
                    "cells": cells,
                    "rows": rows,
                    "hidden_rows": hidden,
                    "merged_ranges": [
                        r.attrib["ref"] for r in tree.findall("s:mergeCells/s:mergeCell", NS)
                    ],
                    "drawing_present": tree.find("s:drawing", NS) is not None,
                    "hidden_columns": [
                        dict(c.attrib)
                        for c in tree.findall("s:cols/s:col", NS)
                        if c.attrib.get("hidden") == "1"
                    ],
                    "date_1904": (
                        xml("xl/workbook.xml").find("s:workbookPr", NS).attrib.get("date1904")
                        in {"1", "true"}
                    )
                    if xml("xl/workbook.xml").find("s:workbookPr", NS) is not None
                    else False,
                }
            )
        return result


def inspect_workbook(content, page=1):
    sheets = workbook(content)
    return {
        "schema": "spreadsheet-inventory/v1",
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "sheets": [
            {k: v for k, v in s.items() if k not in {"cells", "rows"}}
            | {
                "populated_rows": len(s["rows"]),
                "populated_cells": len(s["cells"]),
                "formula_cells": sum("formula" in c for c in s["cells"].values()),
                "page_count": max(1, (len(s["rows"]) + 99) // 100),
                "sample_rows": list(s["rows"].items())[(page - 1) * 100 : page * 100],
            }
            for s in sheets
        ],
        "notice": "Full populated-row inventory includes hidden rows/sheets. Samples and PDF pages are not complete statistics. Drawing text, external sources, units and business completeness are not certified. Pass spreadsheet_plan to classify every row and compute.",
    }


def number(cells, address, seen=None):
    seen = set() if seen is None else seen
    if address in seen or len(seen) > 40:
        raise ValueError(address + ": 循环引用或公式过于复杂")
    cell = cells.get(address, {})
    if "formula" not in cell:
        value = cell.get("value")
        if (
            cell.get("type")
            or isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
        ):
            raise ValueError(address + ": 数量缺失或不是有效数值")
        return value
    formula = cell["formula"]
    if not formula:
        raise ValueError(address + ": 共享公式无法可靠还原")
    formula = formula.lstrip("=")
    seen = seen | {address}
    row = re.search(r"\d+$", address).group()
    # Only a text error fallback may be unwrapped; failures remain pending.
    match = re.fullmatch(r'IFERROR\((.*),\s*"[\-/]?"\)', formula, re.I)
    if match:
        formula = match[1]

    def ref(match):
        target = match[0].replace("$", "")
        if re.search(r"\d+$", target).group() != row:
            raise ValueError(address + ": 跨行公式需要核实，未采用缓存")
        return repr(number(cells, target, seen))

    def sum_range(match):
        def col(value):
            n = 0
            for c in value:
                n = n * 26 + ord(c) - 64
            return n

        start, end = col(match[1]), col(match[3])
        if match[2] != row or match[4] != row or end < start or end - start > 100:
            raise ValueError(address + ": SUM 范围不在受支持的同一行范围内")
        values = []
        for n in range(start, end + 1):
            letters = ""
            q = n
            while q:
                q, r = divmod(q - 1, 26)
                letters = chr(65 + r) + letters
            key = letters + row
            values.append(number(cells, key, seen) if key in cells else 0)
        return repr(sum(values))

    try:
        formula = re.sub(
            r"SUM\(\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)\)", sum_range, formula, flags=re.I
        )
        if any(c in formula for c in ["!", "[", "]"]):
            raise ValueError("含外部或跨工作表引用，需要核实")
        formula = re.sub(r"\$?[A-Z]{1,3}\$?\d+", ref, formula)
        tree = ast.parse(formula, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 100:
            raise ValueError("excessive formula")

        def calc(n):
            if isinstance(n, ast.Constant) and type(n.value) in (int, float):
                return n.value
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
                return calc(n.operand) * (1 if isinstance(n.op, ast.UAdd) else -1)
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                a, b = calc(n.left), calc(n.right)
                if isinstance(n.op, ast.Add):
                    return a + b
                if isinstance(n.op, ast.Sub):
                    return a - b
                if isinstance(n.op, ast.Mult):
                    return a * b
                return a / b
            raise ValueError("公式函数或运算暂不支持，未采用缓存")

        result = calc(tree.body)
        if not math.isfinite(result):
            raise ValueError("计算结果不是有限数值")
        return result
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as e:
        raise ValueError(address + ": " + str(e)) from e


def passes_filters(cells, row, filters, date_1904=False):
    for rule in filters:
        column = rule.get("column", "")
        if not re.fullmatch("[A-Z]{1,3}", column):
            raise ValueError("Filter requires an Excel column letter")
        address = column + str(row)
        value = cells.get(address, {}).get("value")
        op = rule.get("operation")
        if op == "date_range":
            if isinstance(value, (int, float)):
                value = (
                    (
                        (datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30))
                        + timedelta(days=value)
                    )
                    .date()
                    .isoformat()
                )
            else:
                parsed = None
                for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
                    try:
                        parsed = datetime.strptime(str(value), pattern).date().isoformat()
                        break
                    except ValueError:
                        pass
                value = parsed
            if value is None:
                raise ValueError(address + ": 业务日期缺失或无效，无法确定所属期间")
            start = date.fromisoformat(rule["start"]).isoformat()
            end = date.fromisoformat(rule["end_exclusive"]).isoformat()
            if start >= end:
                raise ValueError("Date filter must have increasing bounds")
            if not start <= value < end:
                return False
        elif op in {"eq", "ne", "lt", "lte", "gt", "gte"}:
            if value in (None, "") or "formula" in cells.get(address, {}):
                raise ValueError(address + ": 筛选字段缺失或无法解释，不能静默排除")
            target = rule["value"]
            try:
                matched = {
                    "eq": lambda: value == target,
                    "ne": lambda: value != target,
                    "lt": lambda: value < target,
                    "lte": lambda: value <= target,
                    "gt": lambda: value > target,
                    "gte": lambda: value >= target,
                }[op]()
            except TypeError as e:
                raise ValueError(address + ": 筛选值的类型或单位不一致") from e
            if not matched:
                return False
        else:
            raise ValueError("Unsupported filter operation")
    return True


def analyze_workbook(content, plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("sheets"), dict):
        raise ValueError("spreadsheet_plan.sheets must be an object")
    sheets = workbook(content)
    ledger = []
    groups = {}
    if set(plan["sheets"]) - {s["name"] for s in sheets}:
        raise ValueError("Unknown worksheet in analysis plan")
    for s in sheets:
        p = plan["sheets"].get(s["name"])
        headers = set(p.get("headers", [])) if p else set()
        excluded = {}
        if p:
            for item in p.get("exclude", []):
                if not isinstance(item.get("row"), int) or not str(item.get("reason", "")).strip():
                    raise ValueError("Excluded rows require an exact row and reason")
                if item["row"] in excluded or item["row"] in headers:
                    raise ValueError("Duplicate row disposition")
                excluded[item["row"]] = item["reason"]
            if (headers | set(excluded)) - set(s["rows"]):
                raise ValueError("Disposition references nonexistent/empty rows")
            metrics = p.get("metrics", [])
            if not metrics:
                raise ValueError("Each selected sheet requires metrics")
            for m in metrics:
                if (
                    m.get("operation") not in {"sum", "count", "ratio", "average", "min", "max"}
                    or not m.get("name")
                    or not m.get("unit")
                ):
                    raise ValueError("Metrics require name, supported operation and explicit unit")
                columns = (
                    [m.get("column")]
                    if m["operation"] not in {"count", "ratio"}
                    else [m.get("numerator"), m.get("denominator")]
                    if m["operation"] == "ratio"
                    else []
                )
                if any(
                    not isinstance(c, str) or not re.fullmatch("[A-Z]{1,3}", c) for c in columns
                ):
                    raise ValueError("Metric columns must be Excel column letters")
            if len({m["name"] for m in metrics}) != len(metrics):
                raise ValueError("Duplicate metric names")
            if any(not re.fullmatch("[A-Z]{1,3}", c) for c in p.get("group_by", [])):
                raise ValueError("Invalid grouping column")
        for row, cells in s["rows"].items():
            item = {"sheet": s["name"], "row": row, "source_id": s["name"] + "!" + str(row)}
            if row in headers:
                item.update(status="excluded", reason="已声明的表头行")
            elif row in excluded:
                item.update(status="excluded", reason=excluded[row])
            elif not p:
                item.update(
                    status="pending",
                    reason="该工作表尚未确认分析范围；全部非空行保留待核",
                )
            else:
                try:
                    if not passes_filters(s["cells"], row, p.get("filters", []), s["date_1904"]):
                        item.update(
                            status="excluded",
                            reason="不符合已声明的筛选条件："
                            + json.dumps(p["filters"], ensure_ascii=False),
                        )
                        ledger.append(item)
                        continue
                    key = []
                    for c in p.get("group_by", []):
                        value = s["cells"].get(c + str(row), {}).get("value")
                        if value in (None, ""):
                            raise ValueError(c + str(row) + ": 分组标识缺失或无法解释")
                        key.append(str(value))
                    values = {}
                    for m in metrics:
                        op = m["operation"]
                        cols = (
                            [m["numerator"], m["denominator"]]
                            if op == "ratio"
                            else []
                            if op == "count"
                            else [m["column"]]
                        )
                        nums = [number(s["cells"], c + str(row)) for c in cols]
                        if op == "ratio" and nums[1] <= 0:
                            raise ValueError(
                                m["denominator"] + str(row) + ": 分母为零或负值，需要核实"
                            )
                        values[m["name"]] = nums
                    gkey = (s["name"], tuple(key))
                    g = groups.setdefault(
                        gkey,
                        {
                            "sheet": s["name"],
                            "group": key,
                            "rows": [],
                            "values": defaultdict(list),
                            "definitions": metrics,
                        },
                    )
                    g["rows"].append(row)
                    for name, value in values.items():
                        g["values"][name].append(value)
                    item.update(status="computed", values=values)
                except ValueError as e:
                    item.update(status="pending", reason=str(e))
            ledger.append(item)
    output = []
    for g in groups.values():
        computed = {}
        for m in g["definitions"]:
            values = g["values"][m["name"]]
            op = m["operation"]
            if op == "count":
                value = len(values)
            elif op == "ratio":
                value = sum(v[0] for v in values) / sum(v[1] for v in values)
            else:
                nums = [v[0] for v in values]
                value = {"sum": sum, "average": lambda n: sum(n) / len(n), "min": min, "max": max}[
                    op
                ](nums)
            if not math.isfinite(value):
                raise ValueError("Aggregate exceeds numeric bounds")
            computed[m["name"]] = value
        output.append(
            {k: g[k] for k in ["sheet", "group", "rows"]}
            | {"metrics": computed, "units": {m["name"]: m["unit"] for m in g["definitions"]}}
        )
    counts = {k: sum(r["status"] == k for r in ledger) for k in ["computed", "pending", "excluded"]}
    assert sum(counts.values()) == sum(len(s["rows"]) for s in sheets)
    assert len({r["source_id"] for r in ledger}) == len(ledger)
    return {
        "schema": "spreadsheet-analysis/v1",
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "plan": plan,
        "plan_sha256": digest(plan),
        "ledger": ledger,
        "counts": counts,
        "groups": output,
        "whole_population_complete": False,
        "coverage": "all populated source rows accounted; semantic interpretation and business completeness remain unconfirmed",
        "drawing_sheets_pending_review": [s["name"] for s in sheets if s["drawing_present"]],
    }


def render_report(result, filename, artifact):
    c = result["counts"]
    lines = [
        f"文件 **{filename}** 的统计核对结果：",
        f"共 {sum(c.values())} 个非空来源行：可计算 {c['computed']} 行，待核 {c['pending']} 行，按明确规则排除 {c['excluded']} 行。空白行不作为业务记录。",
        "以下数字仅覆盖可计算部分，不代表整份文件或业务总体已经完整确认。",
        "",
    ]
    for g in result["groups"][:100]:
        lines.append(
            f"- {g['sheet']} / {'、'.join(g['group']) or '所选范围'}（{len(g['rows'])} 行）："
            + "；".join(f"{k} = {v:.10g} {g['units'][k]}" for k, v in g["metrics"].items())
        )
    if len(result["groups"]) > 100:
        lines.append(
            f"正文显示前 100 组，其余 {len(result['groups']) - 100} 组见完整明细，未被排除。"
        )
    pending = [r for r in result["ledger"] if r["status"] == "pending"]
    if pending:
        lines += ["", "**无法解释或未纳入计算的数据**："]
        lines += [
            f"- {r['sheet']} 第 {r['row']} 行：{r['reason']}。该行未计入上述数字。"
            for r in pending[:30]
        ]
        if len(pending) > 30:
            lines.append(f"另有 {len(pending) - 30} 行待核，见完整核对明细。")
    if result["drawing_sheets_pending_review"]:
        lines.append(
            "含图形/文本框的工作表："
            + "、".join(result["drawing_sheets_pending_review"])
            + "；其中业务说明尚未核实，可能影响口径。"
        )
    lines += [
        "",
        f"完整计算口径、全部分组及逐行去向：[{filename} 核对明细]({artifact})。",
    ]
    return "\n".join(lines)
