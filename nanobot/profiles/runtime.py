"""Runtime-profile seam used by the generic agent loop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.memory import ProtectedMemorySection
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.profiles.types import ProfileTurnContext
from nanobot.workspace import WorkspaceConfiguration


class RuntimeProfiles:
    """Load and coordinate the optional behavior selected by one workspace."""

    def __init__(
        self,
        workspace: Path,
        configuration: WorkspaceConfiguration | None = None,
    ) -> None:
        configuration = configuration or WorkspaceConfiguration.load(workspace)
        active_profiles = frozenset(configuration.runtime_profiles)
        for name, profile_type in self._supported_profiles().items():
            if name not in active_profiles:
                profile_type.ensure_safe_to_disable(workspace)
        self._profiles = [self._create(name, workspace) for name in configuration.runtime_profiles]

    @staticmethod
    def _supported_profiles() -> dict[str, type]:
        from nanobot.profiles.manufacturing.profile import ManufacturingSemanticsProfile

        return {"manufacturing-semantics": ManufacturingSemanticsProfile}

    @classmethod
    def _create(cls, name: str, workspace: Path):
        profile_type = cls._supported_profiles().get(name)
        if profile_type is None:
            raise ValueError(f"Unsupported NanoBot runtime profile: {name!r}")
        return profile_type(workspace)

    @property
    def control_tool_names(self) -> frozenset[str]:
        return frozenset(
            tool_name
            for profile in self._profiles
            for tool_name in profile.control_tool_names
        )

    @property
    def protected_memory_sections(self) -> tuple[ProtectedMemorySection, ...]:
        return tuple(
            section
            for profile in self._profiles
            for section in profile.protected_memory_sections
        )

    def register_tools(self, tools: ToolRegistry) -> None:
        for profile in self._profiles:
            profile.register_tools(tools)

    def set_tool_context(self, context: ProfileTurnContext) -> None:
        for profile in self._profiles:
            profile.set_tool_context(context)

    def handoff_message(self, tool_name: str, result: Any) -> str | None:
        for profile in self._profiles:
            if message := profile.handoff_message(tool_name, result):
                return message
        return None

    def skipped_after_handoff_result(self) -> str:
        for profile in self._profiles:
            if profile.control_tool_names:
                return profile.skipped_after_handoff_result()
        return json.dumps(
            {"status": "profile_handoff", "error": "tool call skipped after profile handoff"}
        )

    def blocked_non_control_result(self, active_control_tools: frozenset[str]) -> str:
        for profile in self._profiles:
            if profile.control_tool_names.intersection(active_control_tools):
                return profile.blocked_non_control_result()
        return json.dumps(
            {
                "status": "profile_control_required",
                "error": "non-control tools are blocked in a profile-control turn",
            }
        )

    def handoff_finish_reason(self, active_control_tools: frozenset[str]) -> str:
        for profile in self._profiles:
            if profile.control_tool_names.intersection(active_control_tools):
                return profile.handoff_finish_reason
        return "profile_handoff"

    async def replay_one(self, bus: MessageBus) -> None:
        for profile in self._profiles:
            if await profile.replay_one(bus):
                return

    def enrich_outbound_metadata(self, metadata: dict[str, Any]) -> None:
        for profile in self._profiles:
            profile.enrich_outbound_metadata(metadata)
