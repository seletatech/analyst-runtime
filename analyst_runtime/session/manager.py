"""Session management for conversation history.

NOTE: Session files grow unbounded without periodic cleanup. Call
``SessionManager.cleanup_old_sessions()`` from a maintenance task or
on startup to prune stale sessions.
"""

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from analyst_runtime.utils.helpers import ensure_dir, fsync_directory, safe_filename
from analyst_runtime.utils.tool_calls import sanitize_openai_tool_calls, sanitize_tool_name

# Event type for compressed conversation history summaries.
# Written by AgentLoop._compress_history(); rendered as a synthetic context
# message at the start of get_history() output so the LLM sees what happened
# before the sliding window without loading the full raw event log.
HISTORY_SUMMARY_TYPE = "history_summary"


@dataclass
class Session:
    """
    A conversation session.

    Stores events in JSONL format for full prompt and tool trace.

    Event types: user_input, prompt_snapshot, llm_response, tool_result, final_response.

    Events are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the events list or get_history() output.
    """

    key: str  # channel:chat_id
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Index into get_consolidation_events() already consolidated

    def add_event(self, event: dict) -> None:
        """Add an event to the session. Auto-fills session_id and timestamp."""
        event.setdefault("session_id", self.key)
        event.setdefault("timestamp", datetime.now().isoformat())
        self.events.append(event)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 500,
        *,
        context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Reconstruct LLM-compatible message list from events.

        - history_summary → synthetic user+assistant pair summarising older turns
        - user_input      → {"role": "user", "content": ...}
        - llm_response    → {"role": "assistant", "content": ..., "tool_calls": [...]}
        - tool_result     → {"role": "tool", "tool_call_id": ..., "name": ..., "content": ...}
        - final_response  → {"role": "assistant", "content": ...}
        - prompt_snapshot → skipped
        """
        if max_messages <= 0:
            return []
        max_messages = max(2, max_messages)

        # Scope results to their assistant batch, including older traces without UUIDs.
        # A later batch may reuse a provider's tool ID; it cannot complete an earlier one.
        batch_results: dict[int, list[int]] = {}
        assistant_index: int | None = None
        for index, event in enumerate(self.events):
            event_type = event.get("type")
            if event_type == "llm_response":
                assistant_index = index if event.get("tool_calls") else None
            elif event_type in {"user_input", "final_response", HISTORY_SUMMARY_TYPE}:
                assistant_index = None
            elif event_type == "tool_result" and assistant_index is not None:
                source_uuid = event.get("source_assistant_uuid")
                if source_uuid is not None and source_uuid != self.events[assistant_index].get(
                    "uuid"
                ):
                    continue
                batch_results.setdefault(assistant_index, []).append(index)

        out: list[dict[str, Any]] = []
        included_results: set[int] = set()
        for index, e in enumerate(self.events):
            t = e.get("type")
            if t == HISTORY_SUMMARY_TYPE:
                covered = e.get("covered_turns", "?")
                out.append({
                    "role": "user",
                    "content": f"[Earlier Conversation Summary — {covered} turns compressed]\n\n{e.get('content', '')}",
                })
                out.append({
                    "role": "assistant",
                    "content": "Understood, I have the context from our earlier conversation.",
                })
            elif t == "user_input":
                content = e.get("content") or ""
                if not content:
                    # Empty content (e.g. Telegram media message without caption).
                    # Bedrock rejects empty-content messages; substitute a short placeholder
                    # so the history remains structurally valid for the LLM.
                    media: list[str] = e.get("media") or []
                    if media:
                        labels = []
                        for path in media:
                            low = path.lower()
                            if any(low.endswith(x) for x in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                                labels.append("image")
                            elif any(low.endswith(x) for x in (".ogg", ".mp3", ".wav", ".m4a", ".aac")):
                                labels.append("voice message")
                            elif any(low.endswith(x) for x in (".mp4", ".mov", ".webm")):
                                labels.append("video")
                            else:
                                labels.append("file")
                        content = "[" + ", ".join(labels) + "]"
                    else:
                        content = "[message]"
                out.append({"role": "user", "content": content})
            elif t == "llm_response":
                tool_calls = e.get("tool_calls")
                if tool_calls:
                    # Only include this assistant message if ALL its tool_calls have
                    # matching tool_result events.  An orphaned tool_use (no result)
                    # causes Bedrock to reject the entire request; skip it so the
                    # conversation can continue cleanly from the final_response.
                    call_ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
                    results = batch_results.get(index, [])
                    result_ids = {self.events[result].get("tool_use_id") for result in results}
                    if not call_ids.issubset(result_ids):
                        logger.warning(
                            "Skipping orphaned llm_response in history: "
                            "tool_calls {} have no matching tool_result events",
                            call_ids - result_ids,
                        )
                        continue
                    included_results.update(
                        result
                        for result in results
                        if self.events[result].get("tool_use_id") in call_ids
                    )
                entry: dict[str, Any] = {"role": "assistant", "content": e.get("content") or ""}
                if tool_calls:
                    entry["tool_calls"] = sanitize_openai_tool_calls(tool_calls)
                if e.get("reasoning"):
                    entry["reasoning_content"] = e["reasoning"]
                out.append(entry)
            elif t == "tool_result":
                if index not in included_results:
                    continue
                out.append({
                    "role": "tool",
                    "tool_call_id": e.get("tool_use_id", ""),
                    "name": sanitize_tool_name(e.get("tool_name", ""), fallback=""),
                    "content": e.get("content", ""),
                })
            elif t == "final_response":
                out.append({"role": "assistant", "content": e.get("content") or ""})
            # prompt_snapshot → skip

        if len(out) <= max_messages:
            if context is not None:
                context.update(
                    {
                        "strategy": "full",
                        "messages_before": len(out),
                        "messages_after": len(out),
                        "messages_dropped": 0,
                        "turns_retained": sum(message.get("role") == "user" for message in out),
                        "messages_before_sha256": [self._message_sha256(message) for message in out],
                        "messages_after_sha256": [self._message_sha256(message) for message in out],
                    }
                )
            return out

        # Tool-heavy turns can exceed the whole message window by themselves. Keep
        # completed conversational outcomes, not an unusable tail of orphaned tool calls.
        turns: list[list[dict[str, Any]]] = []
        for message in out:
            if message.get("role") == "user":
                turns.append([message])
            elif turns:
                turns[-1].append(message)

        compacted: list[dict[str, Any]] = []
        for turn in turns:
            compacted.append(turn[0])
            final = next(
                (
                    message
                    for message in reversed(turn[1:])
                    if message.get("role") == "assistant" and not message.get("tool_calls")
                ),
                None,
            )
            if final is not None:
                compacted.append(final)

        trimmed = compacted[-max_messages:]
        while trimmed and trimmed[0].get("role") != "user":
            trimmed.pop(0)
        if context is not None:
            context.update(
                {
                    "strategy": "turn_outcomes",
                    "messages_before": len(out),
                    "messages_after": len(trimmed),
                    "messages_dropped": len(out) - len(trimmed),
                    "turns_retained": sum(
                        message.get("role") == "user" for message in trimmed
                    ),
                    "messages_before_sha256": [self._message_sha256(message) for message in out],
                    "messages_after_sha256": [self._message_sha256(message) for message in trimmed],
                }
            )
        return trimmed

    @staticmethod
    def _message_sha256(message: dict[str, Any]) -> str:
        encoded = json.dumps(
            message, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def count_turns(self) -> int:
        """Count conversation turns (number of user_input events)."""
        return sum(1 for e in self.events if e.get("type") == "user_input")

    def compress_events(self, summary: str, keep_turns: int) -> None:
        """Replace old events with a summary, keeping only the last keep_turns turns.

        The summary text is injected as a HISTORY_SUMMARY_TYPE event at the start
        of the retained event list so get_history() renders it as a synthetic
        context message. Any pre-existing summary event is replaced.

        Also resets last_consolidated to 0 because the old raw events it tracked
        have been removed.
        """
        user_input_indices = [i for i, e in enumerate(self.events) if e.get("type") == "user_input"]

        if len(user_input_indices) <= keep_turns:
            return  # Not enough turns to compress

        # Everything from the (N - keep_turns)th user_input onward is kept verbatim
        cut_at = user_input_indices[-keep_turns]
        events_to_keep = [
            e for e in self.events[cut_at:]
            if e.get("type") != HISTORY_SUMMARY_TYPE  # drop any stale summary
        ]

        summary_event: dict[str, Any] = {
            "type": HISTORY_SUMMARY_TYPE,
            "content": summary,
            "timestamp": datetime.now().isoformat(),
            "covered_turns": len(user_input_indices) - keep_turns,
        }

        self.events = [summary_event] + events_to_keep
        self.last_consolidated = 0  # raw events are gone; restart consolidation tracking
        self.updated_at = datetime.now()

    def get_consolidation_events(self) -> list[dict[str, Any]]:
        """Return only user_input and final_response events for memory consolidation."""
        return [e for e in self.events if e.get("type") in ("user_input", "final_response")]

    def clear(self) -> None:
        """Clear all events and reset session to initial state."""
        self.events = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self._cache: dict[str, Session] = {}

    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            events = []
            metadata = {}
            created_at = None
            last_consolidated = 0

            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    elif data.get("type"):
                        # Only load typed events (new format); skip untyped old-format messages
                        events.append(data)

            return Session(
                key=key,
                events=events,
                created_at=created_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning(f"Failed to load session {key}: {e}")
            return None

    def save(self, session: Session) -> None:
        """Replace the last complete snapshot only after its successor is flushed."""
        path = self._get_session_path(session.key)

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self.sessions_dir,
                prefix=f".{path.stem}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            # ponytail: rewrite each snapshot; use an append journal if session size affects latency.
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                metadata_line = {
                    "_type": "metadata",
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated,
                }
                f.write(json.dumps(metadata_line) + "\n")
                for event in session.events:
                    f.write(json.dumps(event) + "\n")
                f.flush()
                os.fsync(f.fileno())
            temporary_path.replace(path)
            fsync_directory(self.sessions_dir)
        except BaseException:
            self.invalidate(session.key)
            raise
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path) as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            sessions.append({
                                "key": path.stem.replace("_", ":"),
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue

        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def cleanup_old_sessions(self, older_than_days: int = 30) -> int:
        """Delete session files not accessed in *older_than_days* days.

        Returns the number of sessions removed.
        """
        cutoff = time.time() - (older_than_days * 86400)
        removed = 0
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                if path.stat().st_atime < cutoff:
                    key = path.stem.replace("_", ":")
                    path.unlink()
                    self._cache.pop(key, None)
                    removed += 1
                    logger.info("Removed stale session file: {}", path.name)
            except Exception as exc:
                logger.warning("Failed to remove session {}: {}", path.name, exc)
        if removed:
            logger.info("Session cleanup: removed {} stale session(s)", removed)
        return removed
