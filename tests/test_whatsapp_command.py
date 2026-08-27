"""Tests for the /whatsapp slash command in AgentLoop and WhatsApp self-only routing.

Covers:
- Stale status.json ("connected") with no running channel → must start bridge
- Live status.json ("connected") with running channel → confirm already connected
- status.json ("qr_pending") with no running channel → treat as disconnected, restart
- No status.json → start bridge
- Self-only mode: own message must reach the bus (not be silently dropped by is_allowed)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from analyst_runtime.bus.events import InboundMessage
from analyst_runtime.bus.queue import MessageBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_inbound(content: str) -> InboundMessage:
    return InboundMessage(
        channel="telegram",
        sender_id="5743256476",
        chat_id="5743256476",
        content=content,
    )


def _make_agent(workspace: Path):
    """Create a minimal AgentLoop with a fake provider."""
    from unittest.mock import MagicMock
    from analyst_runtime.agent.loop import AgentLoop
    from analyst_runtime.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "gpt-4o-mini"

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=workspace,
    )
    return agent, bus


def _make_channel_manager_without_whatsapp():
    """ChannelManager stub with no WhatsApp channel."""
    mgr = MagicMock()
    mgr.get_channel.return_value = None
    mgr.channels = {}
    # _start_channel returns a coroutine that does nothing
    async def _noop(*a, **kw):
        pass
    mgr._start_channel = _noop
    return mgr


def _make_channel_manager_with_connected_whatsapp():
    """ChannelManager stub where WhatsApp is already running."""
    from analyst_runtime.channels.whatsapp import WhatsAppChannel
    from analyst_runtime.config.schema import WhatsAppConfig

    wa = MagicMock(spec=WhatsAppChannel)
    wa.is_running = True
    wa._running = True

    mgr = MagicMock()
    mgr.get_channel.return_value = wa
    mgr.channels = {"whatsapp": wa}
    return mgr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_stale_connected_status_starts_bridge(tmp_path: Path) -> None:
    """Stale status.json ('connected') with no running channel must start the bridge.

    Regression: the handler used to trust status.json blindly, so when a new
    container started with stale state it returned '✅ WhatsApp connected' and
    never started the bridge — leaving WhatsApp messages unrouted.
    """
    # Write stale "connected" status (from a previous container session)
    wa_dir = tmp_path / ".analyst-runtime" / "whatsapp"
    wa_dir.mkdir(parents=True)
    (wa_dir / "status.json").write_text(json.dumps({"state": "connected", "ts": "2026-01-01T00:00:00+00:00"}))

    agent, _bus = _make_agent(tmp_path)
    agent.channel_manager = _make_channel_manager_without_whatsapp()

    bridge_started = []

    async def fake_start_bridge() -> bool:
        bridge_started.append(True)
        return True

    with patch("analyst_runtime.utils.helpers.get_data_path", return_value=tmp_path / ".analyst-runtime"):
        # Patch WhatsAppChannel so we control start_bridge
        with patch("analyst_runtime.channels.whatsapp.WhatsAppChannel") as MockWA:
            instance = MagicMock()
            instance.start_bridge = fake_start_bridge
            MockWA.return_value = instance

            result = await agent._process_message(_make_inbound("/whatsapp"))

    assert result is not None
    assert bridge_started, "start_bridge() was never called — stale status.json was trusted blindly"
    assert "🔄" in result.content or "Starting" in result.content, (
        f"Expected bridge-starting message, got: {result.content!r}"
    )


@pytest.mark.asyncio
async def test_whatsapp_stale_qr_pending_status_restarts_bridge(tmp_path: Path) -> None:
    """Stale 'qr_pending' status with no running channel should restart bridge, not freeze on old QR."""
    wa_dir = tmp_path / ".analyst-runtime" / "whatsapp"
    wa_dir.mkdir(parents=True)
    (wa_dir / "status.json").write_text(json.dumps({"state": "qr_pending", "ts": "2026-01-01T00:00:00+00:00"}))

    agent, _bus = _make_agent(tmp_path)
    agent.channel_manager = _make_channel_manager_without_whatsapp()

    bridge_started = []

    async def fake_start_bridge() -> bool:
        bridge_started.append(True)
        return True

    with patch("analyst_runtime.utils.helpers.get_data_path", return_value=tmp_path / ".analyst-runtime"):
        with patch("analyst_runtime.channels.whatsapp.WhatsAppChannel") as MockWA:
            instance = MagicMock()
            instance.start_bridge = fake_start_bridge
            MockWA.return_value = instance

            result = await agent._process_message(_make_inbound("/whatsapp"))

    assert result is not None
    assert bridge_started, "start_bridge() was never called — stale qr_pending status was trusted blindly"


@pytest.mark.asyncio
async def test_whatsapp_live_connected_channel_returns_already_connected(tmp_path: Path) -> None:
    """When the WhatsApp channel is actually running, /whatsapp confirms connected."""
    wa_dir = tmp_path / ".analyst-runtime" / "whatsapp"
    wa_dir.mkdir(parents=True)
    (wa_dir / "status.json").write_text(json.dumps({"state": "connected", "ts": "2026-01-01T00:00:00+00:00"}))

    agent, _bus = _make_agent(tmp_path)
    agent.channel_manager = _make_channel_manager_with_connected_whatsapp()

    with patch("analyst_runtime.utils.helpers.get_data_path", return_value=tmp_path / ".analyst-runtime"):
        result = await agent._process_message(_make_inbound("/whatsapp"))

    assert result is not None
    assert "connected" in result.content.lower() or "✅" in result.content, (
        f"Expected connected confirmation, got: {result.content!r}"
    )


@pytest.mark.asyncio
async def test_whatsapp_no_status_file_starts_bridge(tmp_path: Path) -> None:
    """With no status.json at all, /whatsapp must start the bridge."""
    agent, _bus = _make_agent(tmp_path)
    agent.channel_manager = _make_channel_manager_without_whatsapp()

    bridge_started = []

    async def fake_start_bridge() -> bool:
        bridge_started.append(True)
        return True

    with patch("analyst_runtime.utils.helpers.get_data_path", return_value=tmp_path / ".analyst-runtime"):
        with patch("analyst_runtime.channels.whatsapp.WhatsAppChannel") as MockWA:
            instance = MagicMock()
            instance.start_bridge = fake_start_bridge
            MockWA.return_value = instance

            result = await agent._process_message(_make_inbound("/whatsapp"))

    assert result is not None
    assert bridge_started, "start_bridge() was never called with no status.json"


# ---------------------------------------------------------------------------
# Self-only routing tests — the real end-to-end path through WhatsAppChannel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_only_message_reaches_bus(tmp_path: Path) -> None:
    """A WhatsApp self-message (own phone → own phone) must reach the bus.

    Regression: is_allowed() returns False when allow_from is empty, silently
    dropping the message even after the self-only JID check passes.
    The fix: WhatsApp self-only mode must bypass is_allowed() (or ensure the
    sender is added to the allow list before _handle_message is called).
    """
    from analyst_runtime.bus.queue import MessageBus
    from analyst_runtime.channels.whatsapp import WhatsAppChannel
    from analyst_runtime.config.schema import WhatsAppConfig

    bus = MessageBus()
    cfg = WhatsAppConfig(enabled=True)  # allow_from=[] (self-only mode)
    ch = WhatsAppChannel(config=cfg, bus=bus, owner_chat_id=None)
    ch._running = True
    ch._connected = True
    ch._own_jid = "447394414812:18@s.whatsapp.net"

    # Simulate a bridge message from own phone (self-chat)
    import json
    bridge_msg = json.dumps({
        "type": "message",
        "sender": "447394414812@s.whatsapp.net",
        "pn": "",
        "content": "hello from myself",
        "timestamp": 1234567890,
        "isGroup": False,
    })

    inbound_msgs = []

    async def capture_publish(msg):
        inbound_msgs.append(msg)

    bus.publish_inbound = capture_publish

    await ch._handle_bridge_message(bridge_msg)

    assert inbound_msgs, (
        "Self-message was not published to the bus. "
        "is_allowed() is silently blocking it because allow_from is empty."
    )
    assert inbound_msgs[0].content == "hello from myself"
    assert inbound_msgs[0].channel == "whatsapp"


@pytest.mark.asyncio
async def test_self_only_message_with_lid_reaches_bus(tmp_path: Path) -> None:
    """A self-message where pn is LID format must still reach the bus.

    Regression: WhatsApp now sends remoteJidAlt as a LID (e.g. 40441579843636@lid)
    and remoteJid as the phone number (447394414812@s.whatsapp.net).
    Python picked sender_id from the LID (pn field) but own_phone comes from
    own_jid which is always phone-format — so the comparison always failed.
    """
    from analyst_runtime.bus.queue import MessageBus
    from analyst_runtime.channels.whatsapp import WhatsAppChannel
    from analyst_runtime.config.schema import WhatsAppConfig

    bus = MessageBus()
    cfg = WhatsAppConfig(enabled=True)
    ch = WhatsAppChannel(config=cfg, bus=bus, owner_chat_id=None)
    ch._running = True
    ch._connected = True
    ch._own_jid = "447394414812:19@s.whatsapp.net"

    import json
    bridge_msg = json.dumps({
        "type": "message",
        "sender": "447394414812@s.whatsapp.net",   # phone-format (remoteJid)
        "pn": "40441579843636@lid",                 # LID-format (remoteJidAlt)
        "content": "hello from myself via LID",
        "timestamp": 1234567890,
        "isGroup": False,
    })

    inbound_msgs = []
    async def capture_publish(msg):
        inbound_msgs.append(msg)
    bus.publish_inbound = capture_publish

    await ch._handle_bridge_message(bridge_msg)

    assert inbound_msgs, (
        "Self-message with LID pn was silently dropped. "
        "sender_id from LID (40441579843636) doesn't match own_phone from "
        "phone-format own_jid (447394414812) — must compare phone-to-phone."
    )
    assert inbound_msgs[0].content == "hello from myself via LID"
    assert inbound_msgs[0].channel == "whatsapp"


@pytest.mark.asyncio
async def test_self_only_subsequent_messages_not_blocked(tmp_path: Path) -> None:
    """Every self-message must reach the bus, not just the first one.

    Regression: after the first self-message passed, _ensure_allow_from() added
    the sender LID ('40441579843636') to allow_from. The second message saw a
    non-empty allow_from, skipped the self-only JID check, entered the allow_from
    branch, hit _is_sender_authorized (which requires gateway auth), and was dropped.
    """
    from analyst_runtime.bus.queue import MessageBus
    from analyst_runtime.channels.whatsapp import WhatsAppChannel
    from analyst_runtime.config.schema import WhatsAppConfig

    bus = MessageBus()
    cfg = WhatsAppConfig(enabled=True)  # allow_from=[] — self-only mode
    ch = WhatsAppChannel(config=cfg, bus=bus, owner_chat_id=None)
    ch._running = True
    ch._connected = True
    ch._own_jid = "447394414812:19@s.whatsapp.net"

    import json

    def _make_msg(content: str) -> str:
        return json.dumps({
            "type": "message",
            "sender": "447394414812@s.whatsapp.net",
            "pn": "40441579843636@lid",
            "content": content,
            "timestamp": 1234567890,
            "isGroup": False,
        })

    inbound_msgs = []
    async def capture_publish(msg):
        inbound_msgs.append(msg)
    bus.publish_inbound = capture_publish

    # Simulate production: gateway is configured and rejects unknown senders.
    # Without this, _is_sender_authorized returns True in dev mode and masks the bug.
    async def gateway_denies(*_args, **_kwargs) -> bool:
        return False
    ch._is_sender_authorized = gateway_denies

    await ch._handle_bridge_message(_make_msg("first"))
    assert len(inbound_msgs) == 1, "First message must reach bus"

    await ch._handle_bridge_message(_make_msg("second"))
    assert len(inbound_msgs) == 2, (
        "Second message was blocked. allow_from is probably "
        f"{getattr(cfg, 'allow_from', '?')!r} — _ensure_allow_from() corrupted self-only state."
    )


@pytest.mark.asyncio
async def test_non_self_message_blocked_in_self_only_mode(tmp_path: Path) -> None:
    """A message from a different phone is blocked in self-only mode."""
    from analyst_runtime.bus.queue import MessageBus
    from analyst_runtime.channels.whatsapp import WhatsAppChannel
    from analyst_runtime.config.schema import WhatsAppConfig

    bus = MessageBus()
    cfg = WhatsAppConfig(enabled=True)
    ch = WhatsAppChannel(config=cfg, bus=bus, owner_chat_id=None)
    ch._running = True
    ch._connected = True
    ch._own_jid = "447394414812:18@s.whatsapp.net"

    import json
    bridge_msg = json.dumps({
        "type": "message",
        "sender": "449999999999@s.whatsapp.net",  # different phone
        "pn": "",
        "content": "hello from someone else",
        "timestamp": 1234567890,
        "isGroup": False,
    })

    inbound_msgs = []
    async def capture_publish(msg):
        inbound_msgs.append(msg)
    bus.publish_inbound = capture_publish

    await ch._handle_bridge_message(bridge_msg)

    assert not inbound_msgs, "Message from a different phone should be blocked in self-only mode"
