"""Base LLM provider interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    message_id: str = ""  # Provider-assigned response ID (e.g. msg_bdrk_01PRY...)
    retry_count: int = 0  # Provider transport retries before this response.
    error_code: str | None = None  # Stable Runtime-owned support code for failed calls.

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    def set_request_credentials(
        self,
        *,
        api_key: str,
        api_base: str | None = None,
        provider: str | None = None,
    ) -> object | None:
        """Install task-local credentials for one trusted runtime request."""
        return None

    def set_request_provider(self, *, provider: str) -> object | None:
        """Select one deployment provider for the current trusted Runtime request."""
        return None

    def reset_request_credentials(self, token: object | None) -> None:
        """Remove credentials installed by ``set_request_credentials``."""

    async def verify_request_credentials(self, *, api_key: str, provider: str) -> bool:
        """Verify a provider credential inside the Runtime provider boundary."""
        return False

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
