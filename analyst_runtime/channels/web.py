"""Web channel: bridges FastAPI gateway to Analyst Runtime internal bus via HTTP polling.

Message flow:
  FastAPI durable bridge store
      <- GET /internal/sandbox/{id}/inbound  (Analyst Runtime long-polls)
  Analyst Runtime AgentLoop
      -> POST /internal/sandbox/{id}/outbound (Analyst Runtime posts reply)
      -> durable bridge store -> WebSocket -> browser

The channel uses HTTP long-polling so it works across the Docker process
boundary (container <-> host). The ExternalBus asyncio.Queue approach only
works in-process and is kept below for reference / unit tests.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from loguru import logger

from analyst_runtime.bus.events import InboundMessage, OutboundMessage
from analyst_runtime.bus.queue import MessageBus
from analyst_runtime.channels.base import BaseChannel

# ---------------------------------------------------------------------------
# Application gateway message protocol (JSON)
# ---------------------------------------------------------------------------

MSG_TYPE_USER_MESSAGE = "user_message"
MSG_TYPE_CANCEL_REQUEST = "cancel_request"
MSG_TYPE_STEER_REQUEST = "steer_request"
MSG_TYPE_STEER_STATUS_REQUEST = "steer_status_request"
MSG_TYPE_PROVIDER_CREDENTIAL_VERIFICATION = "provider_credential_verification"
MSG_TYPE_MODEL_PROFILE_RESOLUTION = "model_profile_resolution"
MSG_TYPE_AGENT_RESPONSE = "agent_response"
MSG_TYPE_AGENT_STATUS = "agent_status"
MSG_TYPE_ERROR = "error"
MSG_TYPE_ASSET_CREATED = "asset_created"


def make_message(
    msg_type: str,
    content: str,
    *,
    session_id: str = "",
    project_id: str = "",
    sandbox_id: str = "",
    agent_name: str = "",
    run_id: str = "",
    conversation_id: str = "",
    request_id: str = "",
    event_id: str = "",
    metadata: dict | None = None,
) -> dict:
    """Build an application gateway protocol message dict."""
    payload = {
        "type": msg_type,
        "session_id": session_id,
        "project_id": project_id,
        "sandbox_id": sandbox_id,
        "agent_name": agent_name,
        "content": content,
        "metadata": metadata or {},
    }
    if run_id:
        payload["run_id"] = run_id
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if request_id:
        payload["request_id"] = request_id
    if event_id:
        payload["event_id"] = event_id
    return payload


# ---------------------------------------------------------------------------
# WebChannel  (Analyst Runtime BaseChannel implementation)
# ---------------------------------------------------------------------------


class WebChannel(BaseChannel):
    """Bridge between the FastAPI gateway and Analyst Runtime's internal bus.

    Uses HTTP long-polling so it works across the Docker process boundary:
      - start()  launches a poll loop calling GET /internal/sandbox/{id}/inbound
      - send()   posts outbound messages via POST /internal/sandbox/{id}/outbound
    """

    name = "web"

    def __init__(self, config: Any, bus: MessageBus) -> None:
        super().__init__(config, bus)
        self.sandbox_id: str = getattr(config, "sandbox_id", "") or os.environ.get("SANDBOX_ID", "")
        self._gateway_url: str = (
            getattr(config, "gateway_url", None)
            or os.environ.get("GATEWAY_URL", "http://host.docker.internal:8000")
        ).rstrip("/")
        self._gateway_token: str = getattr(config, "gateway_jwt_token", None) or os.environ.get(
            "GATEWAY_JWT_TOKEN", ""
        )
        self._listener_task: asyncio.Task | None = None
        self._accepted_request_ids: OrderedDict[str, None] = OrderedDict()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        if not self.sandbox_id:
            logger.error("Web channel: SANDBOX_ID not set, cannot start")
            return

        self._running = True
        logger.info(f"Web channel started (sandbox={self.sandbox_id}, gateway={self._gateway_url})")
        self._listener_task = asyncio.create_task(self._poll_loop())
        try:
            await self._listener_task
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            self._listener_task = None
        logger.info("Web channel stopped")

    # -- outbound (agent -> gateway) -----------------------------------------

    async def send(self, msg: OutboundMessage) -> dict[str, Any]:
        """Post an agent response to the gateway outbound queue via HTTP."""
        if not self.sandbox_id:
            logger.warning("Web channel: sandbox_id not set, dropping outbound message")
            raise RuntimeError("ANALYST-RUNTIME-DELIVERY-001: Web channel is not configured")

        run_id = msg.run_id or msg.chat_id.removeprefix("chat-")
        attachments = await self._upload_attachments(msg.media or [], run_id=run_id)
        payload = make_message(
            msg_type=MSG_TYPE_AGENT_RESPONSE,
            content=msg.content,
            sandbox_id=self.sandbox_id,
            session_id=msg.chat_id,
            agent_name=msg.metadata.get("agent_name", "director"),
            run_id=run_id,
            conversation_id=msg.conversation_id or "",
            event_id=msg.event_id,
            metadata={**msg.metadata, "attachments": attachments},
        )

        url = f"{self._gateway_url}/internal/sandbox/{self.sandbox_id}/outbound"
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._gateway_token}"},
                    timeout=10.0,
                )
                resp.raise_for_status()
            logger.debug(f"Web channel -> outbound: {msg.content[:80]}")
            return {
                "attachments": [
                    {"name": item.get("name", ""), "status": "delivered"} for item in attachments
                ]
            }
        except Exception as e:
            logger.error(f"Web channel failed to post outbound message: {e}")
            raise

    async def _upload_attachments(
        self, media_paths: list[str], *, run_id: str
    ) -> list[dict[str, Any]]:
        uploaded: list[dict[str, Any]] = []
        if not media_paths:
            return uploaded
        url = f"{self._gateway_url}/internal/sandbox/{self.sandbox_id}/attachments"
        async with httpx.AsyncClient(trust_env=False) as client:
            for raw_path in media_paths:
                workspace = Path(os.environ.get("WORKSPACE_PATH", "/workspace")).resolve()
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = workspace / path
                path = path.resolve()
                if not path.is_relative_to(workspace):
                    raise PermissionError(
                        "Attachments must be inside the Analyst Runtime workspace"
                    )
                if not path.is_file():
                    raise FileNotFoundError(f"Attachment does not exist: {path.name}")
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                response = await client.post(
                    url,
                    content=path.read_bytes(),
                    headers={
                        "Authorization": f"Bearer {self._gateway_token}",
                        "Content-Type": media_type,
                        "X-Agent-Runtime-Run-Id": run_id,
                        "X-Agent-Runtime-Filename": quote(path.name),
                    },
                    timeout=30.0,
                )
                if not response.is_success:
                    detail = ""
                    try:
                        detail = str(response.json().get("detail") or "")
                    except Exception:
                        detail = response.text[:200]
                    raise RuntimeError(
                        "ANALYST-RUNTIME-ATTACHMENT-001: "
                        f"{path.name} upload failed ({response.status_code})"
                        + (f": {detail}" if detail else "")
                    )
                uploaded.append(response.json())
        return uploaded

    # -- inbound (gateway -> agent) ------------------------------------------

    async def _poll_loop(self) -> None:
        """Long-poll the gateway for inbound messages."""
        url = f"{self._gateway_url}/internal/sandbox/{self.sandbox_id}/inbound"
        headers = {"Authorization": f"Bearer {self._gateway_token}"}

        while self._running:
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    resp = await client.get(
                        url, headers=headers, timeout=35.0, params={"timeout": 30}
                    )

                if resp.status_code == 200:
                    payload = resp.json()
                    if payload.get("type") == "timeout":
                        continue  # nothing queued, loop again
                    try:
                        await self._process_inbound(payload)
                    except Exception as e:
                        logger.error(f"Web channel inbound processing error: {e}")
                        await self._post_error(str(e), payload.get("session_id", ""))
                elif resp.status_code in (401, 403):
                    logger.error("Web channel: gateway rejected auth (check GATEWAY_JWT_TOKEN)")
                    await asyncio.sleep(10.0)
                else:
                    logger.warning(f"Web channel: poll returned {resp.status_code}")
                    await asyncio.sleep(2.0)

            except httpx.ConnectError:
                logger.warning(
                    f"Web channel: cannot reach gateway at {self._gateway_url}, retrying..."
                )
                await asyncio.sleep(3.0)
            except Exception as e:
                logger.error(f"Web channel poll error: {e}")
                await asyncio.sleep(2.0)

    async def _process_inbound(self, payload: dict) -> None:
        """Convert a gateway payload to an InboundMessage and publish to Analyst Runtime bus."""
        msg_type = payload.get("type", "")
        content = payload.get("content", "")
        media: list[str] = list(payload.get("media", []))
        session_id = str(payload.get("session_id") or payload.get("correlation_id") or "unknown")
        run_id = str(payload.get("run_id") or session_id.removeprefix("chat-"))
        conversation_id = str(payload.get("conversation_id") or session_id)
        project_id = payload.get("project_id", "")
        metadata = payload.get("metadata", {})
        request_id = str(payload.get("request_id") or "").strip()
        channel_override = payload.get("channel")  # e.g. "telegram" when routed from webhook

        if request_id and request_id in self._accepted_request_ids:
            logger.debug(f"Web channel: ignoring duplicate request {request_id}")
            return

        if msg_type == MSG_TYPE_CANCEL_REQUEST:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=session_id,
                    chat_id=session_id,
                    content="",
                    run_id=run_id,
                    conversation_id=conversation_id,
                    metadata={**metadata, "control": "cancel"},
                )
            )
            self._remember_request(request_id)
            return

        if msg_type == MSG_TYPE_STEER_REQUEST:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=session_id,
                    chat_id=session_id,
                    content=content,
                    run_id=run_id,
                    conversation_id=conversation_id,
                    metadata={**metadata, "control": "steer"},
                )
            )
            return

        if msg_type == MSG_TYPE_STEER_STATUS_REQUEST:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=session_id,
                    chat_id=session_id,
                    content="",
                    run_id=run_id,
                    conversation_id=conversation_id,
                    metadata={**metadata, "control": "steer_status"},
                )
            )
            return

        if msg_type == MSG_TYPE_PROVIDER_CREDENTIAL_VERIFICATION:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=session_id,
                    chat_id=session_id,
                    content="",
                    run_id=run_id,
                    conversation_id=conversation_id,
                    metadata={
                        "project_id": project_id,
                        "sandbox_id": self.sandbox_id,
                        **metadata,
                        "control": "verify_provider_credential",
                    },
                )
            )
            return

        if msg_type == MSG_TYPE_MODEL_PROFILE_RESOLUTION:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=session_id,
                    chat_id=session_id,
                    content="",
                    run_id=run_id,
                    conversation_id=conversation_id,
                    metadata={
                        "project_id": project_id,
                        "sandbox_id": self.sandbox_id,
                        **metadata,
                        "control": "resolve_model_profile",
                    },
                )
            )
            return

        if msg_type != MSG_TYPE_USER_MESSAGE:
            logger.debug(f"Web channel: ignoring non-user message type '{msg_type}'")
            return

        if not content and not media:
            logger.debug("Web channel: ignoring empty message with no media")
            return

        logger.info(
            f"Web channel <- inbound [{session_id}]: {content[:80]}"
            + (f" (+{len(media)} media)" if media else "")
        )

        if channel_override and channel_override != self.name:
            # Gateway routed a non-web message (e.g. Telegram) through the inbound pipe.
            # Publish with the correct source channel so the reply goes to TelegramChannel.
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=channel_override,
                    sender_id=session_id,
                    chat_id=session_id,  # session_id == Telegram chat_id (set by webhook handler)
                    content=content,
                    media=media,
                    metadata={"project_id": project_id, "sandbox_id": self.sandbox_id, **metadata},
                    run_id=run_id,
                    conversation_id=conversation_id,
                )
            )
        else:
            await self._handle_message(
                sender_id=session_id,
                chat_id=session_id,
                content=content,
                media=media,
                run_id=run_id,
                conversation_id=conversation_id,
                metadata={
                    "project_id": project_id,
                    "sandbox_id": self.sandbox_id,
                    **metadata,
                },
            )
        self._remember_request(request_id)

    def _remember_request(self, request_id: str) -> None:
        if not request_id:
            return
        self._accepted_request_ids[request_id] = None
        self._accepted_request_ids.move_to_end(request_id)
        while len(self._accepted_request_ids) > 10_000:
            self._accepted_request_ids.popitem(last=False)

    async def _post_error(self, error: str, session_id: str) -> None:
        """Post an error back to the gateway so the UI sees it."""
        payload = make_message(
            msg_type=MSG_TYPE_ERROR,
            content=f"Error processing message: {error}",
            sandbox_id=self.sandbox_id,
            session_id=session_id,
            event_id=f"error:{session_id}:{uuid4().hex}",
        )
        url = f"{self._gateway_url}/internal/sandbox/{self.sandbox_id}/outbound"
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                await client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._gateway_token}"},
                    timeout=5.0,
                )
        except Exception:
            pass
