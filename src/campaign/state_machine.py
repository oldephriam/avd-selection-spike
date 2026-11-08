from __future__ import annotations

from .models import RunStatus


class InvalidTransition(ValueError):
    pass


_ALLOWED: dict[RunStatus | None, set[RunStatus]] = {
    None: {RunStatus.BOUND, RunStatus.SKIPPED_UNMATCHED_THREAD, RunStatus.SKIPPED_UNVERIFIED,
           RunStatus.STALE_EVIDENCE_RETRY},
    RunStatus.PENDING: {RunStatus.BOUND, RunStatus.SKIPPED_UNMATCHED_THREAD,
                        RunStatus.SKIPPED_UNVERIFIED, RunStatus.STALE_EVIDENCE_RETRY},
    RunStatus.BOUND: {RunStatus.DISPATCH_ATTEMPTED},
    RunStatus.DISPATCH_ATTEMPTED: {RunStatus.SENT, RunStatus.NEEDS_REVIEW},
    RunStatus.SKIPPED_UNMATCHED_THREAD: {RunStatus.STALE_EVIDENCE_RETRY},
    RunStatus.SKIPPED_UNVERIFIED: {RunStatus.STALE_EVIDENCE_RETRY},
    RunStatus.STALE_EVIDENCE_RETRY: {RunStatus.STALE_EVIDENCE_RETRY, RunStatus.BOUND,
                                     RunStatus.SKIPPED_UNMATCHED_THREAD,
                                     RunStatus.SKIPPED_UNVERIFIED},
    RunStatus.SENT: set(),
    RunStatus.NEEDS_REVIEW: set(),
}


def ensure_transition(current: RunStatus | None, target: RunStatus) -> None:
    if target not in _ALLOWED.get(current, set()):
        before = current.value if current else "INITIAL"
        raise InvalidTransition(f"transition {before} -> {target.value} is not allowed")
