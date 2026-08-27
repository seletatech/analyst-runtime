"""Text-to-speech provider using Groq PlayAI TTS."""

import os
import time
from pathlib import Path

import httpx
from loguru import logger


class GroqTTSProvider:
    """Text-to-speech using Groq's PlayAI TTS API.

    Requires GROQ_API_KEY in the environment.
    Model is configurable via GROQ_TTS_MODEL (default: playai-tts).
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.model = os.environ.get("GROQ_TTS_MODEL", "playai-tts")
        self.api_url = "https://api.groq.com/openai/v1/audio/speech"

    async def synthesize(
        self,
        text: str,
        output_dir: Path,
        voice: str = "Arista-PlayAI",
        response_format: str = "wav",
    ) -> Path | None:
        """Synthesize speech from text and save to output_dir.

        Args:
            text: Text to convert to speech.
            output_dir: Directory to save the audio file.
            voice: Voice preset (Groq PlayAI voices: Arista-PlayAI, Atlas-PlayAI, etc.)
            response_format: Output format — "wav", "mp3", or "opus" (.ogg).

        Returns:
            Path to the generated audio file, or None on failure.
        """
        if not self.api_key:
            logger.warning("Groq API key not configured for TTS")
            return None

        ext_map = {"wav": ".wav", "mp3": ".mp3", "opus": ".ogg"}
        ext = ext_map.get(response_format, ".wav")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self.api_url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": text,
                        "voice": voice,
                        "response_format": response_format,
                    },
                    timeout=30.0,
                )
                resp.raise_for_status()

            output_dir.mkdir(parents=True, exist_ok=True)
            filename = f"tts_{int(time.time() * 1000)}{ext}"
            out_path = output_dir / filename
            out_path.write_bytes(resp.content)
            logger.info("TTS generated: %s (%d bytes)", out_path, len(resp.content))
            return out_path

        except Exception as exc:
            logger.error("Groq TTS error: %s", exc)
            return None
