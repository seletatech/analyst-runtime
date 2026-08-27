"""Session management for conversation history.

NOTE: Session files grow unbounded without periodic cleanup. Call
``SessionManager.cleanup_old_sessions()`` from a maintenance task or
on startup to prune stale sessions.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Event type for compressed conversation history summaries.
# Written by AgentLoop._compress_history(); rendered as a synthetic context
# message at the start of get_history() output so the LLM sees what happened
# before the sliding window without loading the full raw event log.
HISTORY_SUMMARY_TYPE = "history_summary"

from loguru import logger

from nanobot.utils.helpers import ensure_dir, safe_filename
from nanobot.utils.tool_calls import sanitize_openai_tool_calls, sanitize_tool_name


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

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
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

        # First pass: collect IDs of tool_calls that have a matching tool_result event,
        # so we can skip orphaned llm_response blocks in the second pass.
        # Bedrock rejects any sequence where a tool_use block is not immediately
        # followed by a matching tool_result block.
        result_ids: set[str] = {
            e["tool_use_id"]
            for e in self.events
            if e.get("type") == "tool_result" and e.get("tool_use_id")
        }

        out: list[dict[str, Any]] = []
        for e in self.events:
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
                    if not call_ids.issubset(result_ids):
                        logger.warning(
                            "Skipping orphaned llm_response in history: "
                            "tool_calls {} have no matching tool_result events",
                            call_ids - result_ids,
                        )
                        continue
                entry: dict[str, Any] = {"role": "assistant", "content": e.get("content") or ""}
                if tool_calls:
                    entry["tool_calls"] = sanitize_openai_tool_calls(tool_calls)
                if e.get("reasoning"):
                    entry["reasoning_content"] = e["reasoning"]
                out.append(entry)
            elif t == "tool_result":
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
            return out

        trimmed = out[-max_messages:]
        for index, message in enumerate(trimmed):
            if message.get("role") == "user":
                return trimmed[index:]
        return []

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
        """Save a session to disk."""
        path = self._get_session_path(session.key)

        with open(path, "w") as f:
            metadata_line = {
                "_type": "metadata",
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line) + "\n")
            for event in session.events:
                f.write(json.dumps(event) + "\n")

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
