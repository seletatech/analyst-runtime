from __future__ import annotations

import json
from pathlib import Path
from types import MethodType

import httpx
import pytest

from analyst_runtime.agent.loop import AgentLoop
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content)


class FakeComposioProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1

        if self.calls == 1:
            tool_names = {tool["function"]["name"] for tool in tools or []}
            assert {
                "composio_search_tools",
                "composio_manage_connections",
                "composio_execute_tools",
            }.issubset(tool_names)

            return LLMResponse(
                content="Checking Gmail access.",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1",
                        name="composio_search_tools",
                        arguments={"intent": "send Alice an email"},
                    ),
                    ToolCallRequest(
                        id="tc2",
                        name="composio_manage_connections",
                        arguments={"toolkit": "gmail"},
                    ),
                ],
            )

        if self.calls == 2:
            connect_link = None
            for message in messages:
                if message.get("role") != "tool":
                    continue
                if message.get("name") != "composio_manage_connections":
                    continue
                payload = json.loads(message["content"])
                assert payload["status"] == "auth_required"
                connect_link = payload["redirect_url"]
                break

            assert connect_link
            return LLMResponse(
                content=f"Please connect Gmail here: {connect_link} and then come back and say continue.",
            )

        if self.calls == 3:
            # If the last user message is the loop's mid-task continuation prompt
            # (second-chance after auth_required), the model is still blocked —
            # auth has not been granted yet.  Repeat the connect-Gmail instruction.
            last_user = next(
                (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            if "still need to take actions" in last_user:
                # Auth still required — auth has not been granted between the continuation
                # prompt and now. Repeat the connect-Gmail instruction with the link.
                connect_link = None
                for message in messages:
                    if message.get("role") == "tool" and message.get("name") == "composio_manage_connections":
                        payload = json.loads(message["content"])
                        connect_link = payload.get("redirect_url")
                        break
                return LLMResponse(
                    content=f"You still need to connect Gmail here: {connect_link} before I can send.",
                )
            return LLMResponse(
                content="Running the send now.",
                tool_calls=[
                    ToolCallRequest(
                        id="tc3",
                        name="composio_execute_tools",
                        arguments={
                            "tool_slug": "GMAIL_SEND_EMAIL",
                            "arguments": {"to": "alice@example.com"},
                        },
                    )
                ],
            )

        if self.calls == 4:
            # Second user request ("continue") — now execute the email.
            return LLMResponse(
                content="Running the send now.",
                tool_calls=[
                    ToolCallRequest(
                        id="tc4",
                        name="composio_execute_tools",
                        arguments={
                            "tool_slug": "GMAIL_SEND_EMAIL",
                            "arguments": {"to": "alice@example.com"},
                        },
                    )
                ],
            )

        return LLMResponse(content="Email sent.")

    def get_default_model(self) -> str:
        return "fake-model"


class FakeComposioRetryProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1

        if self.calls == 1:
            tool_names = {tool["function"]["name"] for tool in tools or []}
            assert "composio_execute_tools" in tool_names
            return LLMResponse(
                content="Checking the inbox.",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1",
                        name="composio_execute_tools",
                        arguments={
                            "tool_slug": "GMAIL_LIST_THREADS",
                            "arguments": {},
                        },
                    )
                ],
            )

        return LLMResponse(content="Inbox checked.")

    def get_default_model(self) -> str:
        return "fake-model"


class FakeComposioMissingCredentialsProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1

        if self.calls == 1:
            return LLMResponse(
                content="Checking Composio tools.",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1",
                        name="composio_search_tools",
                        arguments={"intent": "send Alice an email"},
                    )
                ],
            )

        tool_messages = [m for m in messages if m.get("role") == "tool"]
        assert tool_messages
        payload = json.loads(tool_messages[-1]["content"])
        assert "Local Composio credentials are required at" in payload["error"]
        return LLMResponse(content="Please send your Composio API key so I can finish setting up Composio here.")

    def get_default_model(self) -> str:
        return "fake-model"


