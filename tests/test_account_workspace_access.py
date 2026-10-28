import asyncio
import hashlib
import json
import shlex
import sys
from uuid import uuid4

import pytest

from analyst_runtime.agent.context import ContextBuilder
from analyst_runtime.agent.tools.account_access import account_workspace, bind_account_workspace
from analyst_runtime.agent.tools.filesystem import (
    AppendFileTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from analyst_runtime.agent.tools.message import MessageTool
from analyst_runtime.agent.tools.shell import ExecTool


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_RUNTIME_UPLOAD_MANIFEST_SCHEMA", "linghui-workspace-upload/v1")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "shared.txt").write_text("ROLE_AUTHORIZED_DATA")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "MEMORY.md").write_text("OTHER_ACCOUNT_GLOBAL_MEMORY_473")
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "foreign.jsonl").write_text("OTHER_ACCOUNT_HISTORY_247")
    first, second = str(uuid4()), str(uuid4())

    def upload(owner, text="owned original", name="original.txt"):
        conversation, version = str(uuid4()), str(uuid4())
        file = tmp_path / "uploads" / owner / conversation / version / name
        file.parent.mkdir(parents=True)
        file.write_text(text)
        (file.parent / ".manifest.json").write_text(json.dumps({
            "schema_version": "linghui-workspace-upload/v1", "user_id": owner,
            "conversation_id": conversation, "version": version, "workspace_path": str(file),
            "original_name": name, "created_at": "2026-10-28T00:00:00Z",
            "content_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        }))
        return file

    own = upload(first)
    foreign = upload(second, "FOREIGN_FILE_CONTENT_93741", "foreign.txt")
    bind_account_workspace(tmp_path, first)
    yield tmp_path, first, second, own, foreign, upload
    bind_account_workspace(tmp_path, None)


@pytest.mark.asyncio
async def test_historical_upload_read_preserves_origin_and_hash(accounts):
    root, owner, _, own, foreign, _ = accounts
    tool = ReadFileTool(allowed_dir=root, audit_results=True)
    tool.set_conversation_context(str(uuid4()))
    result = json.loads(await tool.execute(str(own)))
    assert result["status"] == "ok" and result["content"] == "owned original"
    assert result["conversation_id"] == own.parts[-3]
    assert json.loads(await tool.execute(str(foreign)))["status"] == "denied"
    own.write_text("changed bytes")
    assert json.loads(await tool.execute(str(own)))["status"] == "integrity_error"
    assert account_workspace().user_id == owner


@pytest.mark.asyncio
async def test_upload_catalog_paginates_all_owned_conversations(accounts):
    root, owner, other, own, _, upload = accounts
    for n in range(102):
        upload(owner, name=f"report-{n:03}.txt")
    tool = ListDirTool(allowed_dir=root)
    first = json.loads(await tool.execute("uploads"))
    second = json.loads(await tool.execute("uploads", offset=first["next_offset"]))
    assert first["total"] == 103 and len(first["files"]) == 100
    assert len(second["files"]) == 3 and second["next_offset"] is None
    assert len({x["path"] for x in first["files"] + second["files"]}) == 103
    assert other not in json.dumps(first) and "foreign.txt" not in json.dumps(first)
    assert other not in await tool.execute(".")
    assert "outside" in await tool.execute(str(root / "uploads" / other))
    assert "original.txt" in json.dumps(first["files"] + second["files"])


@pytest.mark.asyncio
async def test_file_tools_deny_other_accounts_shared_mutation_and_symlinks(accounts):
    root, _, _, own, foreign, _ = accounts
    scope = account_workspace()
    for tool in [WriteFileTool(allowed_dir=root), AppendFileTool(allowed_dir=root)]:
        assert "outside" in await tool.execute(str(foreign), "tamper")
        assert "outside" in await tool.execute(str(own), "tamper")
        assert "outside" in await tool.execute("data/shared.txt", "tamper")
        assert "Successfully" in await tool.execute("my-result.txt", "mine")
    link = scope.private / "foreign-link.txt"
    link.symlink_to(foreign)
    assert "denied" in await ReadFileTool(allowed_dir=root, audit_results=True).execute(str(link))
    assert "outside" in await ReadFileTool(allowed_dir=root).execute(str(root / "sessions/foreign.jsonl"))
    assert "ROLE_AUTHORIZED_DATA" in await ReadFileTool(allowed_dir=root).execute("data/shared.txt")
    assert "OTHER_ACCOUNT_GLOBAL_MEMORY" not in ContextBuilder(root).build_system_prompt()


@pytest.mark.asyncio
async def test_simultaneous_accounts_do_not_share_file_context(accounts):
    root, first, second, own, foreign, _ = accounts
    reader = ReadFileTool(allowed_dir=root, audit_results=True)

    async def read(owner, expected, rejected):
        bind_account_workspace(root, owner)
        reader.set_conversation_context(str(uuid4()))
        await asyncio.sleep(0)
        assert json.loads(await reader.execute(str(expected)))["status"] == "ok"
        assert json.loads(await reader.execute(str(rejected)))["status"] == "denied"
        assert json.loads(await ListDirTool(allowed_dir=root).execute("uploads"))["total"] == 1

    await asyncio.gather(read(first, own, foreign), read(second, foreign, own))


@pytest.mark.asyncio
async def test_message_attachment_and_target_cannot_bypass_owner(accounts):
    _, owner, _, _, foreign, _ = accounts
    sent = []

    async def send(msg):
        sent.append(msg)

    tool = MessageTool(send_callback=send, default_channel="web", default_chat_id="current-run")
    assert "outside" in await tool.execute("file", media=[str(foreign)])
    assert "authenticated conversation" in await tool.execute("file", chat_id="other-run")
    assert not sent
    assert json.loads(await tool.execute("ok"))["delivered"] is True
    assert sent[0].metadata["_authenticated_user_id"] == owner


@pytest.mark.asyncio
async def test_account_exec_fails_closed_on_unsupported_platform(accounts):
    if sys.platform == "linux":
        pytest.skip("Linux isolation is exercised by the real child-process test")
    root = accounts[0]
    output = await ExecTool(working_dir=str(root), restrict_to_workspace=True).execute("echo SHOULD_NOT_EXECUTE")
    assert "requires Linux Landlock" in output and "SHOULD_NOT_EXECUTE" not in output


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Kernel isolation is verified on Linux CI/staging")
async def test_exec_kernel_blocks_foreign_reads_enumeration_truncate_and_sockets(accounts):
    root, _, other, own, foreign, _ = accounts
    (account_workspace().private / "sitecustomize.py").write_text(
        "from pathlib import Path\ntry:print(Path(" + repr(str(foreign)) + ").read_text())\nexcept PermissionError:pass\n"
    )
    fake_package = account_workspace().private / "analyst_runtime"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        "from pathlib import Path;print(Path(" + repr(str(foreign)) + ").read_text());raise SystemExit(0)"
    )
    source = f'''
import json,os,socket,ctypes,platform
from pathlib import Path
checks={{}}
checks['own']=Path({str(own)!r}).read_text()=='owned original'
checks['role_data']=Path('data/shared.txt').read_text()=='ROLE_AUTHORIZED_DATA'
Path('my-result.txt').write_text('private output')
if platform.machine()=='x86_64':
 libc=ctypes.CDLL(None,use_errno=True);checks['x32_abi']=libc.syscall(0x40000000|41,2,1,0)==-1 and ctypes.get_errno()==13
for name,operation in [
 ('foreign',lambda:Path({str(foreign)!r}).read_text()),
 ('enumerate_foreign',lambda:list(Path({str(foreign.parent)!r}).iterdir())),
 ('global_history',lambda:Path({str(root/'sessions/foreign.jsonl')!r}).read_text()),
 ('truncate_foreign',lambda:os.truncate({str(foreign)!r},0)),
 ('chmod_foreign',lambda:os.chmod({str(foreign)!r},0o777)),
 ('timestamp_foreign',lambda:os.utime({str(foreign)!r},None)),
 ('truncate_own',lambda:os.truncate({str(own)!r},0)),
 ('socket',lambda:socket.socket()),
 ('proc',lambda:Path('/p'+'roc/self/'+'environ').read_bytes()),
]:
 try:operation();checks[name]=False
 except PermissionError:checks[name]=True
print(json.dumps(checks))
'''
    output = await ExecTool(working_dir=str(root), restrict_to_workspace=True).execute("python -c " + shlex.quote(source))
    assert "Exit code" not in output, output
    checks = json.loads(output)
    assert checks and all(checks.values()), checks
    assert foreign.read_text() == "FOREIGN_FILE_CONTENT_93741" and own.read_text() == "owned original"
    assert (account_workspace().private / "my-result.txt").read_text() == "private output"


@pytest.mark.asyncio
async def test_real_agent_loop_requires_trusted_owner_and_seals_conversation(accounts, monkeypatch):
    from analyst_runtime.agent.loop import AgentLoop
    from analyst_runtime.bus.events import InboundMessage
    from analyst_runtime.bus.queue import MessageBus
    from analyst_runtime.providers.base import LLMProvider, LLMResponse

    root, owner, other, _, _, _ = accounts
    (root / "workspace.json").write_text(json.dumps({"schema_version": 1, "trusted_gateway": {"project_id": "account-test", "runtime": "account-runtime"}}))
    monkeypatch.setenv("ANALYST_RUNTIME_REQUIRE_ACCOUNT_SCOPE", "true")

    class Provider(LLMProvider):
        calls = 0

        def get_default_model(self):
            return "test-model"

        async def chat(self, messages, **kwargs):
            self.calls += 1
            assert account_workspace().user_id == owner
            assert owner in str(messages[0]) and "OTHER_ACCOUNT_GLOBAL_MEMORY_473" not in str(messages)
            return LLMResponse(content="done")

    provider = Provider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=root, tool_profile="trusted-analysis")
    conversation = str(uuid4())

    def message(account=None, runtime="account-runtime"):
        return InboundMessage(channel="web", sender_id="server", chat_id="chat-" + str(uuid4()), conversation_id=conversation, content="我之前上传了什么文件", metadata={"runtime": runtime, "project_id": "account-test", "model_profile_id": "deepseek-v4.1-flash-1017", **({"_authenticated_user_id": account} if account else {})})

    assert (await agent._process_message(message(owner))).content == "done"
    for rejected in [message(), message(owner, runtime="untrusted"), message(other)]:
        result = await agent._process_message(rejected)
        assert result.metadata["error_code"] == "ANALYST-RUNTIME-ACCOUNT-001"
        assert account_workspace() is None
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_file_read_and_listing_reject_symlink_replacement_races(accounts, monkeypatch):
    import os

    root, _, _, _, foreign, _ = accounts
    scope = account_workspace()
    target = scope.private / "result.txt"
    target.write_text("own output")
    original_open = os.open
    replaced = False

    def open_replaced_file(name, flags, *args, **kwargs):
        nonlocal replaced
        if name == "result.txt" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            target.unlink()
            target.symlink_to(foreign)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_replaced_file)
    result = await ReadFileTool(allowed_dir=root, audit_results=True).execute("result.txt")
    assert json.loads(result)["status"] == "denied" and "FOREIGN_FILE_CONTENT_93741" not in result
    monkeypatch.setattr(os, "open", original_open)
    folder = scope.private / "folder"
    folder.mkdir()

    def open_replaced_directory(name, flags, *args, **kwargs):
        if name == "folder" and kwargs.get("dir_fd") is not None and not folder.is_symlink():
            folder.rename(scope.private / "old-folder")
            folder.symlink_to(foreign.parent, target_is_directory=True)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_replaced_directory)
    result = await ListDirTool(allowed_dir=root).execute("folder")
    assert "Symbolic" in result and "foreign.txt" not in result


