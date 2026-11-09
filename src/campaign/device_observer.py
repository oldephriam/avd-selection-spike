from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import re
import time
from typing import Protocol


FORBIDDEN_CONTROL_LABELS = re.compile(r"\b(send|compose|message body|sms|rcs)\b", re.IGNORECASE)


class ObservationError(RuntimeError):
    pass


class UnsafeDeviceError(ObservationError):
    pass


class UnsafeUIError(ObservationError):
    pass


class CapturePhase(str, Enum):
    PROBE = "PROBE"
    PICKER_ACTIVE = "PICKER_ACTIVE"
    PROBE_RETURNED = "PROBE_RETURNED"


class SelectionActionResult(str, Enum):
    SELECTED = "SELECTED"
    STALE_CAPTURE = "STALE_CAPTURE"
    LAYOUT_CHANGED = "LAYOUT_CHANGED"
    SESSION_CHANGED = "SESSION_CHANGED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    serial: str
    fingerprint: str
    package_name: str
    package_version: str
    certificate_sha256: str
    declared_permissions: frozenset[str]
    granted_permissions: frozenset[str]
    foreground_package: str
    foreground_package_version: str
    foreground_certificate_sha256: str
    seeder_installed: bool


@dataclass(frozen=True, slots=True)
class ContactCandidate:
    candidate_id: str
    roster_index: int
    recipient_key: str
    label: str
    snapshot_id: str = "snapshot-1"
    visible: bool = True


@dataclass(frozen=True, slots=True)
class DeviceFrame:
    serial: str
    capture_id: str
    session_id: str
    layout_signature: str
    captured_at: datetime
    candidates: tuple[ContactCandidate, ...]
    selected: ContactCandidate | None
    hierarchy_present: bool
    screenshot_sha256: str
    hierarchy_sha256: str
    send_surface_present: bool = False
    active_control_labels: tuple[str, ...] = ()
    visible_text: tuple[str, ...] = ()
    candidate_diagnostics: tuple[str, ...] = ()
    surface_packages: tuple[str, ...] = ()
    external_intent_leak: bool = False
    readback_visible: bool = False


class SelectionOnlyPort(Protocol):
    """Constrained surface; intentionally has no message/dispatch operation."""

    def list_serials(self) -> tuple[str, ...]: ...

    def inspect_device(self, serial: str) -> DeviceIdentity: ...

    def capture(self, serial: str) -> DeviceFrame: ...

    def select_candidate(
        self, serial: str, capture_id: str, candidate_id: str
    ) -> SelectionActionResult: ...


class DeviceObserver:
    def __init__(self, port: SelectionOnlyPort, *, expected_serial: str,
                 expected_fingerprint: str, expected_package: str,
                 expected_package_version: str, expected_certificate_sha256: str,
                 expected_picker_package: str, expected_picker_package_version: str,
                 expected_picker_certificate_sha256: str,
                 max_polls: int = 3, poll_interval_seconds: float = 0.5,
                 sleep_fn=time.sleep):
        if max_polls < 1 or max_polls > 10:
            raise ValueError("max_polls must be between 1 and 10")
        if poll_interval_seconds < 0 or poll_interval_seconds > 5:
            raise ValueError("poll interval must be between 0 and 5 seconds")
        if not expected_picker_package or expected_picker_package == expected_package:
            raise ValueError("the picker must be an explicitly pinned package distinct from the probe app")
        for digest in (expected_certificate_sha256, expected_picker_certificate_sha256):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
                raise ValueError("expected package certificate must be a SHA-256 hex digest")
        self.port = port
        self.expected_serial = expected_serial
        self.expected_fingerprint = expected_fingerprint
        self.expected_package = expected_package
        self.expected_package_version = expected_package_version
        self.expected_certificate_sha256 = expected_certificate_sha256.lower()
        self.expected_picker_package = expected_picker_package
        self.expected_picker_package_version = expected_picker_package_version
        self.expected_picker_certificate_sha256 = expected_picker_certificate_sha256.lower()
        self.max_polls = max_polls
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep_fn = sleep_fn

    def capture(self, phase: CapturePhase = CapturePhase.PROBE) -> DeviceFrame:
        serials = self.port.list_serials()
        if serials != (self.expected_serial,):
            raise UnsafeDeviceError("ADB device set differs from the single approved serial")
        identity = self.port.inspect_device(self.expected_serial)
        if identity.serial != self.expected_serial:
            raise UnsafeDeviceError("device identity serial mismatch")
        if identity.fingerprint != self.expected_fingerprint:
            raise UnsafeDeviceError("device image fingerprint mismatch")
        if (identity.package_name != self.expected_package
                or identity.package_version != self.expected_package_version
                or identity.certificate_sha256 != self.expected_certificate_sha256):
            raise UnsafeDeviceError("probe package identity, version, or certificate mismatch")
        if identity.declared_permissions or identity.granted_permissions:
            raise UnsafeDeviceError("zero-permission probe has a declared or granted permission")
        if identity.seeder_installed:
            raise UnsafeDeviceError("temporary contact seeder must be removed before probing")
        if phase is CapturePhase.PICKER_ACTIVE:
            expected_foreground = self.expected_picker_package
            expected_foreground_version = self.expected_picker_package_version
            expected_foreground_certificate = self.expected_picker_certificate_sha256
        else:
            expected_foreground = self.expected_package
            expected_foreground_version = self.expected_package_version
            expected_foreground_certificate = self.expected_certificate_sha256
        if (identity.foreground_package != expected_foreground
                or identity.foreground_package_version != expected_foreground_version
                or identity.foreground_certificate_sha256 != expected_foreground_certificate):
            raise UnsafeDeviceError("foreground package identity is not allowed for this probe phase")
        frame = self.port.capture(self.expected_serial)
        if frame.serial != self.expected_serial:
            raise UnsafeDeviceError("capture came from an unapproved device")
        if not frame.capture_id or not frame.session_id or not frame.layout_signature:
            raise ObservationError("capture/session/layout identity is incomplete")
        if frame.captured_at.tzinfo is None or frame.captured_at > datetime.now(timezone.utc) + timedelta(seconds=5):
            raise ObservationError("capture timestamp is invalid or unexpectedly in the future")
        if len(frame.screenshot_sha256) != 64:
            raise ObservationError("screenshot evidence hash is missing or malformed")
        if not frame.hierarchy_present or len(frame.hierarchy_sha256) != 64:
            raise ObservationError("accessibility hierarchy is missing or malformed")
        if frame.surface_packages != (expected_foreground,):
            raise UnsafeUIError("unexpected or additional visible application surface")
        if frame.external_intent_leak:
            raise UnsafeUIError("unexpected external activity or intent surface detected")
        if phase is CapturePhase.PROBE_RETURNED and not frame.readback_visible:
            raise UnsafeUIError("probe readback surface did not return after picker selection")
        if frame.send_surface_present or any(FORBIDDEN_CONTROL_LABELS.search(label)
                                             for label in frame.active_control_labels):
            raise UnsafeUIError("unexpected messaging/send surface detected")
        return frame
