import hashlib
import json

from analyst_runtime.agent.citation_refs import citation_from_read, cited_refs, uncited_summary


def test_summary_guard_requires_an_issued_citation_in_the_summary():
    token = f"[[cite:{'a' * 24}]]"
    issued = {'a' * 24}
    assert uncited_summary(f"## 结论\n\n客户待验证。\n\n## 事实\n客户待验证 {token}", issued)
    assert not uncited_summary(f"**结论**：客户待验证 {token}\n\n## 事实\n客户待验证 {token}", issued)
    assert not uncited_summary("## 结论\n\n纯建议。", issued)
    assert not uncited_summary(f"## 事实\n客户待验证 {token}", issued)
    assert not uncited_summary(f"## 结论\n\n客户待验证。\n\n## 事实\n{token}", set())


def test_audited_read_requires_same_conversation_and_unchanged_file(tmp_path):
    path = tmp_path / 'uploads' / 'owner' / 'conversation' / 'version' / 'input.pdf'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'original')
    result = {'schema_version': 'analyst-read-result/v1', 'status': 'ok',
              'conversation_id': 'conversation', 'version': 'version',
              'path': str(path), 'content_sha256': hashlib.sha256(b'original').hexdigest(),
              'format': 'pdf', 'page': 2, 'content': 'page text'}
    encoded = json.dumps(result)
    ref = citation_from_read(encoded, tmp_path, 'conversation', 'tool1')
    assert ref and ref['page'] == 2 and ref['sha256'] == result['content_sha256']
    assert citation_from_read(encoded, tmp_path, 'other', 'tool1') is None
    assert citation_from_read(json.dumps({**result, 'format': 'zip'}), tmp_path, 'conversation', 'tool1') is None
    assert citation_from_read(json.dumps({**result, 'content': ''}), tmp_path, 'conversation', 'tool1') is None
    path.write_bytes(b'changed')
    assert citation_from_read(encoded, tmp_path, 'conversation', 'tool1') is None


def test_only_referenced_successful_reads_are_returned():
    first = {'id': 'a' * 24}
    second = {'id': 'b' * 24}
    events = [{'type': 'tool_result', 'status': 'completed', 'citation_ref': first},
              {'type': 'tool_result', 'status': 'failed', 'citation_ref': second}]
    assert cited_refs(events, f"事实 [[cite:{first['id']}]] 再次 [[cite:{first['id']}]] [[cite:{second['id']}]]") == [first]


def test_excel_ref_records_exact_range(tmp_path):
    path = tmp_path / 'uploads' / 'owner' / 'conversation' / 'version' / 'input.xlsx'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'workbook')
    result = {'schema_version': 'analyst-read-result/v1', 'status': 'ok',
              'conversation_id': 'conversation', 'version': 'version', 'path': str(path),
              'content_sha256': hashlib.sha256(b'workbook').hexdigest(), 'format': 'excel',
              'sheet': '销售', 'cells': 'B4:F4', 'content': [{'column_1': '小米'}]}
    ref = citation_from_read(json.dumps(result), tmp_path, 'conversation', 'tool1')
    assert ref and ref['sheet'] == '销售' and ref['cells'] == 'B4:F4'
    assert citation_from_read(json.dumps({**result, 'cells': 'B4:F9999999'}), tmp_path, 'conversation', 'tool1') is None


def test_history_refs_only_expose_unique_retained_assistant_answers():
    from analyst_runtime.agent.citation_refs import annotate_history
    history = [{'role': 'user', 'content': '请计划'}, {'role': 'assistant', 'content': '周计划'},
               {'role': 'assistant', 'content': '重复'}, {'role': 'assistant', 'content': '中间过程', 'tool_calls': [{'id': '1'}]}]
    events = [{'type': 'final_response', 'content': '周计划'}, {'type': 'final_response', 'content': '重复'},
              {'type': 'final_response', 'content': '重复'}]
    annotated, refs = annotate_history(history, events, 'chat')
    assert len(refs) == 1 and refs[0]['content'] == '周计划'
    assert f"[[cite:{refs[0]['id']}]]" in annotated[1]['content']
    assert annotated[2] == history[2] and annotated[3] == history[3]
    assert cited_refs([], f"基于前一版 [[cite:{refs[0]['id']}]]", refs) == refs
    assert annotate_history(history, events, 'other')[1][0]['id'] != refs[0]['id']


def test_account_scope_allows_owned_history_but_rejects_other_owner(tmp_path):
    from analyst_runtime.agent.tools.account_access import bind_account_workspace
    owner = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    other = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    path = tmp_path / 'uploads' / owner / 'old-chat' / 'version' / 'file.txt'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'original')
    result = {'schema_version': 'analyst-read-result/v1', 'status': 'ok',
              'conversation_id': 'old-chat', 'version': 'version', 'path': str(path),
              'source_pathname': f'{owner}--local--file.txt',
              'content_sha256': hashlib.sha256(b'original').hexdigest(),
              'format': 'text', 'page': 1, 'content': 'original'}
    try:
        bind_account_workspace(tmp_path, owner)
        ref = citation_from_read(json.dumps(result), tmp_path, 'new-chat', 'call')
        assert ref and ref['source_pathname'] == result['source_pathname']
        bind_account_workspace(tmp_path, other)
        assert citation_from_read(json.dumps(result), tmp_path, 'new-chat', 'call') is None
    finally:
        bind_account_workspace(tmp_path, None)



def test_one_sentence_summary_also_requires_a_source():
    token = '[[cite:'+'a'*24+']]'
    assert uncited_summary('**一句话结论**：original fact.\n\n正文 '+token, {'a'*24})


def test_numeric_summary_without_any_tokens_still_requires_read_evidence():
    issued = {'a' * 24}
    assert uncited_summary('**一句话结论**：当天产出 12 m，投入 15 m。\n\n正文未附任何标识。', issued)
    assert not uncited_summary('**一句话结论**：请与负责人确认。', issued)
    assert not uncited_summary('**一句话结论**：当天产出 12 m。', set())