def test_firecrawl_files_and_cookie_profiles_stay_in_account_scope(accounts):
    from analyst_runtime.agent.tools.firecrawl import (
        _artifact_root,
        _json_write,
        _load_cookie_profile,
    )

    _, _, _, _, foreign, _ = accounts
    scope = account_workspace()
    output = _artifact_root("research") / "result.json"
    assert output.is_relative_to(scope.private)
    _json_write(output, {"own": True})
    assert json.loads(output.read_text()) == {"own": True}
    with pytest.raises(PermissionError):
        _load_cookie_profile(None, str(foreign))


def test_analysis_recall_is_account_private_even_with_a_known_artifact_id(accounts):
    from analyst_runtime.agent.analysis_context import AnalysisArtifactError, AnalysisArtifactStore

    root, owner, other, _, _, _ = accounts
    store = AnalysisArtifactStore(root)
    analysis_id = store.save_completed_answer(question="own uploaded file", answer="PRIVATE_ANSWER_473", data_manifest_sha256="a" * 64, confirmed_semantics_sha256="b" * 64, tools_used=["read_file"], evidence=[{"owner": owner}])
    assert store.root.is_relative_to(account_workspace().private)
    assert store.load(analysis_id)["population"]["answer"] == "PRIVATE_ANSWER_473"
    bind_account_workspace(root, other)
    with pytest.raises(AnalysisArtifactError):
        store.load(analysis_id)
    bind_account_workspace(root, owner)
    assert store.load(analysis_id)["population"]["answer"] == "PRIVATE_ANSWER_473"
