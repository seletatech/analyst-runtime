from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.agent.tools.shell import ExecTool
from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.channels.web import WebChannel
from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from analyst_runtime.session.manager import SessionManager


@pytest.mark.asyncio
async def test_web_channel_separates_execution_from_conversation_identity() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(
            allow_from=["*"],
            sandbox_id="test-sandbox",
            gateway_url="http://gateway",
        ),
        bus,
    )

    await channel._process_inbound(
        {
            "type": "user_message",
            "session_id": "chat-run-123",
            "run_id": "run-123",
            "conversation_id": "conversation-456",
            "content": "继续分析上一轮问题",
            "metadata": {"runtime": "example-runtime"},
        }
    )

    published = bus.publish_inbound.await_args.args[0]
    assert published.run_id == "run-123"
    assert published.conversation_id == "conversation-456"
    assert published.execution_key == "web:run-123"
    assert published.session_key == "web:conversation-456"
    assert published.chat_id == "chat-run-123"


@pytest.mark.asyncio
async def test_web_poll_identity_is_stable_until_a_new_channel_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poll_params: list[dict] = []
    active_channel: WebChannel

    class FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def get(self, _url, **kwargs):
            poll_params.append(kwargs["params"])
            if len(poll_params) % 2 == 0:
                active_channel._running = False
            return MagicMock(status_code=200, json=lambda: {"type": "timeout"})

    monkeypatch.setattr("analyst_runtime.channels.web.httpx.AsyncClient", FakeAsyncClient)
    for _ in range(2):
        active_channel = WebChannel(
            MagicMock(sandbox_id="test-sandbox", gateway_url="http://gateway"), MagicMock()
        )
        active_channel._running = True
        await active_channel._poll_loop()

    first, repeated, fresh, fresh_repeated = poll_params
    assert first == repeated and fresh == fresh_repeated
    assert first["runtime_instance_id"] != fresh["runtime_instance_id"]
    assert len(first["runtime_instance_id"]) == len(fresh["runtime_instance_id"]) == 32
    assert first["timeout"] == fresh["timeout"] == 30


@pytest.mark.asyncio
async def test_web_channel_routes_output_and_attachments_by_run_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[tuple[str, dict]] = []

    class FakeResponse:
        is_success = True
        status_code = 200
        text = ""

        def json(self):
            return {
                "id": "a" * 32,
                "media_type": "text/csv",
                "name": "result.csv",
                "run_id": "run-123",
                "size": 12,
            }

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("analyst_runtime.channels.web.httpx.AsyncClient", FakeAsyncClient)
    attachment = tmp_path / "result.csv"
    attachment.write_text("metric,value\nloss,12\n", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_PATH", str(tmp_path))
    channel = WebChannel(
        MagicMock(
            allow_from=["*"],
            gateway_jwt_token="test-token",
            gateway_url="http://gateway",
            sandbox_id="test-sandbox",
        ),
        MagicMock(),
    )

    await channel.send(
        OutboundMessage(
            channel="web",
            chat_id="chat-run-123",
            content="分析完成",
            conversation_id="conversation-456",
            media=[str(attachment)],
            run_id="run-123",
            event_id="event-final-123",
        )
    )

    attachment_request = requests[0][1]
    outbound_request = requests[1][1]
    attachment_headers = attachment_request["headers"]
    assert attachment_headers["X-Agent-Runtime-Run-Id"] == "run-123"
    assert attachment_headers["X-Agent-Runtime-Filename"] == "result.csv"
    assert all(" " not in name for name in attachment_headers)
    assert outbound_request["json"]["session_id"] == "chat-run-123"
    assert outbound_request["json"]["run_id"] == "run-123"
    assert outbound_request["json"]["conversation_id"] == "conversation-456"
    assert outbound_request["json"]["event_id"] == "event-final-123"


@pytest.mark.asyncio
async def test_web_channel_deduplicates_a_redelivered_stable_request() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(
            allow_from=["*"],
            sandbox_id="test-sandbox",
            gateway_url="http://gateway",
        ),
        bus,
    )
    payload = {
        "type": "user_message",
        "request_id": "run:run-123",
        "session_id": "chat-run-123",
        "run_id": "run-123",
        "conversation_id": "conversation-456",
        "content": "检查设备",
    }

    await channel._process_inbound(payload)
    await channel._process_inbound(payload)

    bus.publish_inbound.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_channel_publishes_cancel_control_to_the_agent_bus() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(sandbox_id="test-sandbox", gateway_url="http://gateway"),
        bus,
    )

    await channel._process_inbound(
        {
            "type": "cancel_request",
            "session_id": "chat-run-123",
            "content": "",
            "metadata": {"runtime": "example-runtime"},
        }
    )

    published = bus.publish_inbound.await_args.args[0]
    assert published.chat_id == "chat-run-123"
    assert published.metadata["control"] == "cancel"


