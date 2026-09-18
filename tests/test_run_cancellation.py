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
