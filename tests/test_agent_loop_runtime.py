from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.agent.tools.message import MessageTool
from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.config.schema import AgentDefaults
from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from analyst_runtime.providers.litellm_provider import LiteLLMProvider


class _SequenceProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[list[dict[str, Any]]] = []
        self.models: list[str | None] = []
        self.request_credentials: list[str] = []
        self.request_providers: list[str | None] = []
        self.reset_tokens: list[object | None] = []
        self.verified_credentials: list[tuple[str, str]] = []

    def set_request_credentials(
        self,
        *,
        api_key: str,
        api_base: str | None = None,
        provider: str | None = None,
    ) -> object:
        self.request_credentials.append(api_key)
        self.request_providers.append(provider)
        return "credential-token"

    def reset_request_credentials(self, token: object | None) -> None:
        self.reset_tokens.append(token)

    async def verify_request_credentials(self, *, api_key: str, provider: str) -> bool:
        self.verified_credentials.append((api_key, provider))
        return api_key == "valid-key"

    def get_default_model(self) -> str:
        return "test-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls.append(messages)
        self.models.append(model)
        return self.responses.pop(0)


def _message(chat_id: str) -> InboundMessage:
    return InboundMessage(
        channel="web",
        sender_id=chat_id,
        chat_id=chat_id,
        content=f"question for {chat_id}",
    )


@pytest.mark.asyncio
async def test_trusted_run_model_and_byok_are_request_scoped_and_never_echoed(
    tmp_path: Path,
) -> None:
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
    provider = _SequenceProvider([LLMResponse(content="done")])
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    message = InboundMessage(
        channel="web",
        sender_id="user",
        chat_id="chat-run",
        content="analyze",
        run_id="run-1",
        conversation_id="conversation-1",
        metadata={
            "_provider_credential": {
                "api_key": "byok-secret",
                "provider": "deepseek",
                "source": "byok",
            },
            "model_profile_id": "deepseek-chat",
            "project_id": "linghui-ai-suite",
            "runtime": "linghui-dashboard-agent",
        },
    )

    response = await agent._process_message(message)

    assert provider.models == ["deepseek-v4-flash"]
    assert provider.request_credentials == ["byok-secret"]
    assert provider.request_providers == ["deepseek"]
    assert provider.reset_tokens == ["credential-token"]
    assert "_provider_credential" not in message.metadata
    assert response is not None
    assert "_provider_credential" not in response.metadata
    assert "byok-secret" not in json.dumps(response.metadata)


@pytest.mark.asyncio
async def test_untrusted_messages_cannot_override_model_or_provider_credentials(
    tmp_path: Path,
) -> None:
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
    provider = _SequenceProvider([LLMResponse(content="done")])
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="glm-5.3-flash",
    )
    message = InboundMessage(
        channel="web",
        sender_id="user",
        chat_id="chat-run",
        content="analyze",
        metadata={
            "_provider_credential": {
                "api_key": "untrusted-secret",
                "provider": "deepseek",
                "source": "byok",
            },
            "model": "deepseek-v4-flash",
            "project_id": "different-project",
            "runtime": "linghui-dashboard-agent",
        },
    )

    response = await agent._process_message(message)

    assert provider.models == ["glm-5.3-flash"]
    assert provider.request_credentials == []
    assert provider.request_providers == []
    assert "_provider_credential" not in message.metadata
    assert response is not None
    assert "untrusted-secret" not in json.dumps(response.metadata)


@pytest.mark.asyncio
async def test_runtime_owns_profile_resolution_and_rejects_mismatched_byok(
    tmp_path: Path,
) -> None:
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
    provider = _SequenceProvider([LLMResponse(content="done")])
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    message = InboundMessage(
        channel="web",
        sender_id="user",
        chat_id="chat-run",
        content="analyze",
        run_id="run-1",
        conversation_id="conversation-1",
        metadata={
            "_provider_credential": {
                "api_key": "wrong-provider-key",
                "provider": "zhipu",
                "source": "byok",
            },
            "model_profile_id": "deepseek-chat",
            "project_id": "linghui-ai-suite",
            "runtime": "linghui-dashboard-agent",
        },
    )

    response = await agent._process_message(message)

    assert provider.models == []
    assert provider.request_credentials == []
    assert response is not None
    assert response.metadata["error_code"] == "ANALYST-RUNTIME-CREDENTIAL-001"


