"""Conversation-bound references to immutable analysis artifacts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ANALYSIS_ID = re.compile(r"^(?P<kind>[a-z][a-z0-9-]*):(?P<digest>[0-9a-f]{64})$")


class AnalysisArtifactError(ValueError):
    """Raised when a bound analysis artifact cannot be trusted."""


class AnalysisArtifactStore:
    """Resolve compact analysis context from an opaque analysis identity."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace / "artifacts" / "analyses"

    def load(self, analysis_id: str) -> dict[str, Any]:
        match = ANALYSIS_ID.fullmatch(analysis_id)
        if match is None:
            raise AnalysisArtifactError("invalid analysis identity")
        path = self.root / match.group("kind") / f"{match.group('digest')}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AnalysisArtifactError("analysis artifact is unavailable") from error
        if not isinstance(payload, dict):
            raise AnalysisArtifactError("analysis artifact must be an object")
        if payload.get("analysis_id") != analysis_id or payload.get("status") != "complete":
            raise AnalysisArtifactError("analysis artifact identity or status is invalid")
        if not isinstance(payload.get("request"), dict) or not isinstance(
            payload.get("population"), dict
        ):
            raise AnalysisArtifactError("analysis artifact lacks compact reusable context")
        return payload

    @staticmethod
    def system_instruction(payload: dict[str, Any]) -> str:
        compact = {
            "analysis_id": payload["analysis_id"],
            "request": payload["request"],
            "population": payload["population"],
        }
        return (
            "Active approved analysis context for this conversation:\n"
            + json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\nIf the user's analysis scope and definitions are unchanged, reuse this exact "
            "analysis and its figures. Do not rerun a tool, reconstruct a command, or "
            "recalculate it. This context approves reuse only; it does not approve access "
            "to new business data for a causal investigation. Confirm that new causal scope "
            "before using tools, then keep this population as its numeric anchor. When "
            "checking whether a "
            "previously quoted number is supported, compare it only with this artifact; do "
            "not search chat sessions or workspace files for matching digits unless the user "
            "explicitly asks to locate that quote's source. If the user materially changes "
            "scope or definitions, clarify that change before creating a new analysis."
        )
