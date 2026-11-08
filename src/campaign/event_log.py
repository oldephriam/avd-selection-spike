from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from .models import Event, RunStatus


class EventLogError(RuntimeError):
    pass


class DuplicateEventError(EventLogError):
    pass


class EventLog:
    """Single-writer append-only, hash-chained JSONL event ledger."""

    GENESIS = "0" * 64

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _canonical(value: dict[str, object]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def events(self) -> list[Event]:
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise EventLogError("incomplete final event line; refusing replay")
        result: list[Event] = []
        previous = self.GENESIS
        seen: set[str] = set()
        for expected_seq, line in enumerate(raw.splitlines(), start=1):
            try:
                record = json.loads(line)
                event_hash = record.pop("event_hash")
            except (json.JSONDecodeError, KeyError) as exc:
                raise EventLogError(f"invalid event at sequence {expected_seq}") from exc
            if record.get("sequence") != expected_seq or record.get("previous_hash") != previous:
                raise EventLogError(f"broken event chain at sequence {expected_seq}")
            if record.get("schema_version") != 1:
                raise EventLogError(f"unsupported event schema at sequence {expected_seq}")
            if record.get("event_id") in seen:
                raise DuplicateEventError(f"duplicate event ID at sequence {expected_seq}")
            calculated = hashlib.sha256(self._canonical(record)).hexdigest()
            if calculated != event_hash:
                raise EventLogError(f"event hash mismatch at sequence {expected_seq}")
            seen.add(record["event_id"])
            previous = event_hash
            result.append(Event(
                event_id=record["event_id"], schema_version=record["schema_version"],
                sequence=record["sequence"], job_id=record["job_id"],
                status=RunStatus(record["status"]), occurred_at=record["occurred_at"],
                evidence=record.get("evidence"), note=record.get("note", ""),
                previous_hash=record["previous_hash"], event_hash=event_hash,
            ))
        return result

    def append(
        self,
        job_id: str,
        status: RunStatus,
        *,
        event_id: str | None = None,
        evidence: dict[str, object] | None = None,
        note: str = "",
    ) -> Event:
        existing = self.events()
        event_id = event_id or str(uuid.uuid4())
        if any(event.event_id == event_id for event in existing):
            raise DuplicateEventError(f"event ID already exists: {event_id}")
        previous = existing[-1].event_hash if existing else self.GENESIS
        sequence = len(existing) + 1
        record: dict[str, object] = {
            "event_id": event_id,
            "schema_version": 1,
            "sequence": sequence,
            "job_id": job_id,
            "status": RunStatus(status).value,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "evidence": evidence,
            "note": note,
            "previous_hash": previous,
        }
        event_hash = hashlib.sha256(self._canonical(record)).hexdigest()
        full_record = dict(record, event_hash=event_hash)
        line = self._canonical(full_record) + b"\n"
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(line)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        return Event(
            event_id=event_id, schema_version=1, sequence=sequence, job_id=job_id, status=RunStatus(status),
            occurred_at=str(record["occurred_at"]), evidence=evidence, note=note,
            previous_hash=previous, event_hash=event_hash,
        )

    def latest_status(self, job_id: str) -> RunStatus | None:
        for event in reversed(self.events()):
            if event.job_id == job_id:
                return event.status
        return None

    def latest_event(self, job_id: str) -> Event | None:
        for event in reversed(self.events()):
            if event.job_id == job_id:
                return event
        return None