@pytest.mark.asyncio
async def test_runtime_verifies_provider_credentials_without_echoing_secret(
    tmp_path: Path,
) -> None:
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
    provider = _SequenceProvider([])
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    message = InboundMessage(
        channel="web",
        sender_id="credential-run",
        chat_id="credential-run",
        content="",
        metadata={
            "_provider_credential": {
                "api_key": "valid-key",
                "provider": "zhipu",
                "source": "byok",
            },
            "control": "verify_provider_credential",
            "project_id": "linghui-ai-suite",
            "runtime": "linghui-dashboard-agent",
        },
    )

    response = await agent._process_message(message)

    assert provider.verified_credentials == [("valid-key", "zhipu")]
    assert response is not None
    assert response.metadata == {
        "control": "provider_credential_verified",
        "verified": True,
    }
    assert "valid-key" not in json.dumps(response.metadata)


@pytest.mark.asyncio
async def test_runtime_emits_send_message_trace_before_final_response(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    provider = _SequenceProvider([LLMResponse(content="done")])
    agent = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)

    await agent._process_and_publish_message(_message("trace-chat"))

    trace = await bus.consume_outbound()
    final = await bus.consume_outbound()
    assert trace.content == "Send Message"
    assert trace.metadata == {
        "intermediate": True,
        "phase": "message",
        "terminal_action": "send_message",
    }
    assert final.content == "done"


@pytest.mark.asyncio
async def test_runtime_applies_steer_before_the_next_model_step(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = _SequenceProvider(
        [
            LLMResponse(content="I was about to answer."),
            LLMResponse(content="Updated answer for the latest three months."),
        ]
    )
    agent = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    execution_key = "web:run-123"
    agent._steer_queues[execution_key] = asyncio.Queue()
    agent._steer_queues[execution_key].put_nowait(
        InboundMessage(
            channel="web",
            sender_id="chat-run-123",
            chat_id="chat-run-123",
            content="只看最近三个月",
            run_id="run-123",
            conversation_id="conversation-456",
            metadata={"control": "steer", "steer_id": "steer-789"},
        )
    )

    result = await agent._run_agent_loop(
        [{"role": "user", "content": "分析全年趋势"}],
        execution_key=execution_key,
    )

    assert result.content == "Updated answer for the latest three months."
    assert any(
        message.get("role") == "user" and "只看最近三个月" in message.get("content", "")
        for message in provider.calls[1]
    )
    acknowledgement = await bus.consume_outbound()
    assert acknowledgement.metadata["control"] == "steer_applied"
    assert acknowledgement.metadata["steer_id"] == "steer-789"


@pytest.mark.asyncio
async def test_runtime_persists_steer_id_before_acknowledging_it(tmp_path: Path) -> None:
    bus = MessageBus()
    agent = AgentLoop(bus=bus, provider=_SequenceProvider([]), workspace=tmp_path)
    execution_key = "web:run-123"
    session = agent.sessions.get_or_create("web:conversation-456")
    agent._steer_queues[execution_key] = asyncio.Queue()
    steer = InboundMessage(
        channel="web",
        sender_id="chat-run-123",
        chat_id="chat-run-123",
        content="只看最近三个月",
        run_id="run-123",
        conversation_id="conversation-456",
        metadata={"control": "steer", "steer_id": "steer-durable"},
    )
    agent._steer_queues[execution_key].put_nowait(steer)

    updated = await agent._apply_pending_steers(
        execution_key,
        [{"role": "user", "content": "分析全年趋势"}],
        session=session,
        parent_uuid="request-1",
    )

    assert updated is not None
    persisted = agent.sessions._load("web:conversation-456")
    assert persisted is not None
    assert "steer-durable" in persisted.metadata["applied_steer_ids"]
    assert any(event.get("steer_id") == "steer-durable" for event in persisted.events)
    assert agent._steer_was_applied(steer) is True
    acknowledgement = await bus.consume_outbound()
    assert acknowledgement.metadata["control"] == "steer_applied"


@pytest.mark.asyncio
async def test_trusted_read_file_returns_audited_content_and_rejects_unapproved_paths(
    tmp_path: Path,
) -> None:
    upload = tmp_path / "uploads" / "user" / "conversation" / "version"
    upload.mkdir(parents=True)
    uploaded_file = upload / "record.txt"
    uploaded_file.write_text("批次 A123：待复核", encoding="utf-8")
    import hashlib

    content_hash = hashlib.sha256(uploaded_file.read_bytes()).hexdigest()
    (upload / ".manifest.json").write_text(
        json.dumps(
            {
                "content_sha256": content_hash,
                "conversation_id": "conversation",
                "created_at": "2026-09-02T08:00:00.000Z",
                "schema_version": "linghui-workspace-upload/v1",
                "user_id": "user",
                "version": "version",
                "workspace_path": str(uploaded_file),
            }
        ),
        encoding="utf-8",
    )
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_SequenceProvider([]),
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )
    agent._set_tool_context(
        "web",
        "chat-run",
        analysis_conversation_id="conversation",
    )

    result = json.loads(await agent.tools.execute("read_file", {"path": str(uploaded_file)}))
    agent._set_tool_context(
        "web",
        "chat-run",
        analysis_conversation_id="other-conversation",
    )
    cross_conversation = json.loads(
        await agent.tools.execute("read_file", {"path": str(uploaded_file)})
    )
    denied = json.loads(await agent.tools.execute("read_file", {"path": str(outside)}))

    assert result["status"] == "ok"
    assert result["content"] == "批次 A123：待复核"
    assert result["content_sha256"] == content_hash
    assert result["version"] == "version"
    assert result["read_at"].endswith("Z")
    assert cross_conversation["status"] == "denied"
    assert denied["status"] == "denied"


