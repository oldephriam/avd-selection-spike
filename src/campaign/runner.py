from __future__ import annotations

from .event_log import EventLog
from .fake_driver import FakeDriver, LostAcknowledgment
from .models import Job, ResolutionEvidence, RosterSnapshot, RunStatus
from .state_machine import ensure_transition
from .verifier import RecipientVerifier, VerificationResult


_TERMINAL = {
    RunStatus.SENT,
    RunStatus.SKIPPED_UNMATCHED_THREAD,
    RunStatus.SKIPPED_UNVERIFIED,
    RunStatus.NEEDS_REVIEW,
}


class Runner:
    """Orchestrates jobs through FakeDriver only; no transport knowledge."""

    def __init__(self, snapshot: RosterSnapshot, event_log: EventLog, driver: FakeDriver,
                 verifier: RecipientVerifier | None = None):
        if not isinstance(driver, FakeDriver):
            raise TypeError("offline prototype Runner accepts FakeDriver only")
        self.snapshot = snapshot
        self.event_log = event_log
        self.driver = driver
        self.verifier = verifier or RecipientVerifier()

    def run(self, job: Job, evidence: ResolutionEvidence | None) -> RunStatus:
        current = self.event_log.latest_status(job.job_id)
        if current in _TERMINAL:
            return current
        if current == RunStatus.DISPATCH_ATTEMPTED:
            self._append(job.job_id, RunStatus.NEEDS_REVIEW,
                         note="restart found an unacknowledged dispatch attempt; retry blocked")
            return RunStatus.NEEDS_REVIEW

        result = self.verifier.verify(self.snapshot, job, evidence)
        if result == VerificationResult.MISSING:
            self._append(job.job_id, RunStatus.SKIPPED_UNVERIFIED, note="recipient evidence missing")
            return RunStatus.SKIPPED_UNVERIFIED
        if result == VerificationResult.MISMATCH:
            self._append(job.job_id, RunStatus.SKIPPED_UNMATCHED_THREAD,
                         evidence=evidence.to_dict() if evidence else None,
                         note="recipient evidence does not match locked snapshot row")
            return RunStatus.SKIPPED_UNMATCHED_THREAD
        if result == VerificationResult.STALE:
            self._append(job.job_id, RunStatus.STALE_EVIDENCE_RETRY,
                         evidence=evidence.to_dict() if evidence else None,
                         note="resolution evidence stale; fresh verification required")
            return RunStatus.STALE_EVIDENCE_RETRY

        evidence_dict = evidence.to_dict()
        self._append(job.job_id, RunStatus.BOUND, evidence=evidence_dict,
                     note="recipient evidence matched immutable snapshot")
        self._append(job.job_id, RunStatus.DISPATCH_ATTEMPTED, evidence=evidence_dict,
                     note="fake driver would-send attempt recorded")
        try:
            self.driver.dispatch(job, evidence)
        except LostAcknowledgment:
            self._append(job.job_id, RunStatus.NEEDS_REVIEW, evidence=evidence_dict,
                         note="acknowledgment lost after fake dispatch; automatic retry prohibited")
            raise
        return self._complete_sent(job, evidence_dict)

    def _complete_sent(self, job: Job, evidence: dict[str, object]) -> RunStatus:
        self._append(job.job_id, RunStatus.SENT, evidence=evidence,
                     note="fake acknowledgment; no real message was sent")
        return RunStatus.SENT

    def _append(self, job_id: str, status: RunStatus, **kwargs: object) -> None:
        ensure_transition(self.event_log.latest_status(job_id), status)
        self.event_log.append(job_id, status, **kwargs)

    def on_late_resolution(self, job: Job, evidence: ResolutionEvidence) -> RunStatus:
        current = self.event_log.latest_status(job.job_id)
        if current not in {RunStatus.SKIPPED_UNVERIFIED, RunStatus.SKIPPED_UNMATCHED_THREAD,
                           RunStatus.STALE_EVIDENCE_RETRY}:
            return current or RunStatus.PENDING
        self._append(job.job_id, RunStatus.STALE_EVIDENCE_RETRY, evidence=evidence.to_dict(),
                     note="late resolution arrived after prior terminal evidence; fresh re-verification required")
        return RunStatus.STALE_EVIDENCE_RETRY
