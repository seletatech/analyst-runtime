"""Conversation-bound references to immutable analysis artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
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
        if payload.get("schema_version") == "analyst-runtime-answer/v1" and (
            not isinstance(payload.get("validation"), dict)
            or payload["validation"].get("passed") is not True
        ):
            raise AnalysisArtifactError("answer artifact did not pass deterministic validation")
        canonical = dict(payload)
        canonical.pop("analysis_id", None)
        encoded = json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(encoded).hexdigest() != match.group("digest"):
            raise AnalysisArtifactError("analysis artifact content does not match its identity")
        return payload

    @staticmethod
    def question_hash(question: str) -> str:
        normalized = " ".join(unicodedata.normalize("NFKC", question).casefold().split())
        return hashlib.sha256(normalized.encode()).hexdigest()

    @classmethod
    def completed_answer_key(
        cls,
        question: str,
        data_release_sha256: str,
        confirmed_semantics_sha256: str,
    ) -> str:
        return (
            f"{cls.question_hash(question)}:{data_release_sha256}:"
            f"{confirmed_semantics_sha256}"
        )

    def save_completed_answer(
        self,
        *,
        question: str,
        answer: str,
        data_manifest_sha256: str,
        confirmed_semantics_sha256: str,
        tools_used: list[str],
        evidence: list[dict[str, Any]],
    ) -> str:
        if not answer.strip() or not evidence:
            raise AnalysisArtifactError("answer artifact requires an answer and evidence")
        payload: dict[str, Any] = {
            "schema_version": "analyst-runtime-answer/v1",
            "status": "complete",
            "request": {
                "question_sha256": self.question_hash(question),
                "data_manifest_sha256": data_manifest_sha256,
                "confirmed_semantics_sha256": confirmed_semantics_sha256,
            },
            "population": {
                "answer": answer,
                "tools_used": tools_used,
                "evidence": evidence,
            },
            "validation": {
                "passed": True,
                "all_tool_calls_completed": True,
                "evidence_count": len(evidence),
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        analysis_id = f"chat-answer:{digest}"
        payload["analysis_id"] = analysis_id
        path = self.root / "chat-answer" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return analysis_id

    def matches_completed_answer(
        self,
        analysis_id: str,
        *,
        question: str,
        data_manifest_sha256: str,
        confirmed_semantics_sha256: str,
    ) -> bool:
        payload = self.load(analysis_id)
        request = payload.get("request", {})
        return bool(
            payload.get("schema_version") == "analyst-runtime-answer/v1"
            and request.get("question_sha256") == self.question_hash(question)
            and request.get("data_manifest_sha256") == data_manifest_sha256
            and request.get("confirmed_semantics_sha256")
            == confirmed_semantics_sha256
        )

    @staticmethod
    def system_instruction(payload: dict[str, Any]) -> str:
        kind, digest = str(payload["analysis_id"]).split(":", 1)
        compact = {
            "analysis_id": payload["analysis_id"],
            "artifact_path": f"/workspace/artifacts/analyses/{kind}/{digest}.json",
            "request": payload["request"],
            "population": payload["population"],
        }
        if payload.get("schema_version") == "analyst-runtime-answer/v1":
            compact["validation"] = payload["validation"]
            return (
                "Completed answer artifact for this exact request and data release:\n"
                + json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\nThe recorded tool run completed and produced evidence; this is a reuse "
                "cache, not an independent approval of the business conclusion. Answer from "
                "population.answer now without calling tools or repeating the investigation."
            )
        instruction = (
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
        return instruction