@pytest.mark.asyncio
async def test_trusted_read_file_reads_built_in_business_data_without_upload_manifest(
    tmp_path: Path,
) -> None:
    business_file = tmp_path / "data" / "production-records" / "record.json"
    business_file.parent.mkdir(parents=True)
    business_file.write_text('{"product":"HUD-70538"}', encoding="utf-8")
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_SequenceProvider([]),
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )

    result = await agent.tools.execute("read_file", {"path": str(business_file)})

    assert result == '{"product":"HUD-70538"}'


@pytest.mark.asyncio
async def test_trusted_agent_can_choose_read_file_and_answer_from_uploaded_content(
    tmp_path: Path,
) -> None:
    upload = tmp_path / "uploads" / "user" / "conversation" / "version"
    upload.mkdir(parents=True)
    uploaded_file = upload / "record.txt"
    uploaded_file.write_text("批次 A123 的结论是待复核。", encoding="utf-8")
    import hashlib

    content_hash = hashlib.sha256(uploaded_file.read_bytes()).hexdigest()
    (upload / ".manifest.json").write_text(
        json.dumps(
            {
                "content_sha256": content_hash,
                "conversation_id": "conversation",
                "created_at": "2026-09-02T08:00:00.000Z",
                "schema_version": "linghui-workspace-upload/v1",
                "user_id": "user",
                "version": "version",
                "workspace_path": str(uploaded_file),
            }
        ),
        encoding="utf-8",
    )
    provider = _SequenceProvider(
        [
            LLMResponse(
                content="我先读取附件。",
                tool_calls=[
                    ToolCallRequest(
                        id="read-1",
                        name="read_file",
                        arguments={"path": str(uploaded_file)},
                    )
                ],
                usage={"prompt_tokens": 100, "completion_tokens": 10},
            ),
            LLMResponse(
                content="最终结论：根据已读取文件，批次 A123 的结论是待复核。",
                finish_reason="stop",
                usage={"prompt_tokens": 180, "completion_tokens": 20},
            ),
        ]
    )
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )
    agent._set_tool_context(
        "web",
        "chat-run",
        analysis_conversation_id="conversation",
    )

    result = await agent._run_agent_loop(
        [
            {
                "role": "user",
                "content": f"请读取 {uploaded_file} 并告诉我 A123 的结论。",
            }
        ]
    )

    assert result.content == "最终结论：根据已读取文件，批次 A123 的结论是待复核。"
    assert result.tools_used == ["read_file"]
    assert "批次 A123 的结论是待复核" in str(provider.calls[1])
    assert result.usage == {"prompt_tokens": 280, "completion_tokens": 30}


