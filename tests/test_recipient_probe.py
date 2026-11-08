from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from campaign.device_observer import (
    CapturePhase,
    ContactCandidate,
    DeviceFrame,
    DeviceIdentity,
    DeviceObserver,
    ObservationError,
    SelectionActionResult,
)
from campaign.recipient_probe import ProbeStatus, RecipientSelectionProbe, RecipientTarget


SERIAL = "emulator-5554"
PACKAGE = "test.recipientpicker.sandbox"
PICKER_PACKAGE = "com.android.contacts"
FINGERPRINT = "aosp/test-image:1"
PACKAGE_VERSION = "1.0-test"
PICKER_VERSION = "1.0-aosp-pin"
PACKAGE_CERT = "c" * 64
PICKER_CERT = "d" * 64
KEY_A = "+12085550101"
KEY_B = "+12085550102"


def candidate(candidate_id="c1", recipient_key=KEY_A, roster_index=0, label="Synthetic Alice"):
    return ContactCandidate(candidate_id, roster_index, recipient_key, label)


def frame(*, capture_id="cap-1", session_id="session-1", layout="picker-v1", candidates=(), selected=None,
          hierarchy=True, send_surface=False, control_labels=(), surface_packages=(PICKER_PACKAGE,),
          external_intent_leak=False, readback_visible=False):
    return DeviceFrame(
        serial=SERIAL,
        capture_id=capture_id,
        session_id=session_id,
        layout_signature=layout,
        captured_at=datetime.now(timezone.utc),
        candidates=tuple(candidates),
        selected=selected,
        hierarchy_present=hierarchy,
        screenshot_sha256="a" * 64,
        hierarchy_sha256="b" * 64 if hierarchy else "",
        send_surface_present=send_surface,
        active_control_labels=tuple(control_labels),
        surface_packages=tuple(surface_packages),
        external_intent_leak=external_intent_leak,
        readback_visible=readback_visible,
    )


class FakeSelectionPort:
    def __init__(self, frames=None, *, serials=(SERIAL,), identity=None, selected_key=None,
                 layout_change_on_select=False, session_change_on_select=False,
                 reject_selection=False, extra_surface_on_return=False, intent_leak_on_return=False):
        self.frames = list(frames or [frame(candidates=[candidate()])])
        self.serials = tuple(serials)
        self.identity = identity or DeviceIdentity(
            serial=SERIAL,
            fingerprint=FINGERPRINT,
            package_name=PACKAGE,
            package_version=PACKAGE_VERSION,
            certificate_sha256=PACKAGE_CERT,
            declared_permissions=frozenset(),
            granted_permissions=frozenset(),
            foreground_package=PICKER_PACKAGE,
            foreground_package_version=PICKER_VERSION,
            foreground_certificate_sha256=PICKER_CERT,
            seeder_installed=False,
        )
        self.selected_key = selected_key
        self.layout_change_on_select = layout_change_on_select
        self.session_change_on_select = session_change_on_select
        self.reject_selection = reject_selection
        self.extra_surface_on_return = extra_surface_on_return
        self.intent_leak_on_return = intent_leak_on_return
        self.select_calls = []
        self.tap_calls = []
        self.capture_calls = 0
        self.waits = []
        self.return_to_picker = False

    def list_serials(self):
        return self.serials

    def inspect_device(self, serial):
        return self.identity

    def capture(self, serial):
        self.capture_calls += 1
        if len(self.frames) > 1:
            return self.frames.pop(0)
        return self.frames[0]

    def select_candidate(self, serial, capture_id, candidate_id):
        self.select_calls.append((serial, capture_id, candidate_id))
        if self.reject_selection:
            return SelectionActionResult.REJECTED
        if self.layout_change_on_select:
            return SelectionActionResult.LAYOUT_CHANGED
        if self.session_change_on_select:
            return SelectionActionResult.SESSION_CHANGED
        current = self.frames[0]
        chosen = next((c for c in current.candidates if c.candidate_id == candidate_id), None)
        if chosen is None:
            return SelectionActionResult.REJECTED
        self.tap_calls.append((serial, capture_id, candidate_id))
        chosen = replace(chosen, recipient_key=self.selected_key or chosen.recipient_key)
        updated = replace(
            current,
            capture_id=f"{capture_id}-selected",
            session_id="session-restarted" if self.session_change_on_select else current.session_id,
            layout_signature="changed-layout" if self.layout_change_on_select else current.layout_signature,
            selected=chosen,
            surface_packages=(PACKAGE, PICKER_PACKAGE) if self.extra_surface_on_return else (PACKAGE,),
            external_intent_leak=self.intent_leak_on_return,
            readback_visible=True,
        )
        self.frames[:] = [updated]
        self.identity = replace(
            self.identity,
            foreground_package=(PICKER_PACKAGE if self.return_to_picker else PACKAGE),
            foreground_package_version=(PICKER_VERSION if self.return_to_picker else PACKAGE_VERSION),
            foreground_certificate_sha256=(PICKER_CERT if self.return_to_picker else PACKAGE_CERT),
        )
        return SelectionActionResult.SELECTED

    def wait(self, seconds):
        self.waits.append(seconds)


