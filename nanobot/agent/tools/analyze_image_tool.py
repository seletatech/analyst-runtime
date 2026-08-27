"""Image vision analysis tool — send a local image file to the vision LLM via the gateway."""

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool


class AnalyzeImageTool(Tool):
    """Analyse a local image file using a vision-capable LLM.

    Sends the image through the gateway /tools/analyze_image endpoint,
    which reads it from the shared EFS workspace and calls the vision LLM.
    Primarily used to analyse video frames extracted with ffmpeg.
    """

    @property
    def name(self) -> str:
        return "analyze_image"

    @property
    def description(self) -> str:
        return (
            "Analyse a local image file with a vision LLM and return a description. "
            "Use this to examine video frames extracted with ffmpeg."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the image file (JPEG, PNG, etc.)",
                },
                "prompt": {
                    "type": "string",
                    "description": "What to ask about the image (default: describe what you see)",
                    "default": "Describe what you see in this image in detail.",
                },
            },
            "required": ["file_path"],
        }

    async def execute(self, file_path: str, prompt: str = "Describe what you see in this image in detail.", **kwargs: Any) -> str:
        from nanobot.agent.tools.gateway_auth import gateway_client

        p = Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"

        try:
            async with gateway_client(timeout=60.0) as client:
                resp = await client.post(
                    "/tools/analyze_image",
                    json={"file_path": file_path, "prompt": prompt},
                )
                if resp.status_code == 200:
                    return resp.json().get("analysis", "No analysis returned")
                return f"Vision analysis failed: HTTP {resp.status_code} — {resp.text[:200]}"
        except Exception as exc:
            return f"Vision analysis error: {exc}"
