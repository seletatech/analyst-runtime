"""Delivery acknowledgements for outbound messages."""

import asyncio
from typing import Any


class DeliveryAcknowledgement:
    """One-shot receipt resolved by the channel dispatcher."""

    def __init__(self) -> None:
        self._result = asyncio.get_running_loop().create_future()

    async def wait(self) -> dict[str, Any]:
        return await self._result

    def succeed(self, receipt: dict[str, Any] | None = None) -> None:
        if not self._result.done():
            self._result.set_result(receipt or {})

    def fail(self, error: Exception) -> None:
        if not self._result.done():
            self._result.set_exception(error)
