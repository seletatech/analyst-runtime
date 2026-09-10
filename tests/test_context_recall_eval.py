from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from analyst_runtime.agent.analysis_context import AnalysisArtifactStore
from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.bus.events import InboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from analyst_runtime.session.manager import Session

QUESTION = "请查询批次 `251103L1003A-1` 的完整生产档案"
ANSWER = "最终结论：批次 251103L1003A-1 的贴合、复卷、分切记录完整，PQC 与 OQC 均合格。"


def test_eval_question_fingerprint_preserves_batch_punctuation() -> None:
    assert AnalysisArtifactStore.question_hash(QUESTION) != AnalysisArtifactStore.question_hash(
        "请查询批次 `251103L1003A1` 的完整生产档案"
    )


def test_completed_answer_identity_includes_confirmed_semantics(tmp_path: Path) -> None:
    store = AnalysisArtifactStore(tmp_path)
    analysis_id = store.save_completed_answer(
        question=QUESTION,
        answer=ANSWER,
        data_manifest_sha256="a" * 64,
        confirmed_semantics_sha256="b" * 64,
        tools_used=["batch_lookup"],
        evidence=[{"tool_name": "batch_lookup", "output_sha256": "c" * 64}],
    )

    assert store.matches_completed_answer(
        analysis_id,
        question=QUESTION,
        data_manifest_sha256="a" * 64,
        confirmed_semantics_sha256="b" * 64,
    )
    assert not store.matches_completed_answer(
        analysis_id,
        question=QUESTION,
        data_manifest_sha256="a" * 64,
        confirmed_semantics_sha256="d" * 64,
    )


