"""File system tools: read, write, edit, append, and patch."""

import csv
import hashlib
import json
import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.agent.tools.documents import DOCUMENT_SUFFIXES, read_document, read_text


def _resolve_path(
    path: str,
    allowed_dir: Path | None = None,
    allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and allowed_dir is not None:
        candidate = allowed_dir / candidate
    resolved = candidate.resolve()
    roots = [root.resolve() for root in (allowed_dirs or []) if root is not None]
    if allowed_dir is not None:
        roots.append(allowed_dir.resolve())
    if roots and not any(resolved.is_relative_to(root) for root in roots):
        if len(roots) == 1:
            raise PermissionError(f"Path {path} is outside allowed directory {roots[0]}")
        formatted_roots = ", ".join(str(root) for root in roots)
        raise PermissionError(f"Path {path} is outside allowed directories {formatted_roots}")
    return resolved


def _resolve_writable_path(path: str, allowed_dir: Path | None = None) -> Path:
    """Resolve a generic write target without exposing managed project memory."""
    resolved = _resolve_path(path, allowed_dir)
    is_memory_file = (
        resolved.name.casefold() == "memory.md" and resolved.parent.name.casefold() == "memory"
    )
    if is_memory_file:
        raise PermissionError("Project memory is managed by MemoryStore and is read-only")
    return resolved


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        allowed_dirs: list[Path] | None = None,
        audit_results: bool = False,
    ):
        self._allowed_dir = allowed_dir
        self._allowed_dirs = allowed_dirs or []
        self._audit_results = audit_results
        self._spreadsheet_state = ContextVar("spreadsheet_turn", default=None)
        self._vision: ContextVar[bool] = ContextVar("read_file_vision", default=False)
        self._conversation_id: ContextVar[str | None] = ContextVar(
            "read_file_conversation_id",
            default=None,
        )

    def set_conversation_context(self, conversation_id: str | None) -> None:
        """Bind audited reads to the current authenticated conversation."""
        normalized = conversation_id.strip() if isinstance(conversation_id, str) else ""
        self._conversation_id.set(normalized or None)

    def set_vision_context(self, enabled: bool) -> None:
        self._vision.set(enabled)

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Excel: full sheet/row inventory by default, spreadsheet_plan for row-accounted statistics; page=N reads 100 source rows per sheet. Read text files or one PDF page (text and, for vision models, its image). ZIP: omit member to list entries, then pass member=exact_name. Office/HTML/PDF page defaults to 1; use page=N to read all required pages. PDFs are limited to 100 MB and 200 pages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_plan": {
                    "type": "object",
                    "description": 'For complete statistics: {"sheets":{"Sheet1":{"headers":[1],"exclude":[{"row":9,"reason":"subtotal"}],"group_by":["A"],"metrics":[{"name":"yield","operation":"ratio","numerator":"C","denominator":"B","unit":"fraction"}]}}}. Operations: sum/count/ratio/average/min/max. Use column for non-ratio numeric metrics. Omitted sheets and invalid cells remain pending. Optional filters: [{"column":"D","operation":"date_range","start":"2026-01-01","end_exclusive":"2026-02-01"}] or eq/ne/lt/lte/gt/gte with value. Missing filter values remain pending. Confirm units/grain first; never guess.',
                },
                "member": {
                    "type": "string",
                    "description": "Exact ZIP entry name; omit to list entries",
                },
                "path": {"type": "string", "description": "The file path to read"},
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "PDF page number (one-based), default 1",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        page: int = 1,
        member: str | None = None,
        spreadsheet_plan: dict | None = None,
        **kwargs: Any,
    ) -> str:
        read_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if spreadsheet_plan is not None and (state := self._spreadsheet_state.get()):
            state["reports"].pop(str(Path(path).expanduser().resolve()), None)
        try:
            if self._audit_results and self._has_symlink_component(Path(path).expanduser()):
                return self._audit_error("denied", path, read_at, "Symbolic links are not allowed")
            file_path = _resolve_path(path, self._allowed_dir, self._allowed_dirs)
            audit_upload = self._audit_results and self._is_workspace_upload(file_path)
            if not file_path.exists():
                return (
                    self._audit_error("not_found", path, read_at, "File not found")
                    if audit_upload
                    else f"Error: File not found: {path}"
                )
            if not file_path.is_file():
                return (
                    self._audit_error("denied", path, read_at, "Not a file")
                    if audit_upload
                    else f"Error: Not a file: {path}"
                )

            is_document = file_path.suffix.lower() in DOCUMENT_SUFFIXES | {".pdf", ".xlsm"}
            if is_document and file_path.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("Document exceeds 100 MB")
            content_bytes = file_path.read_bytes()
            if is_document:
                source = {
                    "schema_version": "analyst-read-result/v1",
                    "status": "ok",
                    "path": str(file_path),
                    "read_at": read_at,
                    "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
                }
                if audit_upload:
                    source = json.loads(
                        self._audited_content(file_path, content_bytes, read_at, binary_pdf=True)
                    )
                    if source.get("status") != "ok":
                        return json.dumps(source)
                if file_path.suffix.lower() in {".xlsx", ".xlsm", ".xls", ".ods"}:
                    return await self._read_spreadsheet(
                        file_path, content_bytes, source, page, spreadsheet_plan
                    )
                return await read_document(
                    content_bytes,
                    suffix=file_path.suffix.lower(),
                    page=page,
                    member=member,
                    vision=self._vision.get(),
                    source=source,
                )
            if audit_upload:
                source = json.loads(
                    self._audited_content(file_path, content_bytes, read_at, binary_pdf=True)
                )
                if source.get("status") != "ok":
                    return json.dumps(source)
                source.pop("content", None)
                return read_text(content_bytes, page=page, source=source)
            return content_bytes.decode("utf-8")
        except PermissionError as e:
            return (
                self._audit_error("denied", path, read_at, str(e))
                if self._audit_results
                else f"Error: {e}"
            )
        except Exception as e:
            return (
                self._audit_error("error", path, read_at, str(e))
                if self._audit_results
                else f"Error reading file: {str(e)}"
            )

    def begin_spreadsheet_turn(self, messages):
        """Per-run evidence, never accepted from model-authored files or exec stdout."""
        text = json.dumps(messages, ensure_ascii=False)
        paths = re.findall(
            r'(/[^\s"<>]*?/uploads/[^"\n；）<>]*?\.(?:xlsx|xlsm|xls|ods))', text, re.I
        )
        self._spreadsheet_state.set(
            {"required": set(paths), "reports": {}, "hashes": {}, "artifacts": {}, "attempts": 0}
        )

    def spreadsheet_delivery(self):
        state = self._spreadsheet_state.get()
        if not state or not state["required"]:
            return None, None
        missing = state["required"] - state["reports"].keys()
        for path, expected in state["hashes"].items():
            file = Path(path)
            if (
                self._has_symlink_component(file)
                or not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != expected
            ):
                missing.add(path)
        for path, expected in state["artifacts"].items():
            file = Path(path)
            if (
                self._has_symlink_component(file)
                or not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != expected
            ):
                missing.update(state["required"])
        if missing:
            state["attempts"] += 1
            files = "、".join(Path(p).name for p in sorted(missing))
            return None, (
                f"Excel statistical delivery is not verified for: {files}. Call read_file for workbook inventory, then pass spreadsheet_plan with explicit headers, units, grouping and metrics. Never omit hidden/missing rows. If interpretation is unclear, ask a specific question and explain affected locations; do not invent a statistical result."
            )
        return "\n\n".join(state["reports"][p] for p in sorted(state["required"])), None

    def spreadsheet_blocked_report(self):
        state = self._spreadsheet_state.get() or {}
        names = "、".join(Path(p).name for p in sorted(state.get("required", [])))
        return f"本次 Excel 统计尚未完成核对，不能交付可靠的合计或良率。涉及文件：{names}。请确认表头、数量单位、每行粒度和统计范围；无法解释的行必须列为待核，不能按零值或正常记录处理。"

    def spreadsheet_artifacts(self):
        state = self._spreadsheet_state.get()
        return (
            list(state["artifacts"])
            if state and state["reports"] and not (state["required"] - state["reports"].keys())
            else []
        )

    async def _read_spreadsheet(self, path, content, source, page, plan):
        from analyst_runtime.agent.tools.documents import office_pdf
        from analyst_runtime.agent.tools.spreadsheets import (
            analyze_workbook,
            digest,
            inspect_workbook,
            render_report,
        )

        state = self._spreadsheet_state.get()
        if state is not None:
            state["required"].add(str(path))
        original_hash = source["content_sha256"]
        if path.suffix.lower() in {".xls", ".ods"}:
            # Isolated conversion never replaces original source identity/cached evidence.
            content = await office_pdf(content, path.suffix.lower(), target_format="xlsx")
        if plan is None:
            result = inspect_workbook(content, page)
            result.update(source)
            result.pop("content", None)
            return json.dumps(result, ensure_ascii=False)
        result = analyze_workbook(content, plan)
        result.update(
            converted_input_sha256=result["input_sha256"],
            input_sha256=original_hash,
            original_input_sha256=original_hash,
            conversation_id=source.get("conversation_id"),
            path=str(path),
        )
        root = self._allowed_dir or path.parent
        folder = (
            root
            / "artifacts"
            / "spreadsheet"
            / digest([source.get("conversation_id"), str(path), original_hash, plan])
        )
        if self._has_symlink_component(folder) or not folder.resolve().is_relative_to(
            root.resolve()
        ):
            raise PermissionError("Spreadsheet artifact path is outside its workspace")
        folder.mkdir(parents=True, exist_ok=True)
        artifact = folder / (path.stem + "-" + original_hash[:8] + "-核对明细.json")
        if artifact.is_symlink() or artifact.with_suffix(".csv").is_symlink():
            raise PermissionError("Spreadsheet artifact symbolic links are forbidden")
        artifact.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        csv_path = artifact.with_suffix(".csv")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=["sheet", "row", "source_id", "status", "reason", "values"],
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in result["ledger"]:
                safe = {
                    k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v
                    for k, v in row.items()
                }
                writer.writerow(
                    {
                        k: "'" + v
                        if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                        else v
                        for k, v in safe.items()
                    }
                )
        if max(artifact.stat().st_size, csv_path.stat().st_size) > 25 * 1024 * 1024:
            raise ValueError(
                "Spreadsheet evidence exceeds the 25 MB delivery limit; split the analysis scope"
            )
        report = render_report(result, path.name, artifact.name)
        if state is not None:
            state["reports"][str(path)] = report
            state["hashes"][str(path)] = original_hash
            for file in [artifact, csv_path]:
                state["artifacts"][str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
        return json.dumps(
            {
                **source,
                "schema": "spreadsheet-analysis/v1",
                "counts": result["counts"],
                "groups": result["groups"][:100],
                "artifact": str(artifact),
                "plan_sha256": result["plan_sha256"],
                "report": report,
            },
            ensure_ascii=False,
        )

    def _is_workspace_upload(self, candidate: Path) -> bool:
        if self._allowed_dir is None:
            return False
        try:
            relative = candidate.relative_to(self._allowed_dir.resolve())
        except ValueError:
            return False
        return bool(relative.parts) and relative.parts[0].casefold() == "uploads"

    @staticmethod
    def _has_symlink_component(candidate: Path) -> bool:
        absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
        return any(part.is_symlink() for part in (*reversed(absolute.parents), absolute))

    @staticmethod
    def _audit_error(status: str, path: str, read_at: str, error: str) -> str:
        return json.dumps(
            {
                "schema_version": "analyst-read-result/v1",
                "status": status,
                "path": path,
                "read_at": read_at,
                "error": error,
            },
            ensure_ascii=False,
        )

    def _audited_content(
        self, file_path: Path, content: bytes, read_at: str, binary_pdf: bool = False
    ) -> str:
        if len(content) > (100 * 1024 * 1024 if binary_pdf else 1024 * 1024):
            return self._audit_error(
                "limit_exceeded", str(file_path), read_at, "File exceeds the read size limit"
            )
        try:
            decoded = "" if binary_pdf else content.decode("utf-8")
        except UnicodeDecodeError:
            return self._audit_error(
                "unsupported", str(file_path), read_at, "File is not valid UTF-8 text"
            )
        manifest_path = file_path.parent / ".manifest.json"
        while not manifest_path.is_file() and self._allowed_dir is not None:
            if manifest_path.parent == self._allowed_dir.resolve():
                break
            manifest_path = manifest_path.parent.parent / ".manifest.json"
        if not manifest_path.is_file():
            return self._audit_error(
                "denied", str(file_path), read_at, "File has no approved upload manifest"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._audit_error(
                "integrity_error", str(file_path), read_at, "Upload manifest is invalid"
            )
        digest = hashlib.sha256(content).hexdigest()
        conversation_id = self._conversation_id.get()
        if not conversation_id or manifest.get("conversation_id") != conversation_id:
            return self._audit_error(
                "denied",
                str(file_path),
                read_at,
                "File is not approved for the current conversation",
            )
        relative_parts = file_path.relative_to(self._allowed_dir.resolve()).parts
        if (
            len(relative_parts) < 5
            or relative_parts[0] != "uploads"
            or manifest.get("user_id") != relative_parts[1]
            or conversation_id != relative_parts[2]
            or manifest.get("version") != relative_parts[3]
            or manifest.get("schema_version")
            != os.environ.get(
                "ANALYST_RUNTIME_UPLOAD_MANIFEST_SCHEMA",
                "analyst-runtime-workspace-upload/v1",
            )
            or manifest.get("workspace_path") != str(file_path)
            or manifest.get("content_sha256") != digest
        ):
            return self._audit_error(
                "integrity_error", str(file_path), read_at, "File does not match its manifest"
            )
        return json.dumps(
            {
                "schema_version": "analyst-read-result/v1",
                "status": "ok",
                "path": str(file_path),
                "content_sha256": digest,
                "conversation_id": conversation_id,
                "version": manifest.get("version"),
                "uploaded_at": manifest.get("created_at"),
                "read_at": read_at,
                "content": decoded,
            },
            ensure_ascii=False,
        )


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return (
            "Write content to a file at the given path. Best for small or medium files. "
            "For large generated text files, prefer append_file in smaller chunks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_writable_path(path, self._allowed_dir)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class AppendFileTool(Tool):
    """Tool to append content to a file."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "append_file"

    @property
    def description(self) -> str:
        return (
            "Append content to a file at the given path. Creates parent directories if needed. "
            "Use this for large generated text files in smaller chunks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to append to"},
                "content": {"type": "string", "description": "The content to append"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_writable_path(path, self._allowed_dir)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("a", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully appended {len(content)} bytes to {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error appending file: {str(e)}"


class PatchFileTool(Tool):
    """Tool to patch selected parts of a file."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "patch_file"

    @property
    def description(self) -> str:
        return (
            "Patch selected parts of a text file by applying one or more exact replacements. "
            "Use this to repair targeted sections of a large generated file."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to patch"},
                "patches": {
                    "type": "array",
                    "description": "Exact replacements to apply in order",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {
                                "type": "string",
                                "description": "The exact text to replace",
                            },
                            "new_text": {"type": "string", "description": "The replacement text"},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["path", "patches"],
        }

    async def execute(self, path: str, patches: list[dict[str, str]], **kwargs: Any) -> str:
        try:
            file_path = _resolve_writable_path(path, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            for patch in patches:
                old_text = patch["old_text"]
                new_text = patch["new_text"]

                count = content.count(old_text)
                if count == 0:
                    return f"Error: patch old_text not found in file: {old_text[:80]}"
                if count > 1:
                    return (
                        f"Error: patch old_text appears {count} times. "
                        "Provide more specific context so the patch is unique."
                    )
                content = content.replace(old_text, new_text, 1)

            file_path.write_text(content, encoding="utf-8")
            return f"Successfully patched {len(patches)} section(s) in {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error patching file: {str(e)}"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    def __init__(self, allowed_dir: Path | None = None):
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The exact text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_writable_path(path, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return "Error: old_text not found in file. Make sure it matches exactly."

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            return f"Successfully edited {path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"


class ListDirTool(Tool):
    """Tool to list directory contents."""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        allowed_dirs: list[Path] | None = None,
    ):
        self._allowed_dir = allowed_dir
        self._allowed_dirs = allowed_dirs or []

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The directory path to list"}},
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, self._allowed_dir, self._allowed_dirs)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"
