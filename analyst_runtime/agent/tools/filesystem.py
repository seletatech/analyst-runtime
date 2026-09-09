"""File system tools: read, write, edit, append, and patch."""

import hashlib
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from analyst_runtime.agent.tools.base import Tool


def _resolve_path(
    path: str,
    allowed_dir: Path | None = None,
    allowed_dirs: list[Path] | None = None,
) -> Path:
    """Resolve path and optionally enforce directory restriction."""
    resolved = Path(path).expanduser().resolve()
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
        self._conversation_id: ContextVar[str | None] = ContextVar(
            "read_file_conversation_id",
            default=None,
        )

    def set_conversation_context(self, conversation_id: str | None) -> None:
        """Bind audited reads to the current authenticated conversation."""
        normalized = conversation_id.strip() if isinstance(conversation_id, str) else ""
        self._conversation_id.set(normalized or None)

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "The file path to read"}},
            "required": ["path"],
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        read_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
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

            content_bytes = file_path.read_bytes()
            if audit_upload:
                return self._audited_content(file_path, content_bytes, read_at)
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

    def _audited_content(self, file_path: Path, content: bytes, read_at: str) -> str:
        if len(content) > 1024 * 1024:
            return self._audit_error(
                "limit_exceeded", str(file_path), read_at, "File exceeds the 1MB read limit"
            )
        try:
            decoded = content.decode("utf-8")
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
            or manifest.get("schema_version") != "linghui-workspace-upload/v1"
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