@pytest.mark.asyncio
async def test_web_channel_publishes_steer_control_to_the_same_run() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(sandbox_id="test-sandbox", gateway_url="http://gateway"),
        bus,
    )

    await channel._process_inbound(
        {
            "type": "steer_request",
            "session_id": "chat-run-123",
            "run_id": "run-123",
            "conversation_id": "conversation-456",
            "content": "只看最近三个月",
            "metadata": {
                "runtime": "example-runtime",
                "steer_id": "steer-789",
            },
        }
    )

    published = bus.publish_inbound.await_args.args[0]
    assert published.execution_key == "web:run-123"
    assert published.session_key == "web:conversation-456"
    assert published.content == "只看最近三个月"
    assert published.metadata["control"] == "steer"
    assert published.metadata["steer_id"] == "steer-789"


@pytest.mark.asyncio
async def test_web_channel_publishes_steer_status_lookup_without_applying_it() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(sandbox_id="test-sandbox", gateway_url="http://gateway"),
        bus,
    )

    await channel._process_inbound(
        {
            "type": "steer_status_request",
            "session_id": "chat-run-123",
            "run_id": "run-123",
            "conversation_id": "conversation-456",
            "content": "",
            "metadata": {"steer_id": "steer-789"},
        }
    )

    published = bus.publish_inbound.await_args.args[0]
    assert published.session_key == "web:conversation-456"
    assert published.metadata["control"] == "steer_status"
    assert published.metadata["steer_id"] == "steer-789"


@pytest.mark.asyncio
async def test_runtime_reports_duplicate_and_status_lookups_as_pending_once(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    agent = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    agent._connect_mcp = AsyncMock()  # type: ignore[method-assign]
    started = asyncio.Event()
    release = asyncio.Event()

    async def process_message(message: InboundMessage):
        started.set()
        await release.wait()
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="done",
            conversation_id=message.conversation_id,
            run_id=message.run_id,
        )

    agent._process_message = process_message  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run())
    steer = InboundMessage(
        channel="web",
        sender_id="chat-run-123",
        chat_id="chat-run-123",
        content="只看最近三个月",
        run_id="run-123",
        conversation_id="conversation-456",
        metadata={"control": "steer", "steer_id": "steer-pending"},
    )
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="web",
                sender_id="chat-run-123",
                chat_id="chat-run-123",
                content="分析全年趋势",
                run_id="run-123",
                conversation_id="conversation-456",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await bus.publish_inbound(steer)
        await bus.publish_inbound(steer)

        duplicate = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        assert duplicate.metadata["control"] == "steer_pending"
        assert agent.steering.queues[steer.execution_key].qsize() == 1

        await bus.publish_inbound(
            InboundMessage(
                channel=steer.channel,
                sender_id=steer.sender_id,
                chat_id=steer.chat_id,
                content="",
                run_id=steer.run_id,
                conversation_id=steer.conversation_id,
                metadata={"control": "steer_status", "steer_id": "steer-pending"},
            )
        )
        status = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        assert status.metadata["control"] == "steer_pending"
    finally:
        release.set()
        agent.stop()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_web_channel_routes_credential_verification_to_runtime() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(sandbox_id="test-sandbox", gateway_url="http://gateway"),
        bus,
    )

    await channel._process_inbound(
        {
            "type": "provider_credential_verification",
            "session_id": "chat-credential-123",
            "content": "",
            "metadata": {
                "_provider_credential": {
                    "api_key": "secret-key",
                    "provider": "zhipu",
                    "source": "byok",
                },
                "runtime": "example-runtime",
            },
        }
    )

    published = bus.publish_inbound.await_args.args[0]
    assert published.metadata["control"] == "verify_provider_credential"
    assert published.metadata["_provider_credential"]["provider"] == "zhipu"


