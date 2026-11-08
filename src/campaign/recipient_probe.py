from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .device_observer import (
    ContactCandidate,
    CapturePhase,
    DeviceObserver,
    ObservationError,
    SelectionActionResult,
    UnsafeDeviceError,
    UnsafeUIError,
)


class ProbeStatus(str, Enum):
    BOUND = "BOUND"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_FOUND = "NOT_FOUND"
    MISMATCH = "MISMATCH"
    UNVERIFIED = "UNVERIFIED"
    LAYOUT_CHANGED = "LAYOUT_CHANGED"
    SESSION_CHANGED = "SESSION_CHANGED"
    UNSAFE_DEVICE = "UNSAFE_DEVICE"
    UNSAFE_UI = "UNSAFE_UI"


@dataclass(frozen=True, slots=True)
class RecipientTarget:
    snapshot_id: str
    roster_index: int
    recipient_key: str


@dataclass(frozen=True, slots=True)
class SelectionEvidence:
    device_serial: str
    snapshot_id: str
    roster_index: int
    recipient_key: str
    candidate_id: str
    capture_id: str
    session_id: str
    layout_signature: str
    observed_at: str
    screenshot_sha256: str
    hierarchy_sha256: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    evidence: SelectionEvidence | None = None
    detail: str = ""


class RecipientSelectionProbe:
    """Selection-only probe. It has no method or capability to dispatch messages."""

    def __init__(self, observer: DeviceObserver):
        self.observer = observer

    def select(self, target: RecipientTarget) -> ProbeResult:
        baseline_session: str | None = None
        baseline_layout: str | None = None
        chosen: ContactCandidate | None = None
        selection_frame = None
        try:
            for _ in range(self.observer.max_polls):
                if baseline_session is not None:
                    self.observer.sleep_fn(self.observer.poll_interval_seconds)
                current = self.observer.capture(CapturePhase.PICKER_ACTIVE)
                if baseline_session is None:
                    baseline_session = current.session_id
                    baseline_layout = current.layout_signature
                elif current.session_id != baseline_session:
                    return ProbeResult(ProbeStatus.SESSION_CHANGED, detail="device/app process changed while resolving")
                elif current.layout_signature != baseline_layout:
                    return ProbeResult(ProbeStatus.LAYOUT_CHANGED, detail="picker layout changed while resolving")

                matches = [item for item in current.candidates if item.visible and item.recipient_key == target.recipient_key]
                if len(matches) > 1:
                    return ProbeResult(ProbeStatus.AMBIGUOUS, detail="multiple visible candidates share the target recipient key")
                if not matches:
                    continue
                chosen = matches[0]
                if chosen.roster_index != target.roster_index or chosen.snapshot_id != target.snapshot_id:
                    return ProbeResult(ProbeStatus.MISMATCH, detail="candidate index or snapshot differs from target")
                selection_frame = current
                break
            if chosen is None or selection_frame is None:
                return ProbeResult(ProbeStatus.NOT_FOUND, detail="no unique matching candidate within bounded passive polls")

            # Selection is bound to the fresh capture ID; adapter must reject stale handles.
            action_result = self.observer.port.select_candidate(
                self.observer.expected_serial, selection_frame.capture_id, chosen.candidate_id
            )
            if action_result is SelectionActionResult.LAYOUT_CHANGED:
                return ProbeResult(ProbeStatus.LAYOUT_CHANGED, detail="picker layout changed before candidate tap")
            if action_result is SelectionActionResult.SESSION_CHANGED:
                return ProbeResult(ProbeStatus.SESSION_CHANGED, detail="device/app session changed before candidate tap")
            if action_result is not SelectionActionResult.SELECTED:
                return ProbeResult(ProbeStatus.UNVERIFIED, detail="selection adapter rejected or could not bind the capture-bound candidate")

            selected_frame = self.observer.capture(CapturePhase.PROBE_RETURNED)
            if selected_frame.session_id != baseline_session:
                return ProbeResult(ProbeStatus.SESSION_CHANGED, detail="process/session changed after selection")
            selected = selected_frame.selected
            if selected is None:
                return ProbeResult(ProbeStatus.UNVERIFIED, detail="selected recipient was not readable after selection")
            if (
                selected.recipient_key != target.recipient_key
                or selected.roster_index != target.roster_index
                or selected.snapshot_id != target.snapshot_id
                or selected.candidate_id != chosen.candidate_id
            ):
                return ProbeResult(ProbeStatus.MISMATCH, detail="post-selection recipient readback mismatched target")

            evidence = SelectionEvidence(
                device_serial=selected_frame.serial,
                snapshot_id=target.snapshot_id,
                roster_index=target.roster_index,
                recipient_key=target.recipient_key,
                candidate_id=selected.candidate_id,
                capture_id=selected_frame.capture_id,
                session_id=selected_frame.session_id,
                layout_signature=selected_frame.layout_signature,
                observed_at=selected_frame.captured_at.astimezone(timezone.utc).isoformat(),
                screenshot_sha256=selected_frame.screenshot_sha256,
                hierarchy_sha256=selected_frame.hierarchy_sha256,
            )
            return ProbeResult(ProbeStatus.BOUND, evidence=evidence)
        except UnsafeDeviceError as exc:
            return ProbeResult(ProbeStatus.UNSAFE_DEVICE, detail=str(exc))
        except UnsafeUIError as exc:
            return ProbeResult(ProbeStatus.UNSAFE_UI, detail=str(exc))
        except ObservationError as exc:
            return ProbeResult(ProbeStatus.UNVERIFIED, detail=str(exc))
