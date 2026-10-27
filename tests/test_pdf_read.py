import hashlib
import json

import pytest

from analyst_runtime.agent.tools.filesystem import ReadFileTool
from analyst_runtime.agent.tools.pdf import PdfReadResult, read_pdf_page
from analyst_runtime.providers.registry import model_supports_vision, resolve_model_profile


def pdf_bytes():
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    text = b"BT /F1 18 Tf 20 120 Td (Batch A123: 12.50 kg) Tj ET"
    objects.append(
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream"
    )
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 7 0 R >>"
    )
    text = b"BT /F1 18 Tf 20 120 Td (Page TWO: 99.25 kg) Tj ET"
    objects.append(
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream"
    )
    result = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return (
        result
        + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
    )


def test_vision_catalog():
    profile = resolve_model_profile("deepseek-v4.1-flash-1017")
    assert profile.provider == "nebius"
    assert profile.model == "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    assert model_supports_vision(profile.model)
    assert model_supports_vision("openai/" + profile.model)
    assert not model_supports_vision("deepseek-ai/DeepSeek-V4-Flash-0731")


@pytest.mark.asyncio
async def test_pdf_page_text_and_real_render():
    result = await read_pdf_page(pdf_bytes(), page=2, vision=True, source={"path": "test.pdf"})
    assert isinstance(result, PdfReadResult)
    parsed = json.loads(result)
    assert parsed["page_count"] == 2 and parsed["page"] == 2
    assert "99.25" in parsed["content"] and "12.50" not in parsed["content"]
    assert result.image_blocks[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert "base64" not in str(result)
    text_only = await read_pdf_page(pdf_bytes(), page=1, vision=False, source={"path": "test.pdf"})
    assert not text_only.image_blocks
    assert json.loads(text_only)["visual_status"] == "unavailable"
    for page in [0, -1, 3]:
        with pytest.raises(ValueError):
            await read_pdf_page(pdf_bytes(), page=page, vision=True, source={"path": "test.pdf"})
    with pytest.raises(ValueError):
        await read_pdf_page(b"%PDF-broken", page=1, vision=True, source={"path": "test.pdf"})


@pytest.mark.asyncio
async def test_pdf_upload_authorization_and_checksum_precede_render(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_RUNTIME_UPLOAD_MANIFEST_SCHEMA", "linghui-workspace-upload/v1")
    file = tmp_path / "uploads/user/conversation/version/scan.pdf"
    file.parent.mkdir(parents=True)
    file.write_bytes(pdf_bytes())
    manifest = dict(
        schema_version="linghui-workspace-upload/v1",
        user_id="user",
        conversation_id="conversation",
        version="version",
        workspace_path=str(file),
        content_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
    )
    (file.parent / ".manifest.json").write_text(json.dumps(manifest))
    tool = ReadFileTool(allowed_dir=tmp_path, audit_results=True)
    tool.set_vision_context(True)
    tool.set_conversation_context("other")
    result = await tool.execute(str(file))
    assert not isinstance(result, PdfReadResult)
    assert json.loads(result)["status"] == "denied"
    tool.set_conversation_context("conversation")
    assert isinstance(await tool.execute(str(file)), PdfReadResult)
    file.write_bytes(pdf_bytes() + b"changed")
    assert json.loads(await tool.execute(str(file)))["status"] == "integrity_error"
    link = file.parent / "link.pdf"
    link.symlink_to(file)
    assert json.loads(await tool.execute(str(link)))["status"] == "denied"
