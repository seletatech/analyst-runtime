"""Citation identities for successfully read, conversation-scoped uploads."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from analyst_runtime.agent.tools.account_access import account_workspace

CITATION_TOKEN = re.compile(r"\[\[cite:([a-f0-9]{24})\]\]")


def bounded_excel_range(value: str) -> bool:
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6}):([A-Z]{1,3})([1-9][0-9]{0,6})", value)
    if not match:
        return False
    def column(letters: str) -> int:
        result = 0
        for char in letters:
            result = result * 26 + ord(char) - 64
        return result
    left, top, right, bottom = column(match[1]), int(match[2]), column(match[3]), int(match[4])
    return right <= 16384 and bottom <= 1048576 and right >= left and bottom == top and right-left+1 <= 500


def citation_from_read(
    result: str, workspace: Path, conversation_id: str, tool_call_id: str
) -> dict[str, Any] | None:
    """Create a reference only for an intact, audited read of an upload page.

    The identifier locates evidence; it does not establish that a claim follows from it.
    """
    try:
        payload = json.loads(result)
        if not isinstance(payload, dict) or payload.get("schema_version") != "analyst-read-result/v1":
            return None
        if payload.get("status") != "ok":
            return None
        if payload.get("format") not in {"pdf", "text", "excel"}:
            return None  # A ZIP listing is not a read of a member.
        if not payload.get("content") and payload.get("visual_status") != "page_image_attached":
            return None
        page = 1 if payload.get("format") == "excel" else payload.get("page")
        if type(page) is not int or page < 1:
            return None
        root = (workspace / "uploads").resolve()
        path = Path(payload["path"]).resolve(strict=True)
        parts = path.relative_to(root).parts
        scope = account_workspace()
        if len(parts) < 4 or parts[1] != payload.get("conversation_id") or parts[2] != payload.get("version"):
            return None
        if scope is None and parts[1] != conversation_id:
            return None
        if scope is not None and (scope.workspace != workspace.resolve() or parts[0] != scope.user_id):
            return None
        expected_hash = payload.get("content_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            return None
        with (scope.open(path, "rb") if scope is not None else path.open("rb")) as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != expected_hash:
            return None
        identity = hashlib.sha256(f"{tool_call_id}\0{digest}\0{page}\0{payload.get('archive_member', '')}\0{payload.get('sheet', '')}\0{payload.get('cells', '')}".encode()).hexdigest()[:24]
        ref: dict[str, Any] = {
            "id": identity,
            "kind": "upload",
            "path": str(path.relative_to(workspace.resolve())),
            "sha256": digest,
            "page": page,
            "format": payload["format"],
        }
        if isinstance(payload.get("archive_member"), str):
            ref["member"] = payload["archive_member"]
        if payload.get("pagination") == "converted_document_pages":
            ref["converted"] = True
        pathname = payload.get("source_pathname")
        if isinstance(pathname, str) and pathname.startswith(parts[0] + "--") and "/" not in pathname and "\\" not in pathname:
            ref["source_pathname"] = pathname
        if payload.get("format") == "excel":
            if not isinstance(payload.get("sheet"), str) or not bounded_excel_range(str(payload.get("cells", ""))):
                return None
            ref.update(sheet=payload["sheet"], cells=payload["cells"])
        return ref
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def annotate_history(history: list[dict[str, Any]], events: list[dict[str, Any]], conversation_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expose references only for unambiguous, retained final assistant messages."""
    finals = [event.get("content") for event in events if event.get("type") == "final_response"]
    eligible = {text for text in finals if isinstance(text, str) and 0 < len(text) <= 16000 and finals.count(text) == 1}
    refs: list[dict[str, Any]] = []
    annotated = []
    for message in history:
        content = message.get("content")
        if message.get("role") == "assistant" and not message.get("tool_calls") and isinstance(content, str) and content in eligible:
            identity = hashlib.sha256(f"history\0{conversation_id}\0{content}".encode()).hexdigest()[:24]
            refs.append({"id": identity, "kind": "conversation", "content": content})
            annotated.append({**message, "content": f"{content}\n\n[Reference for this earlier assistant answer: [[cite:{identity}]]]"})
        else:
            annotated.append(message)
    return annotated, refs


def cited_refs(events: list[dict[str, Any]], answer: str, history_refs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return referenced evidence in answer order, excluding unknown/failed reads."""
    available = {
        ref["id"]: ref
        for event in events
        if event.get("type") == "tool_result" and event.get("status") == "completed"
        and isinstance((ref := event.get("citation_ref")), dict)
        and isinstance(ref.get("id"), str)
    }
    available.update({ref["id"]: ref for ref in history_refs or []})
    seen: set[str] = set()
    refs = []
    for match in CITATION_TOKEN.finditer(answer):
        identity = match.group(1)
        if identity in available and identity not in seen:
            seen.add(identity)
            refs.append(available[identity])
    return refs