class FakeComposioBootstrapResumeProvider(LLMProvider):
    def __init__(self, expected_key: str, expected_request: str, expected_path: Path) -> None:
        super().__init__()
        self.calls = 0
        self.expected_key = expected_key
        self.expected_request = expected_request
        self.expected_path = expected_path
        self.seen_system_prompts: list[str] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.calls += 1
        self.seen_system_prompts.append(_message_text(messages[0].get("content")))

        if self.calls == 1:
            system_prompt = self.seen_system_prompts[-1]
            assert "The user's current message is their Composio API key." in system_prompt
            assert self.expected_request in system_prompt
            assert str(self.expected_path) in system_prompt
            assert messages[-1]["role"] == "user"
            assert _message_text(messages[-1]["content"]) == self.expected_key
            return LLMResponse(
                content="Saving the key and resuming the request.",
                tool_calls=[
                    ToolCallRequest(
                        id="tc1",
                        name="write_file",
                        arguments={
                            "path": str(self.expected_path),
                            "content": json.dumps({"api_key": self.expected_key}),
                        },
                    ),
                    ToolCallRequest(
                        id="tc2",
                        name="composio_search_tools",
                        arguments={"intent": self.expected_request},
                    ),
                ],
            )

        tool_results = {m.get("name"): _message_text(m.get("content")) for m in messages if m.get("role") == "tool"}
        assert "write_file" in tool_results
        assert "composio_search_tools" in tool_results
        payload = json.loads(tool_results["composio_search_tools"])
        assert payload["tools"][0]["tool_slug"] == "GMAIL_SEND_EMAIL"
        return LLMResponse(content="Composio is ready now. I found the Gmail tools and can continue.")

    def get_default_model(self) -> str:
        return "fake-model"


class FakePromptCaptureProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.system_prompts: list[str] = []

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        self.system_prompts.append(_message_text(messages[0].get("content")))
        return LLMResponse(content="Still waiting for the Composio API key.")

    def get_default_model(self) -> str:
        return "fake-model"


@pytest.mark.asyncio
async def test_agent_loop_wires_composio_tools_and_reuses_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OWNER_ID", "owner-123")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-composio-key")
    monkeypatch.setenv("COMPOSIO_CALLBACK_BASE_URL", "https://api.example.com")

    provider = FakeComposioProvider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    search_tool = agent.tools.get("composio_search_tools")
    manage_tool = agent.tools.get("composio_manage_connections")
    execute_tool = agent.tools.get("composio_execute_tools")

    assert search_tool is not None
    assert manage_tool is not None
    assert execute_tool is not None
    assert agent.tools.has("composio_search_tools")
    assert agent.tools.has("composio_manage_connections")
    assert agent.tools.has("composio_execute_tools")

    calls: list[tuple[str, str, str, str, dict]] = []

    async def fake_post_json(self, path: str, payload: dict) -> dict:
        calls.append((self.name, self._channel, self._chat_id, path, payload))
        assert self._channel == "web"
        assert self._chat_id == "chat-1"

        if path == "/api/v3/tool_router/session":
            return {"session_id": "trs_123"}
        if path.endswith("/search"):
            return {
                "tools": [
                    {
                        "tool_slug": "GMAIL_SEND_EMAIL",
                        "toolkit": "gmail",
                        "description": "Send an email",
                    }
                ],
                "toolkit_connection_statuses": [
                    {
                        "toolkit": "gmail",
                        "has_active_connection": False,
                    }
                ],
            }
        if path.endswith("/link"):
            return {
                "status": "auth_required",
                "redirect_url": "https://connect.example/link",
            }
        if path.endswith("/execute"):
            return {
                "status": "success",
                "data": {"message": "Email sent."},
            }

        raise AssertionError(f"Unexpected Composio path: {path}")

    for tool in (search_tool, manage_tool, execute_tool):
        monkeypatch.setattr(tool, "_post_json", MethodType(fake_post_json, tool))

    def router_paths() -> list[str]:
        return [path for _, _, _, path, _ in calls]

    first_response = await agent.process_direct(
        "Send Alice an email.",
        session_key="web:chat-1",
        channel="web",
        chat_id="chat-1",
    )

    assert "connect gmail here" in first_response.lower()
    assert "https://connect.example/link" in first_response

    assert router_paths() == [
        "/api/v3/tool_router/session",
        "/api/v3/tool_router/session/trs_123/search",
        "/api/v3/tool_router/session/trs_123/link",
    ]

    agent.sessions.invalidate("web:chat-1")
    session = agent.sessions.get_or_create("web:chat-1")
    assert session.metadata["composio"]["session_id"] == "trs_123"
    assert (workspace / "sessions" / "web_chat-1.jsonl").exists()
    assert not (workspace / "sessions" / "cli_direct.jsonl").exists()

    second_response = await agent.process_direct(
        "continue",
        session_key="web:chat-1",
        channel="web",
        chat_id="chat-1",
    )

    assert second_response == "Email sent."
    assert router_paths() == [
        "/api/v3/tool_router/session",
        "/api/v3/tool_router/session/trs_123/search",
        "/api/v3/tool_router/session/trs_123/link",
        "/api/v3/tool_router/session/trs_123/execute",
    ]
    assert router_paths().count("/api/v3/tool_router/session") == 1
    agent.sessions.invalidate("web:chat-1")
    session = agent.sessions.get_or_create("web:chat-1")
    assert session.metadata["composio"]["session_id"] == "trs_123"
    assert all(channel == "web" and chat_id == "chat-1" for _, channel, chat_id, _, _ in calls)


