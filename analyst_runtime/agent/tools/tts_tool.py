"""Text-to-speech tool — generates speech via the gateway (Amazon Polly)."""

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Awaitable, Callable

from analyst_runtime.agent.tools.base import Tool
from analyst_runtime.bus.events import OutboundMessage


class TextToSpeechTool(Tool):
    """Convert text to a spoken audio file via the gateway (Amazon Polly neural TTS).

    After generating the audio, use the message tool with media=[path] to send
    it to the user as a voice message.
    """

    def __init__(
        self,
        workspace: Path,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._workspace = workspace
        self._send_callback = send_callback
        self._context: ContextVar[tuple[str, str]] = ContextVar(
            "tts_tool_context",
            default=(default_channel, default_chat_id),
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current chat routing context for auto-delivery."""
        self._context.set((channel, chat_id))

    @property
    def name(self) -> str:
        return "text_to_speech"

    @property
    def description(self) -> str:
        return (
            "Convert text to a spoken audio file. "
            "When a current chat is available, it immediately sends the generated audio back "
            "to that chat and also returns the saved file path. "
            "Use this when responding to voice messages or when a spoken reply is appropriate."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The text to convert to speech",
                },
                "voice": {
                    "type": "string",
                    "description": (
                        "Amazon Polly neural voice. English: Joanna (F), Matthew (M), Salli, Kendra, "
                        "Kimberly, Ivy, Justin, Joey. Mandarin Chinese: Zhiyu. "
                        "British English: Amy (F), Brian (M), Emma. Default: Joanna"
                    ),
                    "default": "Joanna",
                },
                "send_immediately": {
                    "type": "boolean",
                    "description": "Whether to immediately send the generated audio to the current chat. Default: true",
                    "default": True,
                },
            },
            "required": ["text"],
        }

    async def execute(
        self,
        text: str,
        voice: str = "Joanna",
        send_immediately: bool = True,
        **kwargs: Any,
    ) -> str:
        from analyst_runtime.agent.tools.gateway_auth import gateway_client

        if not text.strip():
            return "No text provided for TTS"

        try:
            async with gateway_client(timeout=60.0) as client:
                resp = await client.post(
                    "/tools/tts",
                    json={"text": text, "voice": voice},
                )
                if resp.status_code == 200:
                    file_path = resp.json().get("file_path", "")
                    if not file_path:
                        return "TTS generation returned no file path"
                    default_channel, default_chat_id = self._context.get()
                    if (
                        send_immediately
                        and self._send_callback
                        and default_channel
                        and default_chat_id
                    ):
                        await self._send_callback(
                            OutboundMessage(
                                channel=default_channel,
                                chat_id=default_chat_id,
                                content="",
                                media=[file_path],
                            )
                        )
                        return (
                            f"Voice message sent to "
                            f"{default_channel}:{default_chat_id} ({file_path})"
                        )
                    return file_path
                return f"TTS failed: HTTP {resp.status_code} — {resp.text[:200]}"
        except Exception as exc:
            return f"TTS error: {exc}"
