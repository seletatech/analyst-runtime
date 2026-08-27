"""Local durable journal for confirmed-semantics task handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from uuid import UUID

from nanobot.profiles.manufacturing.memory import ConfirmedSemanticEntry

_SEMANTIC_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$")


class ConfirmationIntentJournalError(ValueError):
    """Raised when the local confirmation journal is invalid."""


@dataclass(frozen=True)
class ConfirmationIntent:
    intent_id: str
    channel: str
    chat_id: str
    conversation_id: str
    created_at: str
    run_id: str
    semantic_hash: str
    semantic_key: str
    semantic_snapshot: dict[str, object]

    def to_wire_dict(self) -> dict[str, object]:
        return {
            "intent_id": self.intent_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "semantic_hash": self.semantic_hash,
            "semantic_key": self.semantic_key,
            "semantic_snapshot": self.semantic_snapshot,
        }

    def to_journal_dict(self) -> dict[str, object]:
        return {
            **self.to_wire_dict(),
            "channel": self.channel,
            "chat_id": self.chat_id,
        }

    def to_semantic_entry(self) -> ConfirmedSemanticEntry:
        return ConfirmedSemanticEntry.from_dict(
            {
                "confirmed_at": self.created_at,
                "semantic_hash": self.semantic_hash,
                "semantic_key": self.semantic_key,
                "semantic_snapshot": self.semantic_snapshot,
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ConfirmationIntent:
        snapshot = value.get("semantic_snapshot")
        if not isinstance(snapshot, dict):
            raise ConfirmationIntentJournalError("semantic_snapshot must be an object")
        intent = _create_intent(
            channel=_text(value, "channel"),
            chat_id=_text(value, "chat_id"),
            conversation_id=_text(value, "conversation_id"),
            created_at=_text(value, "created_at"),
            run_id=_text(value, "run_id"),
            semantic_hash=_text(value, "semantic_hash"),
            semantic_key=_text(value, "semantic_key"),
            semantic_snapshot=snapshot,
        )
        if intent.intent_id != value.get("intent_id"):
            raise ConfirmationIntentJournalError("confirmation intent id mismatch")
        return intent


class ConfirmationIntentJournal:
    """Two-phase, restart-safe journal backed by the protected memory volume."""

    def __init__(self, workspace: Path) -> None:
        self.root = Path(workspace) / "memory" / "confirmation-intents"
        self.prepared_root = self.root / "prepared"
        self.pending_root = self.root / "pending"
        self.delivered_root = self.root / "delivered"
        self.quarantined_root = self.root / "quarantined"

    def prepare(
        self,
        *,
        channel: str,
        chat_id: str,
        conversation_id: str,
        created_at: str,
        run_id: str,
        semantic_hash: str,
        semantic_key: str,
        semantic_snapshot: Mapping[str, object],
    ) -> ConfirmationIntent:
        intent = _create_intent(
            channel=channel,
            chat_id=chat_id,
            conversation_id=conversation_id,
            created_at=created_at,
            run_id=run_id,
            semantic_hash=semantic_hash,
            semantic_key=semantic_key,
            semantic_snapshot=semantic_snapshot,
        )
        self._write(self.prepared_root / f"{intent.intent_id}.json", intent)
        return intent

    def commit(self, intent_id: str) -> ConfirmationIntent:
        pending = self.pending_root / f"{intent_id}.json"
        if pending.exists():
            return self._read(pending)
        prepared = self.prepared_root / f"{intent_id}.json"
        intent = self._read(prepared)
        _ensure_directory_durable(pending.parent)
        prepared.replace(pending)
        _fsync_directory(prepared.parent)
        _fsync_directory(pending.parent)
        return intent

    def discard_prepared(self, intent_id: str) -> None:
        prepared = self.prepared_root / f"{intent_id}.json"
        prepared.unlink(missing_ok=True)
        if prepared.parent.exists():
            _fsync_directory(prepared.parent)

    def recover(
        self,
        confirmed_entries: Iterable[ConfirmedSemanticEntry],
        restore_confirmed: Callable[[ConfirmedSemanticEntry], ConfirmedSemanticEntry] | None = None,
    ) -> list[ConfirmationIntent]:
        confirmed = {entry.semantic_key: entry for entry in confirmed_entries}
        if self.prepared_root.exists():
            for path in sorted(self.prepared_root.glob("confirmation-*.json")):
                try:
                    intent = self._read(path)
                except ConfirmationIntentJournalError:
                    self._quarantine(path)
                    continue
                current = confirmed.get(intent.semantic_key)
                exact_match = current is not None and (
                    current.semantic_hash == intent.semantic_hash
                    and current.confirmed_at == intent.created_at
                )
                if exact_match:
                    self.commit(intent.intent_id)
                    continue
                if current is not None and current.confirmed_at >= intent.created_at:
                    self.commit(intent.intent_id)
                    continue
                if restore_confirmed is None:
                    continue
                restored = restore_confirmed(intent.to_semantic_entry())
                confirmed[restored.semantic_key] = restored
                self.commit(intent.intent_id)
        return self.pending()

    def pending(self) -> list[ConfirmationIntent]:
        if not self.pending_root.exists():
            return []
        pending: list[ConfirmationIntent] = []
        for path in sorted(self.pending_root.glob("confirmation-*.json")):
            try:
                pending.append(self._read(path))
            except ConfirmationIntentJournalError:
                self._quarantine(path)
        return pending

    def quarantined_count(self) -> int:
        if not self.quarantined_root.exists():
            return 0
        return sum(1 for _ in self.quarantined_root.glob("confirmation-*.json"))

    def mark_delivered(self, intent_id: str) -> None:
        pending = self.pending_root / f"{intent_id}.json"
        if not pending.exists():
            if (self.delivered_root / f"{intent_id}.json").exists():
                return
            raise ConfirmationIntentJournalError("pending confirmation intent not found")
        _ensure_directory_durable(self.delivered_root)
        pending.replace(self.delivered_root / pending.name)
        _fsync_directory(pending.parent)
        _fsync_directory(self.delivered_root)

    def _quarantine(self, path: Path) -> None:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        except FileNotFoundError:
            return
        _ensure_directory_durable(self.quarantined_root)
        target = self.quarantined_root / f"{path.stem}-{digest}.json"
        if target.exists():
            path.unlink(missing_ok=True)
            _fsync_directory(path.parent)
            return
        path.replace(target)
        _fsync_directory(path.parent)
        _fsync_directory(target.parent)

    @staticmethod
    def _read(path: Path) -> ConfirmationIntent:
        try:
            wrapper = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ConfirmationIntentJournalError(
                "confirmation intent journal entry is unavailable"
            ) from error
        content = wrapper.get("content") if isinstance(wrapper, dict) else None
        if not isinstance(content, dict) or wrapper.get("content_sha256") != _sha256(content):
            raise ConfirmationIntentJournalError("confirmation intent digest mismatch")
        intent = ConfirmationIntent.from_mapping(content)
        if path.stem != intent.intent_id:
            raise ConfirmationIntentJournalError(
                "confirmation intent filename does not match its identity"
            )
        return intent

    @staticmethod
    def _write(path: Path, intent: ConfirmationIntent) -> None:
        content = intent.to_journal_dict()
        wrapper = {"content": content, "content_sha256": _sha256(content)}
        _ensure_directory_durable(path.parent)
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".intent.", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(wrapper, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                existing = ConfirmationIntentJournal._read(path)
                if existing != intent:
                    raise ConfirmationIntentJournalError(
                        "confirmation intent id is bound to different content"
                    )
                _fsync_directory(path.parent)
                return
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def _create_intent(
    *,
    channel: str,
    chat_id: str,
    conversation_id: str,
    created_at: str,
    run_id: str,
    semantic_hash: str,
    semantic_key: str,
    semantic_snapshot: Mapping[str, object],
) -> ConfirmationIntent:
    snapshot = dict(semantic_snapshot)
    if _sha256(snapshot) != semantic_hash:
        raise ConfirmationIntentJournalError("semantic hash does not match snapshot")
    normalized_conversation_id = _uuid_identity(conversation_id, "conversation_id")
    normalized_run_id = _uuid_identity(run_id, "run_id")
    normalized_semantic_key = _semantic_key(semantic_key)
    identity = {
        "conversation_id": normalized_conversation_id,
        "run_id": normalized_run_id,
        "semantic_hash": semantic_hash,
        "semantic_key": normalized_semantic_key,
    }
    return ConfirmationIntent(
        intent_id=f"confirmation-{_sha256(identity)[:32]}",
        channel=_bounded(channel, "channel"),
        chat_id=_bounded(chat_id, "chat_id"),
        conversation_id=normalized_conversation_id,
        created_at=_bounded(created_at, "created_at"),
        run_id=normalized_run_id,
        semantic_hash=semantic_hash,
        semantic_key=normalized_semantic_key,
        semantic_snapshot=snapshot,
    )


def _sha256(value: Mapping[str, object]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text(value: Mapping[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        raise ConfirmationIntentJournalError(f"{key} must be text")
    return raw


def _bounded(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ConfirmationIntentJournalError(f"{field} must be 1-200 characters")
    return normalized


def _uuid_identity(value: str, field: str) -> str:
    normalized = _bounded(value, field)
    try:
        parsed = UUID(normalized)
    except ValueError as error:
        raise ConfirmationIntentJournalError(f"{field} must be a UUID") from error
    canonical = str(parsed)
    if normalized.lower() != canonical:
        raise ConfirmationIntentJournalError(f"{field} must be a canonical UUID")
    return canonical


def _semantic_key(value: str) -> str:
    normalized = value.strip()
    if not _SEMANTIC_KEY.fullmatch(normalized):
        raise ConfirmationIntentJournalError("semantic_key is invalid")
    return normalized


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        _fsync_directory(directory.parent)
