import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from analyst_runtime.agent.tools.documents import archive_entries, read_document
from analyst_runtime.agent.tools.filesystem import ReadFileTool

FIXTURES = Path(__file__).parent / "fixtures/documents"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix", ["doc", "docx", "rtf", "odt", "xls", "xlsx", "ods", "ppt", "pptx", "odp"]
)
async def test_real_office_pages(suffix):
    data = (FIXTURES / ("sample." + suffix)).read_bytes()
    result = await read_document(
        data,
        suffix="." + suffix,
        page=1,
        member=None,
        vision=True,
        source={"path": "uploaded." + suffix, "content_sha256": hashlib.sha256(data).hexdigest()},
    )
    parsed = json.loads(result)
    assert "6397" in parsed["content"]
    assert parsed["page_count"] >= 1
    assert result.image_blocks[-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parsed["content_sha256"] == hashlib.sha256(data).hexdigest()


def zipped(entries):
    out = io.BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return out.getvalue()


@pytest.mark.asyncio
async def test_zip_list_exact_member_and_html_inert():
    data = zipped(
        [
            (
                "中文目录/report.html",
                b"<script>SECRET SCRIPT</script><table><tr><td>Total</td><td>6397</td></tr></table>",
            ),
            ("document.docx", (FIXTURES / "sample.docx").read_bytes()),
        ]
    )
    source = {"path": "uploaded.zip", "content_sha256": hashlib.sha256(data).hexdigest()}
    listed = json.loads(
        await read_document(data, suffix=".zip", page=1, member=None, vision=False, source=source)
    )
    assert len(listed["entries"]) == 2 and "content" not in listed
    read = json.loads(
        await read_document(
            data, suffix=".zip", page=1, member="中文目录/report.html", vision=False, source=source
        )
    )
    assert "6397" in read["content"] and "SECRET SCRIPT" not in read["content"]
    assert (
        read["archive_member"] == "中文目录/report.html"
        and read["content_sha256"] == source["content_sha256"]
    )
    office = await read_document(
        data, suffix=".zip", page=1, member="document.docx", vision=True, source=source
    )
    assert "6397" in json.loads(office)["content"] and office.image_blocks
    with pytest.raises(ValueError, match="not found"):
        await read_document(
            data, suffix=".zip", page=1, member="../missing", vision=False, source=source
        )


@pytest.mark.parametrize(
    "name", ["../escape.txt", "/tmp/escape.txt", "C:/escape.txt", "bad\\escape.txt"]
)
def test_zip_traversal(name):
    with pytest.raises(ValueError, match="Unsafe"):
        archive_entries(zipped([(name, b"evidence")]))


def test_zip_bomb_and_symlink():
    with pytest.raises(ValueError, match="compression"):
        archive_entries(zipped([("huge.txt", b"a" * 200000)]))
    link = ZipInfo("link")
    link.external_attr = 0o120777 << 16
    with pytest.raises(ValueError, match="Unsafe"):
        archive_entries(zipped([(link, b"/etc/passwd")]))
    with pytest.raises(ValueError, match="200 entries"):
        archive_entries(zipped([(str(n), b"x") for n in range(201)]))


@pytest.mark.asyncio
async def test_upload_identity_before_archive_read(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_RUNTIME_UPLOAD_MANIFEST_SCHEMA", "linghui-workspace-upload/v1")
    f = tmp_path / "uploads/user/chat/version/sample.zip"
    f.parent.mkdir(parents=True)
    f.write_bytes(zipped([("note.unknown", b"HELLO 6397")]))
    manifest = {
        "schema_version": "linghui-workspace-upload/v1",
        "user_id": "user",
        "conversation_id": "chat",
        "version": "version",
        "workspace_path": str(f),
        "content_sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
    }
    (f.parent / ".manifest.json").write_text(json.dumps(manifest))
    tool = ReadFileTool(allowed_dir=tmp_path, audit_results=True)
    tool.set_conversation_context("other")
    assert json.loads(await tool.execute(str(f), member="note.unknown"))["status"] == "denied"
    tool.set_conversation_context("chat")
    assert "HELLO 6397" in json.loads(await tool.execute(str(f), member="note.unknown"))["content"]
    f.write_bytes(f.read_bytes() + b"tamper")
    assert json.loads(await tool.execute(str(f)))["status"] == "integrity_error"


@pytest.mark.skipif(os.sys.platform != "linux", reason="Linux parser sandbox")
def test_native_parser_cannot_read_unrelated_files_or_create_internet_socket(tmp_path):
    from analyst_runtime.agent.tools.document_isolation import isolate_office

    private = tmp_path / "private"
    private.write_text("SECRET_OTHER_CONVERSATION")
    sandbox = tmp_path / "parser"
    sandbox.mkdir()
    r = subprocess.run(
        ["/bin/cat", str(private)],
        capture_output=True,
        preexec_fn=lambda: isolate_office(str(sandbox)),
    )
    assert r.returncode != 0 and b"SECRET" not in r.stdout

    # Test socket creation in the isolated child before exec, with an exit code as the assertion.
    def check_network():
        isolate_office(str(sandbox))
        import socket

        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        except PermissionError:
            os._exit(0)
        os._exit(9)

    assert subprocess.run(["/bin/true"], preexec_fn=check_network).returncode == 0