@pytest.mark.asyncio
async def test_readonly_tool_profile_exposes_no_tools_or_mcp(tmp_path: Path) -> None:
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_SequenceProvider([]),
        workspace=tmp_path,
        tool_profile="readonly",
        mcp_servers={"untrusted": {"command": "must-not-run"}},
    )

    assert agent.tools.tool_names == []
    await agent._connect_mcp()
    assert agent._mcp_connected is False


def test_trusted_analysis_profile_exposes_only_product_agent_tools(tmp_path: Path) -> None:
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_SequenceProvider([]),
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )

    assert set(agent.tools.tool_names) == {
        "read_file",
        "write_file",
        "append_file",
        "patch_file",
        "edit_file",
        "list_dir",
        "exec",
        "web_fetch",
        "firecrawl_search",
        "firecrawl_scrape",
        "firecrawl_browser",
        "message",
    }
    assert not {
        "propose_manufacturing_semantics",
        "confirm_manufacturing_semantics",
        "transcribe_audio",
        "text_to_speech",
        "analyze_image",
        "composio_search_tools",
        "composio_manage_connections",
        "composio_execute_tools",
        "gateway_auth",
        "notion",
        "youtube",
        "google_maps",
        "slack",
        "teams",
        "spawn",
        "cron",
    }.intersection(agent.tools.tool_names)


@pytest.mark.asyncio
async def test_trusted_analysis_spawned_tools_remain_workspace_scoped(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_SequenceProvider([]),
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )

    tools = agent.subagents._build_tools()
    result = await tools.execute("read_file", {"path": str(outside)})

    assert result.startswith("Error:")
    assert "outside allowed" in result


@pytest.mark.asyncio
async def test_agent_response_preserves_run_and_conversation_identity(
    tmp_path: Path,
) -> None:
    agent = AgentLoop(
        bus=MessageBus(),
        provider=_SequenceProvider([LLMResponse(content="分析完成", finish_reason="stop")]),
        workspace=tmp_path,
    )
    message = InboundMessage(
        channel="web",
        sender_id="chat-run-123",
        chat_id="chat-run-123",
        content="继续分析",
        conversation_id="conversation-456",
        run_id="run-123",
    )

    response = await agent._process_message(message)

    assert response is not None
    assert response.run_id == "run-123"
    assert response.conversation_id == "conversation-456"
    assert response.chat_id == "chat-run-123"
    assert response.metadata["usage"]["model_call_count"] == 1
    assert response.metadata["usage"]["application_retry_count"] == 0
    assert response.metadata["usage"]["provider_retry_count"] == 0
    assert response.metadata["usage"]["retry_count"] == 0


@pytest.mark.asyncio
async def test_tool_iteration_exhaustion_returns_support_error_code(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider(
        [
            LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="exec",
                        arguments={"command": "printf inspected"},
                    )
                ],
            )
        ]
    )
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        max_iterations=1,
    )

    response = await agent._process_message(_message("iteration-budget"))

    assert response is not None
    assert response.content == (
        "分析未完成。Error Code: ANALYST-RUNTIME-ITERATION-001。请将此错误码提供给技术支持。"
    )
    assert response.metadata["error_code"] == "ANALYST-RUNTIME-ITERATION-001"


