"""Audio transcription tool — transcribes voice/audio via the gateway (Amazon Transcribe)."""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class TranscribeAudioTool(Tool):
    """Transcribe an audio or voice file to text via the gateway (Amazon Transcribe)."""

    @property
    def name(self) -> str:
        return "transcribe_audio"

    @property
    def description(self) -> str:
        return (
            "Transcribe a voice message or audio file to text. "
            "Call this when the user sends a voice message or audio file — "
            "you will receive a [voice message: path] or [audio file: path] hint in the message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the audio file to transcribe",
                },
                "language_code": {
                    "type": "string",
                    "description": "BCP-47 language code, e.g. en-US, zh-CN. Default: en-US",
                    "default": "en-US",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str, language_code: str = "en-US", **kwargs: Any) -> str:
        from nanobot.agent.tools.gateway_auth import gateway_client

        p = Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"

        try:
            async with gateway_client(timeout=90.0) as client:
                resp = await client.post(
                    "/tools/transcribe",
                    json={
                        "file_path": file_path,
                        "language_code": language_code,
                    },
                )
                if resp.status_code == 200:
                    transcript = resp.json().get("transcript", "")
                    return transcript if transcript else "Transcription returned empty result"
                return f"Transcription failed: HTTP {resp.status_code} — {resp.text[:200]}"
        except Exception as exc:
            return f"Transcription error: {exc}"