@pytest.mark.asyncio
async def test_agent_loop_keeps_composio_state_on_logical_session_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OWNER_ID", "owner-123")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-composio-key")
    monkeypatch.setenv("COMPOSIO_CALLBACK_BASE_URL", "https://api.example.com")

    provider = FakeComposioProvider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    search_tool = agent.tools.get("composio_search_tools")
    manage_tool = agent.tools.get("composio_manage_connections")
    execute_tool = agent.tools.get("composio_execute_tools")

    assert search_tool is not None
    assert manage_tool is not None
    assert execute_tool is not None

    calls: list[tuple[str, str, str, str]] = []

    async def fake_post_json(self, path: str, payload: dict) -> dict:
        calls.append((self.name, self._channel, self._chat_id, path))

        if path == "/api/v3/tool_router/session":
            return {"session_id": "trs_123"}
        if path.endswith("/search"):
            return {
                "tools": [
                    {
                        "tool_slug": "GMAIL_SEND_EMAIL",
                        "toolkit": "gmail",
                        "description": "Send an email",
                    }
                ],
                "toolkit_connection_statuses": [
                    {
                        "toolkit": "gmail",
                        "has_active_connection": False,
                    }
                ],
            }
        if path.endswith("/link"):
            return {
                "status": "auth_required",
                "redirect_url": "https://connect.example/link",
            }
        if path.endswith("/execute"):
            return {
                "status": "success",
                "data": {"message": "Email sent."},
            }

        raise AssertionError(f"Unexpected Composio path: {path}")

    for tool in (search_tool, manage_tool, execute_tool):
        monkeypatch.setattr(tool, "_post_json", MethodType(fake_post_json, tool))

    logical_session_key = "cli:logical-1"

    first_response = await agent.process_direct(
        "Send Alice an email.",
        session_key=logical_session_key,
        channel="web",
        chat_id="chat-1",
    )
    assert "connect gmail here" in first_response.lower()

    second_response = await agent.process_direct(
        "continue",
        session_key=logical_session_key,
        channel="slack",
        chat_id="thread-9",
    )
    assert second_response == "Email sent."

    agent.sessions.invalidate(logical_session_key)
    logical_session = agent.sessions.get_or_create(logical_session_key)
    assert logical_session.metadata["composio"]["session_id"] == "trs_123"

    assert agent.sessions.get_or_create("web:chat-1").metadata.get("composio") is None
    assert agent.sessions.get_or_create("slack:thread-9").metadata.get("composio") is None
    assert [path for _, _, _, path in calls].count("/api/v3/tool_router/session") == 1