@pytest.mark.asyncio
async def test_tool_protocol_abort_is_not_mislabeled_as_iteration_exhaustion(
    tmp_path: Path,
) -> None:
    malformed = ToolCallRequest(
        id="call-1",
        name="tooluse_1234567890",
        arguments={},
    )
    provider = _SequenceProvider(
        [
            LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[malformed]),
            LLMResponse(content=None, finish_reason="tool_calls", tool_calls=[malformed]),
        ]
    )
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        max_iterations=200,
    )

    response = await agent._process_message(_message("malformed-tools"))

    assert response is not None
    assert response.metadata["error_code"] == "ANALYST-RUNTIME-PROTOCOL-001"
    assert "ANALYST-RUNTIME-ITERATION-001" not in response.content


@pytest.mark.asyncio
async def test_message_tool_routing_context_is_isolated_per_chat_task() -> None:
    delivered: list[tuple[str, str]] = []

    async def send(message: OutboundMessage) -> None:
        delivered.append((message.chat_id, message.content))

    tool = MessageTool(send_callback=send)
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()

    async def execute_for(
        chat_id: str,
        mine: asyncio.Event,
        other: asyncio.Event,
    ) -> None:
        tool.set_context("web", chat_id)
        mine.set()
        await other.wait()
        await tool.execute(content=f"answer for {chat_id}")

    await asyncio.gather(
        execute_for("first", first_ready, second_ready),
        execute_for("second", second_ready, first_ready),
    )

    assert sorted(delivered) == [
        ("first", "answer for first"),
        ("second", "answer for second"),
    ]


@pytest.mark.asyncio
async def test_agent_loop_does_not_block_other_chats_behind_a_long_run(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    provider = _SequenceProvider([])
    agent = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def connect_mcp() -> None:
        return None

    async def process(msg: InboundMessage) -> OutboundMessage:
        if msg.chat_id == "first":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="done",
        )

    agent._connect_mcp = connect_mcp  # type: ignore[method-assign]
    agent._process_message = process  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(_message("first"))
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await bus.publish_inbound(_message("second"))

        await asyncio.wait_for(second_started.wait(), timeout=0.5)
    finally:
        release_first.set()
        agent.stop()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task


@pytest.mark.asyncio
async def test_agent_loop_serializes_messages_within_the_same_chat(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    agent = AgentLoop(bus=bus, provider=_SequenceProvider([]), workspace=tmp_path)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def connect_mcp() -> None:
        return None

    call_count = 0

    async def process(msg: InboundMessage) -> OutboundMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="done")

    agent._connect_mcp = connect_mcp  # type: ignore[method-assign]
    agent._process_message = process  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(_message("same-chat"))
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await bus.publish_inbound(_message("same-chat"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_started.wait(), timeout=0.05)

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=0.5)
    finally:
        release_first.set()
        agent.stop()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task


