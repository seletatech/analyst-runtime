"""Tests for TelegramChannel inbound message debouncing.

Covers:
- Single message published as-is after the debounce window
- Rapid burst of messages combined into one bus publish
- Media paths from multiple messages merged in order
- [empty message] placeholders dropped when real content exists in the burst
- Debounce timer resets when a new message arrives before it fires
- Calling _flush_pending with nothing buffered is a no-op
"""
from __future__ import annotations

import asyncio

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.channels.telegram import TelegramChannel
from nanobot.config.schema import TelegramConfig


@pytest.fixture()
def bus() -> MessageBus:
    return MessageBus()


@pytest.fixture()
def channel(bus: MessageBus) -> TelegramChannel:
    cfg = TelegramConfig(allow_from=["*"])
    ch = TelegramChannel(config=cfg, bus=bus)
    # Shrink the debounce window so tests run in milliseconds.
    ch.DEBOUNCE_SECS = 0.01
    return ch


# ---------------------------------------------------------------------------
# _flush_pending — unit tests (no timer involved)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flush_pending_single_message_published(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """Single buffered message is published unchanged."""
    channel._pending["chat1"] = [
        {"content": "hello", "media": [], "sender_id": "user1", "metadata": {"message_id": 1}},
    ]
    await channel._flush_pending("chat1")
    msg = bus.inbound.get_nowait()
    assert msg.content == "hello"
    assert msg.chat_id == "chat1"
    assert msg.sender_id == "user1"


@pytest.mark.asyncio
async def test_flush_pending_combines_content_with_newlines(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """Multiple buffered text parts are joined with newlines into one message."""
    channel._pending["chat2"] = [
        {"content": "do X", "media": [], "sender_id": "u", "metadata": {}},
        {"content": "also Y", "media": [], "sender_id": "u", "metadata": {}},
        {"content": "and Z", "media": [], "sender_id": "u", "metadata": {"message_id": 3}},
    ]
    await channel._flush_pending("chat2")
    msg = bus.inbound.get_nowait()
    assert msg.content == "do X\nalso Y\nand Z"


@pytest.mark.asyncio
async def test_flush_pending_combines_media_paths(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """Media paths from all burst messages are merged in arrival order."""
    channel._pending["chat3"] = [
        {"content": "see this", "media": ["/tmp/a.jpg"], "sender_id": "u", "metadata": {}},
        {"content": "and this", "media": ["/tmp/b.jpg"], "sender_id": "u", "metadata": {}},
    ]
    await channel._flush_pending("chat3")
    msg = bus.inbound.get_nowait()
    assert msg.media == ["/tmp/a.jpg", "/tmp/b.jpg"]


@pytest.mark.asyncio
async def test_flush_pending_drops_empty_placeholder_when_real_content_exists(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """[empty message] placeholders are filtered when real content exists in the burst."""
    channel._pending["chat4"] = [
        {"content": "[empty message]", "media": [], "sender_id": "u", "metadata": {}},
        {"content": "real text", "media": [], "sender_id": "u", "metadata": {}},
    ]
    await channel._flush_pending("chat4")
    msg = bus.inbound.get_nowait()
    assert msg.content == "real text"


@pytest.mark.asyncio
async def test_flush_pending_all_empty_message_preserved(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """When every part is [empty message], the result is still [empty message]."""
    channel._pending["chat5"] = [
        {"content": "[empty message]", "media": [], "sender_id": "u", "metadata": {}},
        {"content": "[empty message]", "media": [], "sender_id": "u", "metadata": {}},
    ]
    await channel._flush_pending("chat5")
    msg = bus.inbound.get_nowait()
    assert msg.content == "[empty message]"


@pytest.mark.asyncio
async def test_flush_pending_uses_last_messages_sender_and_metadata(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """sender_id and metadata are taken from the last message in the burst."""
    channel._pending["chat6"] = [
        {"content": "a", "media": [], "sender_id": "first_user", "metadata": {"message_id": 10}},
        {"content": "b", "media": [], "sender_id": "last_user", "metadata": {"message_id": 11}},
    ]
    await channel._flush_pending("chat6")
    msg = bus.inbound.get_nowait()
    assert msg.sender_id == "last_user"
    assert msg.metadata["message_id"] == 11


@pytest.mark.asyncio
async def test_flush_pending_noop_when_nothing_buffered(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """Calling _flush_pending for an unknown chat_id publishes nothing."""
    await channel._flush_pending("nonexistent")
    assert bus.inbound.empty()


# ---------------------------------------------------------------------------
# Debounce timer integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_debounce_fires_and_publishes_after_idle_window(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """After the debounce window expires a single buffered message is published."""
    channel._pending["chat7"] = [
        {"content": "hi", "media": [], "sender_id": "u", "metadata": {}},
    ]
    channel._reset_debounce("chat7")
    await asyncio.sleep(channel.DEBOUNCE_SECS * 5)
    msg = bus.inbound.get_nowait()
    assert msg.content == "hi"


@pytest.mark.asyncio
async def test_debounce_reset_delays_flush_and_combines_burst(
    channel: TelegramChannel, bus: MessageBus
) -> None:
    """Timer reset on second message ensures both parts are flushed together."""
    channel._pending["chat8"] = [
        {"content": "first", "media": [], "sender_id": "u", "metadata": {}},
    ]
    channel._reset_debounce("chat8")

    # Arrive before the timer fires — this resets it.
    channel._pending["chat8"].append(
        {"content": "second", "media": [], "sender_id": "u", "metadata": {}}
    )
    channel._reset_debounce("chat8")

    await asyncio.sleep(channel.DEBOUNCE_SECS * 5)

    msg = bus.inbound.get_nowait()
    assert msg.content == "first\nsecond"
    # Only one combined message was published — not two separate messages.
    assert bus.inbound.empty()