@pytest.mark.asyncio
async def test_agent_loop_persists_refreshed_composio_session_after_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OWNER_ID", "owner-123")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-composio-key")
    monkeypatch.setenv("COMPOSIO_CALLBACK_BASE_URL", "https://api.example.com")

    provider = FakeComposioRetryProvider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    execute_tool = agent.tools.get("composio_execute_tools")
    assert execute_tool is not None

    session_key = "web:chat-1"
    session = agent.sessions.get_or_create(session_key)
    session.metadata["composio"] = {"session_id": "trs_old"}
    agent.sessions.save(session)

    calls: list[str] = []

    async def fake_post_json(self, path: str, payload: dict) -> dict:
        calls.append(path)

        if path == "/api/v3/tool_router/session":
            return {"session_id": "trs_new"}
        if path == "/api/v3/tool_router/session/trs_old/execute":
            request = httpx.Request("POST", f"https://backend.composio.dev{path}")
            response = httpx.Response(
                404,
                request=request,
                json={
                    "error": {
                        "status": "invalid_session",
                        "slug": "invalid_session",
                        "message": "Session expired",
                    }
                },
            )
            raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)
        if path == "/api/v3/tool_router/session/trs_new/execute":
            return {"ok": True}

        raise AssertionError(f"Unexpected Composio path: {path}")

    monkeypatch.setattr(execute_tool, "_post_json", MethodType(fake_post_json, execute_tool))

    response = await agent.process_direct(
        "Check my inbox.",
        session_key=session_key,
        channel="web",
        chat_id="chat-1",
    )

    assert response == "Inbox checked."

    agent.sessions.invalidate(session_key)
    refreshed_session = agent.sessions.get_or_create(session_key)
    assert refreshed_session.metadata["composio"]["session_id"] == "trs_new"
    assert calls == [
        "/api/v3/tool_router/session/trs_old/execute",
        "/api/v3/tool_router/session",
        "/api/v3/tool_router/session/trs_new/execute",
    ]


@pytest.mark.asyncio
async def test_agent_loop_does_not_resave_stale_composio_session_when_refresh_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OWNER_ID", "owner-123")
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-composio-key")
    monkeypatch.setenv("COMPOSIO_CALLBACK_BASE_URL", "https://api.example.com")

    provider = FakeComposioRetryProvider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    execute_tool = agent.tools.get("composio_execute_tools")
    assert execute_tool is not None

    session_key = "web:chat-1"
    session = agent.sessions.get_or_create(session_key)
    session.metadata["composio"] = {"session_id": "trs_old"}
    agent.sessions.save(session)

    calls: list[str] = []

    async def fake_post_json(self, path: str, payload: dict) -> dict:
        calls.append(path)

        if path == "/api/v3/tool_router/session/trs_old/execute":
            request = httpx.Request("POST", f"https://backend.composio.dev{path}")
            response = httpx.Response(
                404,
                request=request,
                json={
                    "error": {
                        "status": "invalid_session",
                        "slug": "invalid_session",
                        "message": "Session expired",
                    }
                },
            )
            raise httpx.HTTPStatusError("404 Not Found", request=request, response=response)

        if path == "/api/v3/tool_router/session":
            return {}

        raise AssertionError(f"Unexpected Composio path: {path}")

    monkeypatch.setattr(execute_tool, "_post_json", MethodType(fake_post_json, execute_tool))

    response = await agent.process_direct(
        "Check my inbox.",
        session_key=session_key,
        channel="web",
        chat_id="chat-1",
    )

    assert response == "Inbox checked."
    assert calls == [
        "/api/v3/tool_router/session/trs_old/execute",
        "/api/v3/tool_router/session",
    ]

    agent.sessions.invalidate(session_key)
    refreshed_session = agent.sessions.get_or_create(session_key)
    assert refreshed_session.metadata["composio"] == {}


