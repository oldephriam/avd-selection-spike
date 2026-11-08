from __future__ import annotations

from dataclasses import dataclass

from .models import Job, ResolutionEvidence


class LostAcknowledgment(RuntimeError):
    """The fake driver recorded the dispatch, but the caller received no ACK."""


class SimulatedCrash(RuntimeError):
    """Crash after the fake driver records an attempted dispatch."""


@dataclass(frozen=True, slots=True)
class Attempt:
    job_id: str
    recipient_key: str
    payload_sha256: str
    evidence_id: str


class FakeDriver:
    """Non-sending driver. It only records simulated would-send attempts."""

    def __init__(self, outcome: str = "ack"):
        self.outcome = outcome
        self.attempts: list[Attempt] = []

    def dispatch(self, job: Job, evidence: ResolutionEvidence) -> None:
        if any(attempt.job_id == job.job_id for attempt in self.attempts):
            raise RuntimeError("duplicate fake dispatch attempt blocked")
        self.attempts.append(Attempt(job.job_id, job.recipient_key, job.payload_sha256, evidence.evidence_id))
        if self.outcome == "lost_ack":
            raise LostAcknowledgment("simulated acknowledgment loss")
        if self.outcome == "crash_after_record":
            raise SimulatedCrash("simulated process crash after attempt record")
        if self.outcome != "ack":
            raise ValueError(f"unsupported fake outcome: {self.outcome}")