class RecipientProbeTests(unittest.TestCase):
    def target(self):
        return RecipientTarget(snapshot_id="snapshot-1", roster_index=0, recipient_key=KEY_A)

    def probe(self, port, *, max_polls=3):
        observer = DeviceObserver(
            port,
            expected_serial=SERIAL,
            expected_fingerprint=FINGERPRINT,
            expected_package=PACKAGE,
            expected_package_version=PACKAGE_VERSION,
            expected_certificate_sha256=PACKAGE_CERT,
            expected_picker_package=PICKER_PACKAGE,
            expected_picker_package_version=PICKER_VERSION,
            expected_picker_certificate_sha256=PICKER_CERT,
            max_polls=max_polls,
            poll_interval_seconds=0.5,
            sleep_fn=port.wait,
        )
        return RecipientSelectionProbe(observer)

    def test_probe_poll_budget_is_hard_bounded(self):
        with self.assertRaises(ValueError):
            self.probe(FakeSelectionPort(), max_polls=11)

    def test_duplicate_contacts_are_ambiguous_and_never_selected(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate("c1"), candidate("c2")])])
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.AMBIGUOUS)
        self.assertEqual(port.select_calls, [])

    def test_delayed_picker_result_is_passively_polled_then_uniquely_bound(self):
        port = FakeSelectionPort(frames=[
            frame(capture_id="loading-1", candidates=[]),
            frame(capture_id="loading-2", candidates=[]),
            frame(capture_id="ready", candidates=[candidate()]),
        ])
        result = self.probe(port, max_polls=3).select(self.target())
        self.assertEqual(result.status, ProbeStatus.BOUND)
        self.assertEqual(port.capture_calls, 4)  # 3 picker reads plus post-selection readback
        self.assertEqual(port.waits, [0.5, 0.5])
        self.assertEqual(len(port.select_calls), 1)
        self.assertEqual(result.evidence.recipient_key, KEY_A)

    def test_wrong_selected_recipient_fails_readback(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], selected_key=KEY_B)
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.MISMATCH)
        self.assertIsNone(result.evidence)

    def test_missing_hierarchy_fails_closed(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()], hierarchy=False)])
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.UNVERIFIED)
        self.assertEqual(port.select_calls, [])

    def test_picker_layout_change_before_tap_fails_closed(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], layout_change_on_select=True)
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.LAYOUT_CHANGED)
        self.assertIsNone(result.evidence)
        self.assertEqual(port.tap_calls, [])

    def test_process_restart_during_selection_fails_closed(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], session_change_on_select=True)
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.SESSION_CHANGED)
        self.assertIsNone(result.evidence)

    def test_multiple_or_unapproved_devices_are_rejected(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], serials=(SERIAL, "device-2"))
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.UNSAFE_DEVICE)
        self.assertEqual(port.capture_calls, 0)

    def test_forbidden_permission_or_send_surface_is_rejected(self):
        identity = DeviceIdentity(
            serial=SERIAL,
            fingerprint=FINGERPRINT,
            package_name=PACKAGE,
            package_version=PACKAGE_VERSION,
            certificate_sha256=PACKAGE_CERT,
            declared_permissions=frozenset({"android.permission.INTERNET"}),
            granted_permissions=frozenset(),
            foreground_package=PICKER_PACKAGE,
            foreground_package_version=PICKER_VERSION,
            foreground_certificate_sha256=PICKER_CERT,
            seeder_installed=False,
        )
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], identity=identity)
        result = self.probe(port).select(self.target())
        self.assertEqual(result.status, ProbeStatus.UNSAFE_DEVICE)
        self.assertEqual(port.select_calls, [])

        safe_port = FakeSelectionPort(frames=[frame(candidates=[candidate()], send_surface=True)])
        surface_result = self.probe(safe_port).select(self.target())
        self.assertEqual(surface_result.status, ProbeStatus.UNSAFE_UI)
        self.assertFalse(hasattr(safe_port, "dispatch"))
        self.assertEqual(safe_port.select_calls, [])

        labeled_port = FakeSelectionPort(frames=[frame(candidates=[candidate()], control_labels=("Send",))])
        self.assertEqual(self.probe(labeled_port).select(self.target()).status, ProbeStatus.UNSAFE_UI)
        self.assertEqual(labeled_port.select_calls, [])

        wrong_build = FakeSelectionPort(
            frames=[frame(candidates=[candidate()])],
            identity=replace(FakeSelectionPort().identity, certificate_sha256="e" * 64),
        )
        self.assertEqual(self.probe(wrong_build).select(self.target()).status, ProbeStatus.UNSAFE_DEVICE)
        self.assertEqual(wrong_build.capture_calls, 0)
        self.assertFalse(hasattr(self.probe(wrong_build), "dispatch"))

    def test_probe_rejects_any_declared_or_granted_permission(self):
        for field in ("declared_permissions", "granted_permissions"):
            for permission in ("android.permission.READ_CONTACTS", "android.permission.INTERNET"):
                identity = replace(FakeSelectionPort().identity, **{field: frozenset({permission})})
                port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], identity=identity)
                self.assertEqual(self.probe(port).select(self.target()).status, ProbeStatus.UNSAFE_DEVICE)
                self.assertEqual(port.capture_calls, 0)

    def test_temporary_seeder_must_be_absent_during_probe(self):
        identity = replace(FakeSelectionPort().identity, seeder_installed=True)
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], identity=identity)
        self.assertEqual(self.probe(port).select(self.target()).status, ProbeStatus.UNSAFE_DEVICE)
        self.assertEqual(port.capture_calls, 0)

    def test_picker_package_identity_is_pinned_separately_from_probe_app(self):
        identity = replace(FakeSelectionPort().identity, foreground_certificate_sha256="e" * 64)
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])], identity=identity)
        self.assertEqual(self.probe(port).select(self.target()).status, ProbeStatus.UNSAFE_DEVICE)
        self.assertEqual(port.capture_calls, 0)

    def test_return_to_probe_requires_single_probe_surface_and_no_intent_leak(self):
        extra_surface = FakeSelectionPort(
            frames=[frame(candidates=[candidate()])], extra_surface_on_return=True)
        self.assertEqual(self.probe(extra_surface).select(self.target()).status, ProbeStatus.UNSAFE_UI)

        intent_leak = FakeSelectionPort(
            frames=[frame(candidates=[candidate()])], intent_leak_on_return=True)
        self.assertEqual(self.probe(intent_leak).select(self.target()).status, ProbeStatus.UNSAFE_UI)

    def test_return_to_picker_instead_of_probe_is_rejected(self):
        port = FakeSelectionPort(frames=[frame(candidates=[candidate()])])
        port.return_to_picker = True
        self.assertEqual(self.probe(port).select(self.target()).status, ProbeStatus.UNSAFE_DEVICE)


if __name__ == "__main__":
    unittest.main()