def _add_tool_heavy_turn(session: Session, turn: int, tool_calls: int = 30) -> None:
    session.add_event({"type": "user_input", "content": f"第 {turn} 轮问题"})
    for index in range(tool_calls):
        tool_id = f"turn-{turn}-tool-{index}"
        session.add_event(
            {
                "type": "llm_response",
                "content": f"核对第 {index} 项",
                "tool_calls": [
                    {
                        "id": tool_id,
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        )
        session.add_event(
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "tool_name": "exec",
                "content": f"第 {index} 项证据",
            }
        )
    session.add_event({"type": "final_response", "content": f"第 {turn} 轮最终答案"})


def test_eval_tool_heavy_multi_turn_history_keeps_recall() -> None:
    session = Session(key="web:recall-eval")
    for turn in range(1, 5):
        _add_tool_heavy_turn(session, turn)

    context: dict[str, Any] = {}
    history = session.get_history(max_messages=50, context=context)
    contents = [str(message.get("content") or "") for message in history]

    for turn in range(1, 5):
        assert f"第 {turn} 轮问题" in contents
        assert f"第 {turn} 轮最终答案" in contents
    assert not [message for message in history if message.get("role") == "tool"]
    assert context["strategy"] == "turn_outcomes"
    assert context["messages_before"] == 248
    assert context["messages_after"] == 8
    assert context["messages_dropped"] == 240
    assert context["turns_retained"] == 4
    assert len(context["messages_before_sha256"]) == 248
    assert len(context["messages_after_sha256"]) == 8


def test_eval_one_message_window_still_keeps_one_complete_turn() -> None:
    session = Session(key="web:small-window")
    session.add_event({"type": "user_input", "content": "问题"})
    session.add_event({"type": "final_response", "content": "答案"})
    session.add_event({"type": "user_input", "content": "追问"})
    session.add_event({"type": "final_response", "content": "答复"})

    assert session.get_history(max_messages=1) == [
        {"role": "user", "content": "追问"},
        {"role": "assistant", "content": "答复"},
    ]


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[list[dict[str, Any]]] = []
        self.tool_sets: list[list[dict[str, Any]] | None] = []
        self.responses = [
            LLMResponse(
                content="我核对已有资料。",
                tool_calls=[
                    ToolCallRequest(
                        id="batch-query",
                        name="batch_lookup",
                        arguments={"batch": "251103L1003A-1"},
                    )
                ],
            ),
            LLMResponse(content=ANSWER),
            LLMResponse(content=ANSWER),
            LLMResponse(
                content="数据版本已更新，我重新核对。",
                tool_calls=[
                    ToolCallRequest(
                        id="batch-refresh",
                        name="batch_lookup",
                        arguments={"batch": "251103L1003A-1"},
                    )
                ],
            ),
            LLMResponse(content=ANSWER),
        ]

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> LLMResponse:
        self.calls.append(messages)
        self.tool_sets.append(tools)
        return self.responses.pop(0)


class _BatchLookup(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "batch_lookup"

    @property
    def description(self) -> str:
        return "Look up one manufacturing batch."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"batch": {"type": "string"}},
            "required": ["batch"],
        }

    async def execute(self, **_: Any) -> str:
        self.calls += 1
        return json.dumps(
            {
                "status": "complete",
                "batch": "251103L1003A-1",
                "records": ["贴合", "复卷", "分切", "PQC 合格", "OQC 合格"],
            },
            ensure_ascii=False,
        )


def _trusted_message(run_id: str, content: str = QUESTION) -> InboundMessage:
    return InboundMessage(
        channel="web",
        sender_id=run_id,
        chat_id=f"chat-{run_id}",
        content=content,
        run_id=run_id,
        conversation_id="recall-eval",
        metadata={
            "model_profile_id": "glm-5.3-flash",
            "project_id": "linghui-ai-suite",
            "runtime": "linghui-dashboard-agent",
        },
    )


@pytest.mark.asyncio
async def test_eval_exact_question_reuses_answer_artifact_after_noisy_turns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GLM_5_3_FLASH_PROVIDER", "tokenhub")
    (tmp_path / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "trusted_gateway": {
                    "project_id": "linghui-ai-suite",
                    "runtime": "linghui-dashboard-agent",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "manifest.json").write_text(
        '{"release":"recall-eval"}', encoding="utf-8"
    )

    provider = _Provider()
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )
    lookup = _BatchLookup()
    agent.tools.register(lookup)
    session = agent.sessions.get_or_create("web:recall-eval")
    session.add_event({"type": "user_input", "content": QUESTION})
    session.add_event(
        {
            "type": "final_response",
            "content": "## 待确认的定义与口径\n\n- 查询对象：251103L1003A-1",
        }
    )
    agent.sessions.save(session)

    first = await agent._process_message(
        _trusted_message("run-1", "确认并按上述口径分析")
    )
    assert first is not None

    session = agent.sessions.get_or_create("web:recall-eval")
    first_analysis_id = str(session.metadata["active_analysis_id"])
    for turn in range(1, 5):
        _add_tool_heavy_turn(session, turn)
    data_release = agent._runtime_provenance()["workspace_data_manifest_sha256"]
    other_question = "请查询批次 `OTHER-1` 的完整生产档案"
    other_analysis_id = agent.analysis_artifacts.save_completed_answer(
        question=other_question,
        answer="OTHER-1 完整档案",
        data_manifest_sha256=data_release,
        confirmed_semantics_sha256="b" * 64,
        tools_used=["batch_lookup"],
        evidence=[{"tool_name": "batch_lookup", "output_sha256": "b" * 64}],
    )
    session.metadata["active_analysis_id"] = other_analysis_id
    session.metadata.setdefault("answer_artifacts", {})[
        agent.analysis_artifacts.completed_answer_key(
            other_question,
            data_release,
            "b" * 64,
        )
    ] = other_analysis_id
    session.add_event({"type": "user_input", "content": other_question})
    session.add_event({"type": "final_response", "content": "OTHER-1 完整档案"})
    agent.sessions.save(session)

    second = await agent._process_message(_trusted_message("run-2"))
    assert second is not None
    assert lookup.calls == 1
    assert provider.tool_sets[-1] == []
    assert ANSWER in json.dumps(provider.calls[-1], ensure_ascii=False)
    assert session.metadata["active_analysis_id"] == first_analysis_id

    prompt_trace = next(
        event for event in second.metadata["trace_summary"] if event["type"] == "prompt_snapshot"
    )
    model_trace = next(
        event for event in second.metadata["trace_summary"] if event["type"] == "model_call"
    )
    assert prompt_trace["context"]["history"]["strategy"] == "turn_outcomes"
    assert prompt_trace["context"]["input"]["roles"][-1] == "user"
    assert model_trace["context"]["message_count"] >= 3
    assert all("content" not in item for item in model_trace["context"]["messages"])

    (tmp_path / "data" / "manifest.json").write_text(
        '{"release":"recall-eval-updated"}', encoding="utf-8"
    )
    third = await agent._process_message(_trusted_message("run-3"))
    assert third is not None
    assert lookup.calls == 2
    assert provider.tool_sets[-2]
    assert not any(
        event["type"] == "analysis_context" and event.get("mode") == "reused"
        for event in third.metadata["trace_summary"]
    )
