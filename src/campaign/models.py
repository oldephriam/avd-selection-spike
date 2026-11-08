from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json


class RunStatus(str, Enum):
    PENDING = "PENDING"
    BOUND = "BOUND"
    DISPATCH_ATTEMPTED = "DISPATCH_ATTEMPTED"
    SENT = "SENT"
    SKIPPED_UNMATCHED_THREAD = "SKIPPED_UNMATCHED_THREAD"
    SKIPPED_UNVERIFIED = "SKIPPED_UNVERIFIED"
    STALE_EVIDENCE_RETRY = "STALE_EVIDENCE_RETRY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


@dataclass(frozen=True, slots=True)
class Contact:
    roster_index: int
    display_name: str
    recipient_key: str


@dataclass(frozen=True, slots=True)
class RosterSnapshot:
    campaign_id: str
    source_sha256: str
    snapshot_id: str
    contacts: tuple[Contact, ...]

    def contact_at(self, roster_index: int) -> Contact:
        for contact in self.contacts:
            if contact.roster_index == roster_index:
                return contact
        raise KeyError(f"No roster entry at index {roster_index}")


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    campaign_id: str
    snapshot_id: str
    roster_index: int
    recipient_key: str
    payload: str
    payload_sha256: str

    @classmethod
    def create(cls, snapshot: RosterSnapshot, roster_index: int, payload: str) -> "Job":
        contact = snapshot.contact_at(roster_index)
        payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        stable = json.dumps(
            [snapshot.campaign_id, snapshot.snapshot_id, roster_index, contact.recipient_key, payload_hash],
            separators=(",", ":"),
        )
        job_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        return cls(job_id, snapshot.campaign_id, snapshot.snapshot_id, roster_index, contact.recipient_key, payload, payload_hash)


@dataclass(frozen=True, slots=True)
class ResolutionEvidence:
    snapshot_id: str
    roster_index: int
    recipient_key: str
    observed_at: datetime
    source: str
    evidence_id: str

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "roster_index": self.roster_index,
            "recipient_key": self.recipient_key,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "source": self.source,
            "evidence_id": self.evidence_id,
        }


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    schema_version: int
    sequence: int
    job_id: str
    status: RunStatus
    occurred_at: str
    evidence: dict[str, object] | None
    note: str
    previous_hash: str
    event_hash: str
