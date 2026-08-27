"""Google Maps tool via Maps APIs."""

import json
import os
from typing import Any

import httpx

from analyst_runtime.agent.tools.base import Tool


class GoogleMapsTool(Tool):
    """Search places, get directions, and geocode addresses via Google Maps."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY", "")

    @property
    def name(self) -> str:
        return "google_maps"

    @property
    def description(self) -> str:
        return "Google Maps: search places, get directions, geocode addresses. Actions: search_places, get_directions, geocode."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search_places", "get_directions", "geocode"],
                    "description": "The action to perform",
                },
                "query": {"type": "string", "description": "Search query (search_places)"},
                "location": {"type": "string", "description": "Location bias lat,lng (search_places)"},
                "origin": {"type": "string", "description": "Origin address (get_directions)"},
                "destination": {"type": "string", "description": "Destination address (get_directions)"},
                "mode": {
                    "type": "string",
                    "enum": ["driving", "walking", "bicycling", "transit"],
                    "description": "Travel mode (get_directions)",
                },
                "address": {"type": "string", "description": "Address to geocode (geocode)"},
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> str:
        if not self._api_key:
            return "not_configured"

        if action == "search_places":
            return await self._search_places(
                query=kwargs.get("query", ""),
                location=kwargs.get("location"),
            )
        elif action == "get_directions":
            return await self._get_directions(
                origin=kwargs.get("origin", ""),
                destination=kwargs.get("destination", ""),
                mode=kwargs.get("mode", "driving"),
            )
        elif action == "geocode":
            return await self._geocode(address=kwargs.get("address", ""))
        return f"Unknown action: {action}"

    async def _search_places(self, query: str, location: str | None) -> str:
        if not query:
            return "Error: query is required"

        params: dict[str, str] = {
            "query": query,
            "key": self._api_key,
        }
        if location:
            params["location"] = location
            params["radius"] = "5000"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://maps.googleapis.com/maps/api/place/textsearch/json",
                    params=params,
                    timeout=15.0,
                )
                r.raise_for_status()
            results = r.json().get("results", [])
            places = [
                {
                    "name": p.get("name", ""),
                    "address": p.get("formatted_address", ""),
                    "rating": p.get("rating"),
                    "location": p.get("geometry", {}).get("location", {}),
                    "place_id": p.get("place_id", ""),
                }
                for p in results[:10]
            ]
            return json.dumps({"places": places, "count": len(places)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _get_directions(self, origin: str, destination: str, mode: str) -> str:
        if not origin or not destination:
            return "Error: origin and destination are required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://maps.googleapis.com/maps/api/directions/json",
                    params={
                        "origin": origin,
                        "destination": destination,
                        "mode": mode,
                        "key": self._api_key,
                    },
                    timeout=15.0,
                )
                r.raise_for_status()
            data = r.json()
            routes = data.get("routes", [])
            if not routes:
                return json.dumps({"error": "No routes found"})

            leg = routes[0].get("legs", [{}])[0]
            steps = [
                {
                    "instruction": s.get("html_instructions", ""),
                    "distance": s.get("distance", {}).get("text", ""),
                    "duration": s.get("duration", {}).get("text", ""),
                }
                for s in leg.get("steps", [])
            ]
            return json.dumps({
                "distance": leg.get("distance", {}).get("text", ""),
                "duration": leg.get("duration", {}).get("text", ""),
                "start_address": leg.get("start_address", ""),
                "end_address": leg.get("end_address", ""),
                "steps": steps,
            })
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _geocode(self, address: str) -> str:
        if not address:
            return "Error: address is required"

        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": address, "key": self._api_key},
                    timeout=15.0,
                )
                r.raise_for_status()
            results = r.json().get("results", [])
            if not results:
                return json.dumps({"error": "No results found"})

            result = results[0]
            loc = result.get("geometry", {}).get("location", {})
            return json.dumps({
                "formatted_address": result.get("formatted_address", ""),
                "lat": loc.get("lat"),
                "lng": loc.get("lng"),
                "place_id": result.get("place_id", ""),
            })
        except Exception as e:
            return json.dumps({"error": str(e)})