@pytest.mark.asyncio
async def test_agent_loop_serializes_distinct_runs_in_one_conversation(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    agent = AgentLoop(bus=bus, provider=_SequenceProvider([]), workspace=tmp_path)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def connect_mcp() -> None:
        return None

    async def process(msg: InboundMessage) -> OutboundMessage:
        if msg.run_id == "run-1":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="done",
            conversation_id=msg.conversation_id,
            run_id=msg.run_id,
        )

    agent._connect_mcp = connect_mcp  # type: ignore[method-assign]
    agent._process_message = process  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="web",
                sender_id="chat-run-1",
                chat_id="chat-run-1",
                content="first",
                conversation_id="conversation-456",
                run_id="run-1",
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        await bus.publish_inbound(
            InboundMessage(
                channel="web",
                sender_id="chat-run-2",
                chat_id="chat-run-2",
                content="second",
                conversation_id="conversation-456",
                run_id="run-2",
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(second_started.wait(), timeout=0.05)

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=0.5)
    finally:
        release_first.set()
        agent.stop()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task


def test_default_compaction_threshold_leaves_deepseek_headroom() -> None:
    defaults = AgentDefaults()

    assert defaults.max_tool_iterations == 500
    assert defaults.context_compact_threshold == 80_000
    assert defaults.context_compact_threshold + defaults.max_tokens < 128_000


def test_deepseek_cache_usage_is_preserved_and_logged(tmp_path: Path) -> None:
    cache_log = tmp_path / "cache_stats.jsonl"
    provider = LiteLLMProvider(
        default_model="deepseek/deepseek-v4-flash",
        cache_log_path=cache_log,
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="done",
                    reasoning_content=None,
                    tool_calls=None,
                ),
            )
        ],
        id="response-1",
        model="deepseek-v4-flash",
        usage=SimpleNamespace(
            prompt_tokens=1_000,
            completion_tokens=25,
            total_tokens=1_025,
            prompt_cache_hit_tokens=900,
            prompt_cache_miss_tokens=100,
        ),
    )

    parsed = provider._parse_response(response)

    assert parsed.usage == {
        "prompt_tokens": 1_000,
        "completion_tokens": 25,
        "total_tokens": 1_025,
        "prompt_cache_hit_tokens": 900,
        "prompt_cache_miss_tokens": 100,
        "cache_read_tokens": 900,
        "cache_write_tokens": 0,
    }
    logged = json.loads(cache_log.read_text(encoding="utf-8"))
    assert logged == {
        "ts": logged["ts"],
        "model": "deepseek-v4-flash",
        "prompt_tokens": 1_000,
        "completion_tokens": 25,
        "cache_write_tokens": 0,
        "cache_read_tokens": 900,
    }


