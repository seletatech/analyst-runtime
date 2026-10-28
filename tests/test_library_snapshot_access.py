import hashlib
import json
import shlex
import sys
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
