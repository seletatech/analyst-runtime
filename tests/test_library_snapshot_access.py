import errno
import hashlib
import json
import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from analyst_runtime.agent.tools.account_access import account_workspace, bind_account_workspace
from analyst_runtime.agent.tools.filesystem import ReadFileTool
from analyst_runtime.agent.tools.shell import ExecTool


def snapshot(root, user, run, value):
    directory = root / "library-snapshots" / user / run
    directory.mkdir(parents=True)
    (directory / "original.txt").write_text(value)
    manifest = json.dumps({"schema_version": "linghui-library-snapshot/v1", "user_id": user, "run_id": run, "files": []}).encode()
    (directory / "manifest.json").write_bytes(manifest)
    return {"run_id": run, "manifest_sha256": hashlib.sha256(manifest).hexdigest()}


def test_new_account_can_analyze_with_read_only_upload_mount(tmp_path, monkeypatch):
    user, run = str(uuid4()), str(uuid4())
    identity = snapshot(tmp_path, user, run, "company original")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    original_mkdir = Path.mkdir

    def readonly_mkdir(path, *args, **kwargs):
        if path.is_relative_to(uploads) and not path.exists():
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", readonly_mkdir)
    try:
        bind_account_workspace(tmp_path, user, identity, run)
        scope = account_workspace()
        assert scope.resolve("data/original.txt").read_text() == "company original"
        assert scope.private.is_dir()
        assert not scope.uploads.exists()
        assert json.loads(scope.catalog())["total"] == 0
        (uploads / user).symlink_to(scope.data, target_is_directory=True)
        with pytest.raises(PermissionError, match="symbolic"):
            bind_account_workspace(tmp_path, user, identity, run)
    finally:
        bind_account_workspace(tmp_path, None)


@pytest.mark.asyncio
async def test_snapshot_selects_exact_run_and_hides_legacy_and_other_runs(tmp_path):
    user, other, first, second = [str(uuid4()) for _ in range(4)]
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "legacy.txt").write_text("CROSS_DEPARTMENT_LEGACY")
    frozen = snapshot(tmp_path, user, first, "first version")
    newer = snapshot(tmp_path, user, second, "second version")
    foreign = snapshot(tmp_path, other, str(uuid4()), "FOREIGN_SNAPSHOT")
    bind_account_workspace(tmp_path, user, frozen, first)
    first_scope = account_workspace()
    tool = ReadFileTool(allowed_dir=tmp_path)
    assert "first version" in await tool.execute("data/original.txt")
    for candidate in [tmp_path / "data" / "legacy.txt", tmp_path / "library-snapshots" / user / second / "original.txt", tmp_path / "library-snapshots" / other / foreign["run_id"] / "original.txt"]:
        with pytest.raises(PermissionError):
            first_scope.resolve(candidate)
    with pytest.raises(PermissionError):
        first_scope.resolve("data/original.txt", write=True)
    bind_account_workspace(tmp_path, user, newer, second)
    assert "second version" in await tool.execute("data/original.txt")
    assert first_scope.private != account_workspace().private
    assert first_scope.resolve("data/original.txt").read_text() == "first version"
    bind_account_workspace(tmp_path, None)


def test_missing_forged_or_reassigned_snapshot_identity_is_rejected(tmp_path):
    user, run, other_run = [str(uuid4()) for _ in range(3)]
    identity = snapshot(tmp_path, user, run, "valid")
    for owner, value, target in [(None, identity, run), (user, identity, other_run), (user, {**identity, "manifest_sha256": "0" * 64}, run), (str(uuid4()), identity, run)]:
        with pytest.raises((PermissionError, FileNotFoundError)):
            bind_account_workspace(tmp_path, owner, value, target)
        assert account_workspace() is None


def test_symlinks_cannot_escape_selected_snapshot(tmp_path):
    user, run = str(uuid4()), str(uuid4())
    identity = snapshot(tmp_path, user, run, "valid")
    private = tmp_path / "private-secret"
    private.write_text("NEVER_READ")
    (tmp_path / "library-snapshots" / user / run / "escape").symlink_to(private)
    bind_account_workspace(tmp_path, user, identity, run)
    with pytest.raises(PermissionError):
        account_workspace().resolve("data/escape")
    bind_account_workspace(tmp_path, None)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux Landlock")
async def test_exec_receives_same_snapshot_and_cannot_read_previous_data(tmp_path):
    user, run, second = [str(uuid4()) for _ in range(3)]
    identity = snapshot(tmp_path, user, run, "selected original")
    snapshot(tmp_path, user, second, "other run secret")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "legacy.txt").write_text("old role secret")
    bind_account_workspace(tmp_path, user, identity, run)
    try:
        denied = [tmp_path / "data" / "legacy.txt", tmp_path / "library-snapshots" / user / second / "original.txt"]
        source = f"""
from pathlib import Path
assert Path('data/original.txt').read_text() == 'selected original'
for name in {list(map(str, denied))!r}:
 try: Path(name).read_text()
 except PermissionError: pass
 else: raise AssertionError('unauthorized read')
try: Path('data/original.txt').write_text('overwrite')
except PermissionError: pass
else: raise AssertionError('original is writable')
Path('answer.txt').write_text('private result')
print('SNAPSHOT_BOUNDARY_PASSED')
"""
        output = await ExecTool(working_dir=str(tmp_path), restrict_to_workspace=True).execute("python -c " + shlex.quote(source))
        assert output.strip() == "SNAPSHOT_BOUNDARY_PASSED", output
        assert (account_workspace().private / "answer.txt").read_text() == "private result"
    finally:
        bind_account_workspace(tmp_path, None)


@pytest.mark.asyncio
async def test_citation_is_bound_to_library_version_and_actual_original_hash(tmp_path):
    from analyst_runtime.agent.citation_refs import citation_from_read
    user, run, file_id, version = [str(uuid4()) for _ in range(4)]
    identity = snapshot(tmp_path, user, run, "production count 17")
    root = tmp_path / "library-snapshots" / user / run
    manifest = json.loads((root / "manifest.json").read_text())
    digest = hashlib.sha256((root / "original.txt").read_bytes()).hexdigest()
    manifest["files"] = [{"file_id": file_id, "version_id": version, "path": "original.txt", "sha256": digest}]
    content = json.dumps(manifest).encode()
    (root / "manifest.json").write_bytes(content)
    identity["manifest_sha256"] = hashlib.sha256(content).hexdigest()
    bind_account_workspace(tmp_path, user, identity, run)
    try:
        tool = ReadFileTool(allowed_dir=tmp_path, audit_results=True)
        result = await tool.execute("data/original.txt")
        ref = citation_from_read(result, tmp_path, "chat", "call")
        assert ref and (ref["kind"], ref["file_id"], ref["version_id"], ref["sha256"]) == ("library", file_id, version, digest)
        forged = json.loads(result)
        forged["library_version_id"] = str(uuid4())
        assert citation_from_read(json.dumps(forged), tmp_path, "chat", "call") is None
        (root / "original.txt").write_text("changed original")
        assert json.loads(await tool.execute("data/original.txt"))["status"] == "integrity_error"
        assert citation_from_read(result, tmp_path, "chat", "call") is None
    finally:
        bind_account_workspace(tmp_path, None)