@pytest.mark.asyncio
async def test_litellm_reports_transport_retries(monkeypatch) -> None:
    from analyst_runtime.providers import litellm_provider

    attempts = 0

    async def fake_completion(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Unable to get json response - Expecting value")
        message = SimpleNamespace(content="recovered", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(litellm_provider, "acompletion", fake_completion)
    monkeypatch.setattr(litellm_provider, "_LLM_RETRY_DELAYS_SECONDS", (0, 0))
    provider = LiteLLMProvider(default_model="deepseek/deepseek-v4-flash")

    response = await provider.chat(messages=[{"role": "user", "content": "analyze"}])

    assert attempts == 2
    assert response.retry_count == 1


@pytest.mark.asyncio
async def test_length_continuation_never_sends_an_empty_assistant_message(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider(
        [
            LLMResponse(
                content=None,
                reasoning_content="private reasoning only",
                finish_reason="length",
            ),
            LLMResponse(content="finished", finish_reason="stop"),
        ]
    )
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    result, _ = await agent._run_agent_loop([{"role": "user", "content": "do a long analysis"}])

    assert result == "finished"
    second_request = provider.calls[1]
    assistant_messages = [
        message for message in second_request if message.get("role") == "assistant"
    ]
    assert assistant_messages
    assert assistant_messages[-1].get("content")


@pytest.mark.asyncio
async def test_agent_loop_aggregates_actual_provider_usage_across_iterations(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider(
        [
            LLMResponse(
                content="partial",
                finish_reason="length",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_cache_hit_tokens": 80,
                    "prompt_cache_miss_tokens": 20,
                },
            ),
            LLMResponse(
                content="finished",
                finish_reason="stop",
                usage={
                    "prompt_tokens": 150,
                    "completion_tokens": 30,
                    "prompt_cache_hit_tokens": 100,
                    "prompt_cache_miss_tokens": 50,
                },
            ),
        ]
    )
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    result = await agent._run_agent_loop([{"role": "user", "content": "do a long analysis"}])

    assert result.usage == {
        "prompt_tokens": 250,
        "completion_tokens": 50,
        "prompt_cache_hit_tokens": 180,
        "prompt_cache_miss_tokens": 70,
    }
    assert result.model_call_count == 2
    assert result.application_retry_count == 1
    assert result.provider_retry_count == 0
    assert result.retry_count == 1


@pytest.mark.asyncio
async def test_agent_loop_counts_provider_transport_retries(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [
            LLMResponse(
                content="finished",
                finish_reason="stop",
                retry_count=2,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        ]
    )
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)

    result = await agent._run_agent_loop([{"role": "user", "content": "analyze"}])

    assert result.model_call_count == 3
    assert result.application_retry_count == 0
    assert result.provider_retry_count == 2
    assert result.retry_count == 2


@pytest.mark.asyncio
async def test_active_context_is_compacted_after_token_threshold(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider(
        [
            LLMResponse(
                content="partial result",
                finish_reason="length",
                usage={"prompt_tokens": 11},
            ),
            LLMResponse(content="preserved facts and unfinished work", finish_reason="stop"),
            LLMResponse(content="finished after compacting", finish_reason="stop"),
        ]
    )
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_compact_threshold=10,
        context_compact_keep_messages=1,
        consolidation_model="different-maintenance-model",
    )

    loop_result = await agent._run_agent_loop(
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "OLD RAW CONTEXT"},
        ]
    )

    assert loop_result.content == "finished after compacting"
    assert loop_result.model_call_count == 3
    assert loop_result.retry_count == 1
    assert loop_result.usage == {"prompt_tokens": 11}
    assert provider.models == ["test-model", "test-model", "test-model"]
    final_request = provider.calls[2]
    rendered = str(final_request)
    assert "preserved facts and unfinished work" in rendered
    assert "OLD RAW CONTEXT" not in rendered
    assert final_request[0] == {"role": "system", "content": "system rules"}


@pytest.mark.asyncio
async def test_trusted_analysis_does_not_launch_unmetered_model_maintenance(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider([LLMResponse(content="finished", finish_reason="stop")])
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        tool_profile="trusted-analysis",
        memory_window=1,
        compress_after_turns=1,
        compress_keep_turns=1,
        consolidation_interval=0,
    )
    message = _message("metered-chat")
    session = agent.sessions.get_or_create(message.session_key)
    for index in range(2):
        session.add_event(
            {
                "content": f"old question {index}",
                "parent_uuid": None,
                "type": "user_input",
                "uuid": f"user-{index}",
            }
        )
        session.add_event(
            {
                "content": f"old answer {index}",
                "parent_uuid": f"user-{index}",
                "type": "final_response",
                "uuid": f"answer-{index}",
            }
        )

    response = await agent._process_message(message)

    assert response is not None
    assert len(provider.calls) == 1
    assert response.metadata["usage"]["model_call_count"] == 1


@pytest.mark.asyncio
async def test_trusted_analysis_new_session_does_not_call_consolidation_model(
    tmp_path: Path,
) -> None:
    provider = _SequenceProvider([])
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )
    message = _message("new-session")
    message.content = "/new"

    response = await agent._process_message(message)

    assert response is not None
    assert response.content == "New session started."
    assert provider.calls == []


@pytest.mark.asyncio
async def test_compaction_keeps_tool_call_and_result_together(tmp_path: Path) -> None:
    provider = _SequenceProvider([LLMResponse(content="summary", finish_reason="stop")])
    agent = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_compact_keep_messages=1,
    )
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    compacted = await agent._compact_active_context(messages, model="test-model")

    tail = compacted[-2:]
    assert [message["role"] for message in tail] == ["assistant", "tool"]


@pytest.mark.asyncio
async def test_short_but_oversized_exchange_is_fully_compacted(tmp_path: Path) -> None:
    provider = _SequenceProvider(
        [LLMResponse(content="summary of the large result", finish_reason="stop")]
    )
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "analysis"},
        {"role": "tool", "content": "very large result"},
    ]

    compacted = await agent._compact_active_context(messages, model="test-model")

    assert [message["role"] for message in compacted] == [
        "system",
        "user",
        "assistant",
    ]
    assert "summary of the large result" in str(compacted)
    assert "very large result" not in str(compacted)
