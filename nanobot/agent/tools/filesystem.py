"""File system tools: read, write, edit, append, and patch."""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


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
    is_confirmation_journal = any(
        parent.name.casefold() == "confirmation-intents"
        and parent.parent.name.casefold() == "memory"
        for parent in (resolved, *resolved.parents)
    )
    if is_memory_file or is_confirmation_journal:
        raise PermissionError(
            "Project semantic memory is managed by MemoryStore and "
            "ConfirmationIntentJournal and cannot be changed with generic file tools"
        )
    return resolved


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(
        self,
        allowed_dir: Path | None = None,
        allowed_dirs: list[Path] | None = None,
    ):
        self._allowed_dir = allowed_dir
        self._allowed_dirs = allowed_dirs or []

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
        try:
            file_path = _resolve_path(path, self._allowed_dir, self._allowed_dirs)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            return content
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


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
