from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from .models import Job, ResolutionEvidence, RosterSnapshot


class VerificationResult(str, Enum):
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    MISSING = "MISSING"
    STALE = "STALE"


class RecipientVerifier:
    def __init__(self, *, max_age: timedelta = timedelta(seconds=30)):
        self.max_age = max_age

    def verify(
        self,
        snapshot: RosterSnapshot,
        job: Job,
        evidence: ResolutionEvidence | None,
        *,
        now: datetime | None = None,
    ) -> VerificationResult:
        if evidence is None:
            return VerificationResult.MISSING
        expected = snapshot.contact_at(job.roster_index)
        if (
            job.campaign_id != snapshot.campaign_id
            or job.snapshot_id != snapshot.snapshot_id
            or evidence.snapshot_id != snapshot.snapshot_id
            or evidence.roster_index != job.roster_index
            or evidence.recipient_key != expected.recipient_key
            or job.recipient_key != expected.recipient_key
        ):
            return VerificationResult.MISMATCH
        current = now or datetime.now(timezone.utc)
        observed = evidence.observed_at.astimezone(timezone.utc)
        age = current.astimezone(timezone.utc) - observed
        if age > self.max_age or age < timedelta(seconds=-5):
            return VerificationResult.STALE
        return VerificationResult.VERIFIED
