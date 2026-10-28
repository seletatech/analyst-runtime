import hashlib
import json
import zipfile

import pytest

from analyst_runtime.agent.citation_refs import citation_from_read
from analyst_runtime.agent.tools.filesystem import ReadFileTool


@pytest.mark.asyncio
async def test_excel_read_requires_audited_upload_and_bounded_range(tmp_path, monkeypatch):
    file = tmp_path / 'uploads' / 'user' / 'chat' / 'version' / 'book.xlsx'
    file.parent.mkdir(parents=True)
    with zipfile.ZipFile(file, 'w') as archive:
        archive.writestr('xl/workbook.xml', 'test workbook')
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    (file.parent / '.manifest.json').write_text(json.dumps({
        'schema_version': 'analyst-runtime-workspace-upload/v1', 'user_id': 'user',
        'conversation_id': 'chat', 'version': 'version', 'workspace_path': str(file),
        'content_sha256': digest,
    }))
    binary = tmp_path / 'excel-mcp'
    binary.write_text('#!/bin/sh\nread input\nprintf \'{"sheet_name":"Sales","range":"B2:C2","rows":[{"column_1":"customer","column_2":42}]}\'\n')
    binary.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    tool = ReadFileTool(allowed_dir=tmp_path, audit_results=True)
    tool.set_conversation_context('chat')
    result = await tool.execute(str(file), sheet='Sales', cells='B2:C2')
    parsed = json.loads(result)
    assert parsed['content'][0]['column_2'] == 42
    ref = citation_from_read(result, tmp_path, 'chat', 'call')
    assert ref and (ref['sheet'], ref['cells']) == ('Sales', 'B2:C2')
    assert json.loads(await tool.execute(str(file), sheet='Sales', cells='A1:Z100'))['status'] == 'error'
    tool.set_conversation_context('another-chat')
    assert json.loads(await tool.execute(str(file), sheet='Sales', cells='B2:C2'))['status'] == 'denied'