@pytest.mark.asyncio
async def test_agent_loop_marks_session_awaiting_composio_api_key_after_missing_local_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OWNER_ID", "owner-123")
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_PATH", raising=False)

    provider = FakeComposioMissingCredentialsProvider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    response = await agent.process_direct(
        "Send Alice an email.",
        session_key="web:chat-1",
        channel="web",
        chat_id="chat-1",
    )

    assert "composio api key" in response.lower()
    agent.sessions.invalidate("web:chat-1")
    session = agent.sessions.get_or_create("web:chat-1")
    assert session.metadata["awaiting_composio_api_key"] is True
    assert session.metadata["pending_composio_user_request"] == "Send Alice an email."


@pytest.mark.asyncio
async def test_agent_loop_uses_chat_sent_composio_key_to_write_local_credentials_and_resume_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    credentials_path = workspace / ".analyst-runtime" / "composio" / "credentials.json"

    monkeypatch.setenv("OWNER_ID", "owner-123")
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("WORKSPACE_PATH", raising=False)

    expected_key = "cmp_test_key_1234567890"
    expected_request = "Send Alice an email."
    provider = FakeComposioBootstrapResumeProvider(expected_key, expected_request, credentials_path)
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    session = agent.sessions.get_or_create("web:chat-1")
    session.metadata["awaiting_composio_api_key"] = True
    session.metadata["pending_composio_user_request"] = expected_request
    agent.sessions.save(session)

    search_tool = agent.tools.get("composio_search_tools")
    assert search_tool is not None

    async def fake_post_json(self, path: str, payload: dict) -> dict:
        credentials = self._load_local_credentials()
        assert credentials["api_key"] == expected_key
        if path == "/api/v3/tool_router/session":
            return {"session_id": "trs_123"}
        if path.endswith("/search"):
            assert payload == {"queries": [{"use_case": expected_request}]}
            return {
                "tools": [
                    {
                        "tool_slug": "GMAIL_SEND_EMAIL",
                        "toolkit": "gmail",
                        "description": "Send an email",
                    }
                ],
                "toolkit_connection_statuses": [{"toolkit": "gmail", "has_active_connection": False}],
            }
        raise AssertionError(f"Unexpected Composio path: {path}")

    monkeypatch.setattr(search_tool, "_post_json", MethodType(fake_post_json, search_tool))

    response = await agent.process_direct(
        expected_key,
        session_key="web:chat-1",
        channel="web",
        chat_id="chat-1",
    )

    assert "gmail tools" in response.lower()
    assert json.loads(credentials_path.read_text(encoding="utf-8")) == {"api_key": expected_key}
    agent.sessions.invalidate("web:chat-1")
    refreshed_session = agent.sessions.get_or_create("web:chat-1")
    assert refreshed_session.metadata.get("awaiting_composio_api_key") in (None, False)
    assert refreshed_session.metadata.get("pending_composio_user_request") in (None, "")


@pytest.mark.asyncio
async def test_agent_loop_does_not_enter_composio_bootstrap_mode_for_normal_follow_up_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("OWNER_ID", "owner-123")
    provider = FakePromptCaptureProvider()
    agent = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)

    session = agent.sessions.get_or_create("web:chat-1")
    session.metadata["awaiting_composio_api_key"] = True
    session.metadata["pending_composio_user_request"] = "Send Alice an email."
    agent.sessions.save(session)

    await agent.process_direct(
        "continue please",
        session_key="web:chat-1",
        channel="web",
        chat_id="chat-1",
    )

    assert provider.system_prompts
    assert "The user's current message is their Composio API key." not in provider.system_prompts[-1]
