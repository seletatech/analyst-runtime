"""Async message queue for decoupled channel-agent communication."""

import asyncio
from typing import Awaitable, Callable

from loguru import logger

from analyst_runtime.bus.events import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    _MAX_QUEUE_SIZE = 1000

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=self._MAX_QUEUE_SIZE)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=self._MAX_QUEUE_SIZE)
        self._outbound_subscribers: dict[str, list[Callable[[OutboundMessage], Awaitable[None]]]] = {}
        self._inbound_listeners: list[Callable[[InboundMessage], Awaitable[None]]] = []
        self._running = False

    def add_inbound_listener(self, callback: Callable[[InboundMessage], Awaitable[None]]) -> None:
        """Register a non-consuming listener called whenever a message is published inbound."""
        self._inbound_listeners.append(callback)

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        if self.inbound.full():
            logger.warning("Inbound queue is full (%d messages) — blocking until space available", self._MAX_QUEUE_SIZE)
        await self.inbound.put(msg)
        for listener in self._inbound_listeners:
            try:
                asyncio.create_task(listener(msg))
            except Exception as exc:
                logger.debug("Inbound listener error: %s", exc)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        if self.outbound.full():
            logger.warning("Outbound queue is full (%d messages) — blocking until space available", self._MAX_QUEUE_SIZE)
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    def subscribe_outbound(
        self,
        channel: str,
        callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        """Subscribe to outbound messages for a specific channel."""
        if channel not in self._outbound_subscribers:
            self._outbound_subscribers[channel] = []
        self._outbound_subscribers[channel].append(callback)

    async def dispatch_outbound(self) -> None:
        """
        Dispatch outbound messages to subscribed channels.
        Run this as a background task.
        """
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self.outbound.get(), timeout=1.0)
                subscribers = self._outbound_subscribers.get(msg.channel, [])
                for callback in subscribers:
                    try:
                        await callback(msg)
                    except Exception as e:
                        logger.error(f"Error dispatching to {msg.channel}: {e}")
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the dispatcher loop."""
        self._running = False

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()