@pytest.mark.asyncio
async def test_web_channel_routes_model_profile_resolution_to_runtime() -> None:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    channel = WebChannel(
        MagicMock(sandbox_id="test-sandbox", gateway_url="http://gateway"),
        bus,
    )

    await channel._process_inbound(
        {
            "type": "model_profile_resolution",
            "session_id": "chat-model-profile-123",
            "content": "",
            "metadata": {
                "model_profile_id": "glm-5.3-flash",
                "runtime": "example-runtime",
            },
        }
    )

    published = bus.publish_inbound.await_args.args[0]
    assert published.metadata["control"] == "resolve_model_profile"
    assert published.metadata["model_profile_id"] == "glm-5.3-flash"


@pytest.mark.asyncio
async def test_cancel_control_stops_the_active_chat_without_stopping_agent(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    agent = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    agent._connect_mcp = AsyncMock()  # type: ignore[method-assign]

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def process_message(message: InboundMessage):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    agent._process_message = process_message  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(
            InboundMessage(
                channel="web",
                sender_id="chat-run-123",
                chat_id="chat-run-123",
                content="分析六月生产情况",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=0.5)

        await bus.publish_inbound(
            InboundMessage(
                channel="web",
                sender_id="chat-run-123",
                chat_id="chat-run-123",
                content="",
                metadata={"control": "cancel"},
            )
        )

        await asyncio.wait_for(cancelled.wait(), timeout=0.5)
        acknowledgement = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        assert acknowledgement.chat_id == "chat-run-123"
        assert acknowledgement.metadata["control"] == "cancelled"
        assert run_task.done() is False
    finally:
        agent.stop()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_targets_one_run_without_stopping_a_sibling_run(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    agent = AgentLoop(bus=bus, provider=provider, workspace=tmp_path)
    agent._connect_mcp = AsyncMock()  # type: ignore[method-assign]
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    release_first = asyncio.Event()

    async def process_message(message: InboundMessage):
        if message.run_id == "run-1":
            first_started.set()
            try:
                await release_first.wait()
            except asyncio.CancelledError:
                first_cancelled.set()
                raise
        return OutboundMessage(
            channel=message.channel,
            chat_id=message.chat_id,
            content="done",
            conversation_id=message.conversation_id,
            run_id=message.run_id,
        )

    agent._process_message = process_message  # type: ignore[method-assign]
    run_task = asyncio.create_task(agent.run())
    try:
        for run_id in ("run-1", "run-2"):
            await bus.publish_inbound(
                InboundMessage(
                    channel="web",
                    sender_id="shared-route",
                    chat_id="shared-route",
                    content=f"request {run_id}",
                    conversation_id="conversation-456",
                    run_id=run_id,
                )
            )
            if run_id == "run-1":
                await asyncio.wait_for(first_started.wait(), timeout=0.5)

        await bus.publish_inbound(
            InboundMessage(
                channel="web",
                sender_id="shared-route",
                chat_id="shared-route",
                content="",
                conversation_id="conversation-456",
                metadata={"control": "cancel"},
                run_id="run-2",
            )
        )

        acknowledgement = await asyncio.wait_for(bus.consume_outbound(), timeout=0.5)
        assert acknowledgement.metadata["control"] == "cancelled"
        assert acknowledgement.run_id == "run-2"
        assert first_cancelled.is_set() is False
    finally:
        release_first.set()
        agent.stop()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_preserves_returned_usage_for_reload_without_inventing_pending_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_call_started = asyncio.Event()
    provider_cancelled = asyncio.Event()

    class PartialProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="synthetic-private-test-credential")
            self.calls = 0

        def get_default_model(self) -> str:
            return "synthetic-test-model"

        async def chat(self, **_kwargs) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    content="",
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCallRequest(
                            id="synthetic-read",
                            name="read_file",
                            arguments={"path": "synthetic.txt"},
                        )
                    ],
                    usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
                )
            second_call_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                provider_cancelled.set()
                raise
            raise AssertionError("Blocked synthetic provider must be cancelled")

    (tmp_path / "synthetic.txt").write_text("SYNTHETIC cancellation evidence\n")
    bus = MessageBus()
    provider = PartialProvider()
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        tool_profile="trusted-analysis",
    )
    agent._connect_mcp = AsyncMock()  # type: ignore[method-assign]
    message = InboundMessage(
        channel="web",
        sender_id="synthetic-user",
        chat_id="chat-synthetic-cancel-run",
        content="SYNTHETIC read the local note, then continue",
        conversation_id="synthetic-cancel-conversation",
        run_id="synthetic-cancel-run",
    )
    completed_snapshots: list[list[dict]] = []
    publish_outbound = bus.publish_outbound

    async def capture_completed_snapshot(outbound: OutboundMessage) -> None:
        tool = outbound.metadata.get("tool")
        if isinstance(tool, dict) and tool.get("status") == "completed":
            completed_snapshots.append(
                SessionManager(tmp_path).get_or_create(message.session_key).events
            )
        await publish_outbound(outbound)

    monkeypatch.setattr(bus, "publish_outbound", capture_completed_snapshot)
    run_task = asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(message)
        await asyncio.wait_for(second_call_started.wait(), timeout=2)
        # A hard exit cannot run the cancellation handler; inspect disk before cancelling.
        pending = SessionManager(tmp_path).get_or_create(message.session_key)
        returned = [event for event in pending.events if event.get("type") == "model_call"]
        assert len(returned) == 1
        assert returned[0]["usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
        }
        assert any(event.get("type") == "tool_result" for event in pending.events)
        assert not any(event.get("type") == "final_response" for event in pending.events)
        assert provider.calls == 2
        assert len(completed_snapshots) == 1
        assert any(event.get("type") == "tool_result" for event in completed_snapshots[0])
        session_file = next((tmp_path / "sessions").glob("*.jsonl"))
        assert provider.api_key not in session_file.read_text()
        await bus.publish_inbound(
            InboundMessage(
                channel=message.channel,
                sender_id=message.sender_id,
                chat_id=message.chat_id,
                content="",
                conversation_id=message.conversation_id,
                metadata={"control": "cancel"},
                run_id=message.run_id,
            )
        )
        for _ in range(8):
            acknowledgement = await asyncio.wait_for(bus.consume_outbound(), timeout=2)
            if acknowledgement.metadata.get("control") == "cancelled":
                break
        else:
            pytest.fail("Runtime did not acknowledge this run's cancellation")
        assert acknowledgement.run_id == message.run_id
        assert provider_cancelled.is_set()
        assert not run_task.done()

        # Recreate the session manager to prove disk persistence, not its live cache.
        restored = SessionManager(tmp_path).get_or_create(message.session_key)
        returned = [event for event in restored.events if event.get("type") == "model_call"]
        assert len(returned) == 1
        assert returned[0]["usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
        }
        assert provider.calls == 2
        assert not any(event.get("type") == "final_response" for event in restored.events)
        assert any(event.get("type") == "user_input" for event in restored.events)
        assert any(event.get("type") == "tool_result" for event in restored.events)
        session_file = next((tmp_path / "sessions").glob("*.jsonl"))
        assert provider.api_key not in session_file.read_text()
    finally:
        agent.stop()
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelling_exec_terminates_its_subprocess(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    script = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    task = asyncio.create_task(ExecTool(timeout=60, working_dir=str(tmp_path)).execute(command))
    child_pid: int | None = None
    try:
        for _ in range(100):
            if pid_path.exists():
                child_pid = int(pid_path.read_text())
                break
            await asyncio.sleep(0.01)
        assert child_pid is not None

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("cancelled exec left its child process running")
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
