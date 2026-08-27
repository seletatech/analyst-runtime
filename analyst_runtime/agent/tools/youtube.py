"""YouTube Data API v3 tool."""

import json
import os
from typing import Any

import httpx

from analyst_runtime.agent.tools.base import Tool


class YouTubeTool(Tool):
    """Search and retrieve YouTube video/channel information."""

    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("YOUTUBE_API_KEY", "")

    @property
    def name(self) -> str:
        return "youtube"

    @property
    def description(self) -> str:
        return "Search YouTube videos and get video/channel details. Actions: search_videos, get_channel_info, get_video_details."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search_videos", "get_channel_info", "get_video_details"],
                    "description": "The action to perform",
                },
                "query": {"type": "string", "description": "Search query (search_videos)"},
                "max_results": {"type": "integer", "description": "Max results (search_videos)", "minimum": 1, "maximum": 25},
                "channel_id": {"type": "string", "description": "Channel ID (get_channel_info)"},
                "video_id": {"type": "string", "description": "Video ID (get_video_details)"},
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        if not self._api_key:
            return "not_configured"

        if action == "search_videos":
            return await self._search_videos(
                query=kwargs.get("query", ""),
                max_results=kwargs.get("max_results", 5),
            )
        elif action == "get_channel_info":
            return await self._get_channel_info(channel_id=kwargs.get("channel_id", ""))
        elif action == "get_video_details":
            return await self._get_video_details(video_id=kwargs.get("video_id", ""))
        return f"Unknown action: {action}"

    async def _search_videos(self, query: str, max_results: int) -> str:
        if not query:
            return "Error: query is required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE_URL}/search",
                    params={
                        "part": "snippet",
                        "q": query,
                        "type": "video",
                        "maxResults": min(max(max_results, 1), 25),
                        "key": self._api_key,
                    },
                    timeout=15.0,
                )
                r.raise_for_status()
            items = r.json().get("items", [])
            results = [
                {
                    "video_id": item.get("id", {}).get("videoId", ""),
                    "title": item.get("snippet", {}).get("title", ""),
                    "channel": item.get("snippet", {}).get("channelTitle", ""),
                    "published": item.get("snippet", {}).get("publishedAt", ""),
                    "description": item.get("snippet", {}).get("description", "")[:200],
                }
                for item in items
            ]
            return json.dumps({"results": results, "count": len(results)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _get_channel_info(self, channel_id: str) -> str:
        if not channel_id:
            return "Error: channel_id is required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE_URL}/channels",
                    params={
                        "part": "snippet,statistics",
                        "id": channel_id,
                        "key": self._api_key,
                    },
                    timeout=15.0,
                )
                r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                return json.dumps({"error": "Channel not found"})

            ch = items[0]
            snippet = ch.get("snippet", {})
            stats = ch.get("statistics", {})
            return json.dumps({
                "id": channel_id,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[:500],
                "subscribers": stats.get("subscriberCount", ""),
                "video_count": stats.get("videoCount", ""),
                "view_count": stats.get("viewCount", ""),
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _get_video_details(self, video_id: str) -> str:
        if not video_id:
            return "Error: video_id is required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    f"{self.BASE_URL}/videos",
                    params={
                        "part": "snippet,statistics,contentDetails",
                        "id": video_id,
                        "key": self._api_key,
                    },
                    timeout=15.0,
                )
                r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                return json.dumps({"error": "Video not found"})

            v = items[0]
            snippet = v.get("snippet", {})
            stats = v.get("statistics", {})
            details = v.get("contentDetails", {})
            return json.dumps({
                "id": video_id,
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "published": snippet.get("publishedAt", ""),
                "description": snippet.get("description", "")[:500],
                "duration": details.get("duration", ""),
                "views": stats.get("viewCount", ""),
                "likes": stats.get("likeCount", ""),
                "comments": stats.get("commentCount", ""),
            })
        except Exception as e:
            return json.dumps({"error": str(e)})
