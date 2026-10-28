"""Authenticated account file scope shared by ordinary workspace tools."""

import errno
import json
import os
import stat
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class AccountWorkspace:
    workspace: Path
    user_id: str

    def __post_init__(self):
        object.__setattr__(self, "workspace", self.workspace.resolve())
        object.__setattr__(self, "user_id", str(UUID(self.user_id)))

    @property
    def uploads(self) -> Path:
        return self.workspace / "uploads" / self.user_id

    @property
    def private(self) -> Path:
        return self.workspace / "tmp_analysis" / "accounts" / self.user_id

    @property
    def shared(self) -> list[Path]:
        return [self.workspace / name for name in ["data", "skills", "bin", "AGENTS.md", "SOUL.md", "README.md"]]

    def prepare(self) -> None:
        for path in [self.uploads, self.private, self.private / "tmp_analysis", self.private / "artifacts"]:
            parent = self.workspace
            for part in path.relative_to(self.workspace).parts:
                parent = parent / part
                parent.mkdir(exist_ok=True)
                if parent.is_symlink() or not parent.is_dir():
                    raise PermissionError("Account directories must not contain symbolic links")
        for target in [*self.shared, self.uploads]:
            if not target.exists():
                continue
            link = self.private / target.name
            if target == self.uploads:
                link = self.private / "uploads"
            if link.is_symlink() and link.resolve() == target.resolve():
                continue
            if link.exists() or link.is_symlink():
                raise PermissionError("Account workspace view was replaced")
            link.symlink_to(target, target_is_directory=target.is_dir())

    def resolve(self, value: str | Path, *, write: bool = False) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            parts = path.parts
            if parts and parts[0] == "uploads":
                path = self.workspace / path if len(parts) > 1 and parts[1] == self.user_id else self.uploads.joinpath(*parts[1:])
            elif parts and parts[0] in {"data", "skills", "bin", "AGENTS.md", "SOUL.md", "README.md"}:
                path = self.workspace / path
            else:
                path = self.private / path
        resolved = path.resolve()
        allowed = [self.private] if write else [self.private, self.uploads, *self.shared]
        if not any(resolved == root.resolve() or (root.is_dir() and resolved.is_relative_to(root.resolve())) for root in allowed):
            raise PermissionError("File access is outside the authenticated account scope")
        return resolved

    def _directory_fd(self, target: Path, *, create: bool = False) -> int:
        relative = target.relative_to(self.workspace)
        fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in relative.parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except OSError as error:
            os.close(fd)
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PermissionError("Symbolic file paths are not allowed") from error
            raise

    def mkdir(self, path: str | Path) -> Path:
        target = self.resolve(path, write=True)
        fd = self._directory_fd(target, create=True)
        os.close(fd)
        return target

    def open(self, path: str | Path, mode: str = "rb"):
        writing = any(character in mode for character in "wax+")
        target = self.resolve(path, write=writing)
        parent = self._directory_fd(target.parent, create=writing)
        flags = os.O_NOFOLLOW | (os.O_WRONLY | os.O_CREAT if writing else os.O_RDONLY)
        if "w" in mode:
            flags |= os.O_TRUNC
        if "a" in mode:
            flags |= os.O_APPEND
        if "x" in mode:
            flags |= os.O_EXCL
        try:
            fd = os.open(target.name, flags, mode=0o600, dir_fd=parent)
            return os.fdopen(fd, mode, encoding=None if "b" in mode else "utf-8")
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PermissionError("Symbolic file paths are not allowed") from error
            raise
        finally:
            os.close(parent)

    def listing(self, path: str | Path) -> str:
        target = self.resolve(path)
        fd = self._directory_fd(target)
        try:
            return "\n".join(("📁 " if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode) else "📄 ") + name for name in sorted(os.listdir(fd))) or "Directory is empty"
        finally:
            os.close(fd)

    def catalog(self, offset: int = 0) -> str:
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        files = []
        invalid = 0
        for manifest in self.uploads.rglob(".manifest.json"):
            try:
                if manifest.is_symlink():
                    raise ValueError("symbolic manifest")
                item = json.loads(manifest.read_text())
                target = self.resolve(item["workspace_path"])
                relative = target.relative_to(self.uploads)
                if (len(relative.parts) < 3 or manifest.parent != self.uploads / relative.parts[0] / relative.parts[1]
                    or item.get("user_id") != self.user_id or item.get("conversation_id") != relative.parts[0]
                    or item.get("version") != relative.parts[1] or not target.is_file()
                    or item.get("schema_version") != os.environ.get("ANALYST_RUNTIME_UPLOAD_MANIFEST_SCHEMA", "analyst-runtime-workspace-upload/v1")):
                    raise ValueError("invalid upload identity")
                if not isinstance(item.get("source", {}), dict) or not isinstance(item.get("original_name"), str):
                    raise ValueError("invalid upload metadata")
                files.append({"name": item["original_name"], "path": str(target), "uploaded_at": (item.get("source") or {}).get("received_at") or item.get("created_at"), "source_conversation_id": (item.get("source") or {}).get("conversation_id", item["conversation_id"]), "content_sha256": item["content_sha256"]})
            except (OSError, ValueError, KeyError, TypeError):
                invalid += 1
        files.sort(key=lambda item: (item["uploaded_at"] or "", item["path"]))
        end = min(offset + 100, len(files))
        return json.dumps({"files": files[offset:end], "total": len(files), "invalid_manifests": invalid, "next_offset": end if end < len(files) else None}, ensure_ascii=False)


_current: ContextVar[AccountWorkspace | None] = ContextVar("authenticated_account_workspace", default=None)


def account_workspace() -> AccountWorkspace | None:
    return _current.get()


def bind_account_workspace(workspace: Path, user_id: str | None) -> None:
    _current.set(None)
    if user_id:
        scope = AccountWorkspace(workspace, user_id)
        scope.prepare()
        _current.set(scope)
