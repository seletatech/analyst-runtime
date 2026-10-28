"""File system tools: read, write, edit, append, and patch."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analyst_runtime.agent.tools.account_access import account_workspace
from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.agent.tools.document_isolation import isolate_office
from analyst_runtime.agent.tools.documents import (
    DOCUMENT_SUFFIXES,
    archive_entries,
    read_document,
    read_text,
)


async def _read_excel_cells(content: bytes, sheet: str | None, cells: str | None, source: dict) -> str:
    if not isinstance(sheet, str) or not sheet or len(sheet) > 31:
        raise ValueError("A worksheet name is required")
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})(?::([A-Z]{1,3})([1-9][0-9]{0,6}))?", cells or "")
    if not match:
        raise ValueError("Expected a bounded Excel range such as A2:F2")
    def col(value: str) -> int:
        result = 0
        for char in value:
            result = result * 26 + ord(char) - 64
        return result
    left, top = col(match[1]), int(match[2])
    right, bottom = col(match[3] or match[1]), int(match[4] or match[2])
    if right > 16384 or bottom > 1048576 or right < left or bottom != top or right-left+1 > 500:
        raise ValueError("Excel citations require one row of at most 500 cells within worksheet bounds")
    archive, _ = archive_entries(content)
    archive.close()
    normalized = f"{match[1]}{top}:{match[3] or match[1]}{bottom}"
    with tempfile.TemporaryDirectory(prefix="analyst-xlsx-read-") as directory:
        snapshot = Path(directory) / "source.xlsx"
        snapshot.write_bytes(content)
        payload = {"filepath": str(snapshot), "sheet_name": sheet, "range": normalized,
                   "filters": [{"column": "1", "operator": "regex", "value": ".*"}], "has_header": False}
        executable = shutil.which("excel-mcp")
        if not executable:
            raise ValueError("Excel reader is unavailable")
        process = await asyncio.create_subprocess_exec(
            executable, "cli", "filter_rows", "--input", "-",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": directory},
            preexec_fn=(lambda: isolate_office(directory, readonly_paths=(executable,), local_sockets=False)) if os.sys.platform == "linux" else None,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps(payload).encode())
            await process.stdin.drain()
            process.stdin.close()
            try:
                stdout = await asyncio.wait_for(process.stdout.readexactly(100_001), 30)
            except asyncio.IncompleteReadError as error:
                stdout = error.partial
            if len(stdout) > 100_000:
                raise ValueError("Excel range output exceeds the read limit")
            await asyncio.wait_for(process.wait(), 30)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ValueError("Excel range could not be read or is too large")
        value = json.loads(stdout)
        if not isinstance(value, dict) or value.get("sheet_name") != sheet or value.get("range") != normalized or not isinstance(value.get("rows"), list) or len(value["rows"]) > 1:
            raise ValueError("Excel returned an invalid range")
        source.pop("content", None)
        return json.dumps({**source, "format": "excel", "sheet": sheet, "cells": normalized,
                           "content": value["rows"], "notice": "column_1 starts at the first requested column; headers and merged-cell meaning require separate checks."}, ensure_ascii=False)


def _read_bytes(path: Path) -> bytes:
    if (scope := account_workspace()) is not None:
        with scope.open(path, "rb") as source:
            return source.read()
    return path.read_bytes()


def _read_text(path: Path) -> str:
    if (scope := account_workspace()) is not None:
        with scope.open(path, "r") as source:
            return source.read()
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, content: str, mode: str = "w") -> None:
    if (scope := account_workspace()) is not None:
        with scope.open(path, mode) as output:
            output.write(content)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode, encoding="utf-8") as output:
            output.write(content)


def _resolve_path(
    path: str,
    allowed_dir: Path | None = None,
    allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    scope = account_workspace()
    candidate = scope.resolve(path) if scope is not None else Path(path).expanduser()
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
    scope = account_workspace()
    resolved = scope.resolve(path, write=True) if scope is not None else _resolve_path(path, allowed_dir)
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
        return "Read text files or one PDF page (text and, for vision models, its image). ZIP: omit member to list entries, then pass member=exact_name. Office/HTML/PDF page defaults to 1; use page=N to read all required pages. For uploaded Excel files, sheet and cells read a bounded exact range. PDFs are limited to 100 MB and 200 pages."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "member": {
                    "type": "string",
                    "description": "Exact ZIP entry name; omit to list entries",
                },
                "path": {"type": "string", "description": "The file path to read"},
                "sheet": {"type": "string", "description": "Excel worksheet name for a bounded cell read"},
                "cells": {"type": "string", "description": "Single Excel row such as A2:F2, at most 500 cells"},
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "PDF page number (one-based), default 1",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self, path: str, page: int = 1, member: str | None = None,
        sheet: str | None = None, cells: str | None = None, **kwargs: Any
    ) -> str:
        read_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            if (scope := account_workspace()) is not None:
                path = str(scope.resolve(path))
            if self._audit_results and self._has_symlink_component(Path(path).expanduser()):
                return self._audit_error("denied", path, read_at, "Symbolic links are not allowed")
            file_path = _resolve_path(path, self._allowed_dir, self._allowed_dirs)
            audit_upload = self._audit_results and (self._is_workspace_upload(file_path) or (scope is not None and scope.library_entry(file_path) is not None))
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

            is_document = file_path.suffix.lower() in DOCUMENT_SUFFIXES | {".pdf"}
            if is_document and file_path.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("Document exceeds 100 MB")
            content_bytes = _read_bytes(file_path)
            if sheet is not None or cells is not None:
                if not audit_upload or file_path.suffix.lower() not in {".xlsx", ".xlsm"} or member is not None:
                    raise ValueError("Cell reads require an authorized Excel original")
                source = json.loads(self._audited_content(file_path, content_bytes, read_at, binary_pdf=True))
                if source.get("status") != "ok":
                    return json.dumps(source)
                return await _read_excel_cells(content_bytes, sheet, cells, source)
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
        scope = account_workspace()
        entry = scope.library_entry(file_path) if scope is not None else None
        if entry is not None:
            digest = hashlib.sha256(content).hexdigest()
            if digest != entry.get("sha256"):
                return self._audit_error("integrity_error", str(file_path), read_at, "Library original changed")
            return json.dumps({"schema_version": "analyst-read-result/v1", "status": "ok", "path": str(file_path),
                               "content_sha256": digest, "library_file_id": entry["file_id"], "library_version_id": entry["version_id"],
                               "snapshot_run_id": scope.snapshot_run_id, "read_at": read_at, "content": "" if binary_pdf else content.decode("utf-8")}, ensure_ascii=False)
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
            manifest = json.loads(_read_text(manifest_path))
        except (OSError, ValueError):
            return self._audit_error(
                "integrity_error", str(file_path), read_at, "Upload manifest is invalid"
            )
        digest = hashlib.sha256(content).hexdigest()
        conversation_id = self._conversation_id.get()
        scope = account_workspace()
        if scope is not None:
            if manifest.get("user_id") != scope.user_id:
                return self._audit_error("denied", str(file_path), read_at, "File belongs to another account")
            conversation_id = manifest.get("conversation_id")
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
                "uploaded_at": (manifest.get("source") or {}).get("received_at") if isinstance(manifest.get("source"), dict) and (manifest.get("source") or {}).get("received_at") else manifest.get("created_at"),
                "source_pathname": (manifest.get("source") or {}).get("pathname") if isinstance(manifest.get("source"), dict) else None,
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
            _write_text(file_path, content)
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
            _write_text(file_path, content, "a")
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

            content = _read_text(file_path)
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

            _write_text(file_path, content)
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

            content = _read_text(file_path)

            if old_text not in content:
                return "Error: old_text not found in file. Make sure it matches exactly."

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            _write_text(file_path, new_content)

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
        return "List directory contents. For authenticated accounts, uploads lists all historical uploaded files; use offset to continue past 100 files."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The directory path to list"}, "offset": {"type": "integer", "minimum": 0, "description": "Upload catalog pagination offset"}},
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int = 0, **kwargs: Any) -> str:
        try:
            scope = account_workspace()
            if scope is not None and scope.resolve(path) == scope.uploads:
                return scope.catalog(offset)
            if scope is not None:
                return scope.listing(path)
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
