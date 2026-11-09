from __future__ import annotations

import sys
import subprocess
import struct
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from campaign.adb_selection_adapter import AdbSelectionAdapter, RosterBinding
from campaign.device_observer import DeviceObserver, SelectionActionResult, UnsafeDeviceError
from campaign.recipient_probe import ProbeStatus, RecipientSelectionProbe, RecipientTarget

SERIAL = "emulator-5554"
PROBE_PACKAGE = "com.pacer.test.probe"
PICKER_PACKAGE = "com.android.contacts"
FINGERPRINT = "aosp/test-image:1"
PROBE_VERSION = "1.0.0:1"
PICKER_VERSION = "1.0.1:1"
PROBE_CERT = "a" * 64
PICKER_CERT = "b" * 64
KEY = "+12085550101"
SNAPSHOT = "synthetic-snapshot"
PNG_1080_1920 = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                 + struct.pack(">II", 1080, 1920) + bytes((8, 2, 0, 0, 0)))
PNG_100_100 = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
               + struct.pack(">II", 100, 100) + bytes((8, 2, 0, 0, 0)))

PICKER_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0"><node index="0" text="" resource-id="" class="android.widget.FrameLayout"
package="com.android.contacts" content-desc="" checkable="false" checked="false" clickable="false"
enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false"
password="false" selected="false" bounds="[0,0][1080,1920]">
<node index="0" text="Synthetic Contact 001" resource-id="com.android.contacts:id/contact_row"
class="android.widget.LinearLayout" package="com.android.contacts" content-desc="" checkable="false"
checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false"
long-clickable="false" password="false" selected="false" bounds="[0,100][1080,260]">
<node index="0" text="Synthetic Contact 001" resource-id="com.android.contacts:id/name"
class="android.widget.TextView" package="com.android.contacts" content-desc="" clickable="false"
enabled="true" bounds="[20,110][700,170]" />
<node index="1" text="+12085550101" resource-id="com.android.contacts:id/number"
class="android.widget.TextView" package="com.android.contacts" content-desc="" clickable="false"
enabled="true" bounds="[20,180][700,230]" />
</node></node></hierarchy>'''

PROBE_XML = b'''<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0"><node index="0" text="" resource-id="" class="android.widget.LinearLayout"
package="com.pacer.test.probe" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,1920]">
<node index="0" text="BOUND CANDIDATE\ncontact_row_id=42\ndisplay_name=Synthetic Contact 001\nrecipient_key=+12085550101"
resource-id="" class="android.widget.TextView" package="com.pacer.test.probe"
content-desc="selection_readback: BOUND CANDIDATE" clickable="false" enabled="true" bounds="[20,200][1000,700]" />
</node></hierarchy>'''


class FakeProcessRunner:
    def __init__(self, *, extra_serial=False, requested_permissions="", picker_cert=PICKER_CERT,
                 returned_extra_surface=False, omit_empty_requested_permissions=False):
        self.calls = []
        self.state = "picker"
        self.extra_serial = extra_serial
        self.requested_permissions = requested_permissions
        self.picker_cert = picker_cert
        self.returned_extra_surface = returned_extra_surface
        self.omit_empty_requested_permissions = omit_empty_requested_permissions
        self.layout_changes_before_tap = False
        self.picker_hierarchy_reads = 0
        self.selected_name = "Synthetic Contact 001"
        self.screenshot_bytes = PNG_1080_1920

    def __call__(self, args, *, capture_output, text, timeout, check, shell):
        self.calls.append((list(map(str, args)), capture_output, text, timeout, check, shell))
        joined = " ".join(map(str, args))
        stdout = ""
        if args[-2:] == ["devices", "-l"]:
            stdout = "List of devices attached\n" + f"{SERIAL} device product:aosp model:CI\n"
            if self.extra_serial:
                stdout += "unauthorized-1 unauthorized\n"
        elif "getprop ro.build.fingerprint" in joined:
            stdout = FINGERPRINT + "\n"
        elif "dumpsys package " + PROBE_PACKAGE in joined:
            requested_section = (
                "" if self.omit_empty_requested_permissions and not self.requested_permissions
                else f" requested permissions:\n{self.requested_permissions}\n"
            )
            stdout = (
                f"Package [{PROBE_PACKAGE}]\n versionName={PROBE_VERSION.split(':')[0]}\n"
                f"versionCode={PROBE_VERSION.split(':')[1]} minSdk=23\n{requested_section}"
                " install permissions:\n"
            )
        elif "dumpsys package " + PICKER_PACKAGE in joined:
            stdout = f"Package [{PICKER_PACKAGE}]\n versionName={PICKER_VERSION.split(':')[0]}\n versionCode={PICKER_VERSION.split(':')[1]} minSdk=23\n requested permissions:\n install permissions:\n"
        elif "pm path " in joined:
            package = args[-1]
            stdout = f"package:/data/app/{package}/base.apk\n"
        elif "adb" in str(args[0]) and "pull" in args:
            Path(args[-1]).write_bytes(b"fake-apk")
            stdout = "1 file pulled\n"
        elif "apksigner" in str(args[0]):
            cert = self.picker_cert if PICKER_PACKAGE in joined else PROBE_CERT
            stdout = f"Signer #1 certificate SHA-256 digest: {cert}\n"
        elif "pm list packages" in joined:
            stdout = ""
        elif "dumpsys activity activities" in joined:
            package = PICKER_PACKAGE if self.state == "picker" else PROBE_PACKAGE
            stdout = f"topResumedActivity=ActivityRecord{{abc u0 {package}/.MainActivity t1}}\n"
        elif "pidof " in joined:
            stdout = "1234\n"
        elif "uiautomator dump" in joined:
            pass
        elif "exec-out" in args and "cat" in args:
            if self.state == "picker":
                self.picker_hierarchy_reads += 1
                if self.layout_changes_before_tap and self.picker_hierarchy_reads > 1:
                    stdout = PICKER_XML.decode().replace("[0,100][1080,260]", "[0,120][1080,300]")
                else:
                    stdout = PICKER_XML.decode()
            else:
                stdout = PROBE_XML.decode().replace("Synthetic Contact 001", self.selected_name)
                if self.returned_extra_surface:
                    stdout = stdout.replace(
                        "</hierarchy>",
                        '<node package="com.android.contacts" text="stale picker surface" /></hierarchy>',
                    )
        elif "exec-out" in args and "screencap" in args:
            stdout = self.screenshot_bytes if not text else ""
        elif "input tap" in joined:
            self.state = "probe"
        else:
            raise AssertionError(f"unexpected subprocess command in test: {joined}")
        if not text and isinstance(stdout, str):
            stdout = stdout.encode()
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


class AdbSelectionAdapterTests(unittest.TestCase):
    def make_adapter(self, runner, *, bindings=None):
        return AdbSelectionAdapter(
            "C:/Android/platform-tools/adb",
            "C:/Android/build-tools/35.0.0/apksigner",
            picker_package=PICKER_PACKAGE,
            bindings=bindings or (RosterBinding(SNAPSHOT, 0, KEY, "Synthetic Contact 001"),),
            process_runner=runner,
            timeout_seconds=3,
        )

    def make_probe(self, adapter):
        observer = DeviceObserver(
            adapter,
            expected_serial=SERIAL,
            expected_fingerprint=FINGERPRINT,
            expected_package=PROBE_PACKAGE,
            expected_package_version=PROBE_VERSION,
            expected_certificate_sha256=PROBE_CERT,
            expected_picker_package=PICKER_PACKAGE,
            expected_picker_package_version=PICKER_VERSION,
            expected_picker_certificate_sha256=PICKER_CERT,
            sleep_fn=lambda _: None,
        )
        return RecipientSelectionProbe(observer)

    def test_empty_requested_permissions_section_may_be_omitted_by_package_dump(self):
        runner = FakeProcessRunner(omit_empty_requested_permissions=True)
        identity = self.make_adapter(runner).inspect_device(SERIAL)
        self.assertEqual(identity.declared_permissions, frozenset())

    def test_timeout_cannot_be_configured_as_unbounded(self):
        with self.assertRaises(ValueError):
            AdbSelectionAdapter(
                "adb", "apksigner", picker_package=PICKER_PACKAGE,
                bindings=(RosterBinding(SNAPSHOT, 0, KEY, "Synthetic Contact 001"),),
                timeout_seconds=31,
            )

    def test_package_version_attestation_includes_version_code(self):
        adapter = self.make_adapter(FakeProcessRunner())
        identity = adapter.inspect_device(SERIAL)
        self.assertEqual(identity.package_version, PROBE_VERSION)

    def test_real_adapter_port_runs_single_contact_selection_readback(self):
        runner = FakeProcessRunner()
        adapter = self.make_adapter(runner)
        result = self.make_probe(adapter).select(
            RecipientTarget(SNAPSHOT, 0, KEY)
        )
        self.assertEqual(result.status, ProbeStatus.BOUND)
        self.assertEqual(result.evidence.device_serial, SERIAL)
        self.assertEqual(result.evidence.recipient_key, KEY)
        self.assertEqual(sum("input tap" in " ".join(c[0]) for c in runner.calls), 1)
        self.assertTrue(all(call[-1] is False for call in runner.calls))
        for argv, *_ in runner.calls:
            if argv[0].endswith("adb") and argv[-2:] != ["devices", "-l"]:
                self.assertEqual(argv[1:3], ["-s", SERIAL])
        self.assertFalse(hasattr(adapter, "dispatch"))

    def test_additional_or_unauthorized_serial_blocks_every_device_command(self):
        runner = FakeProcessRunner(extra_serial=True)
        adapter = self.make_adapter(runner)
        with self.assertRaises(UnsafeDeviceError):
            adapter.inspect_device(SERIAL)
        self.assertFalse(any("shell" in call[0] for call in runner.calls))

    def test_zero_permission_violation_is_reported_to_observer_and_blocks_tap(self):
        runner = FakeProcessRunner(requested_permissions="  android.permission.READ_CONTACTS\n")
        adapter = self.make_adapter(runner)
        result = self.make_probe(adapter).select(RecipientTarget(SNAPSHOT, 0, KEY))
        self.assertEqual(result.status, ProbeStatus.UNSAFE_DEVICE)
        self.assertFalse(any("input tap" in " ".join(c[0]) for c in runner.calls))

    def test_picker_certificate_mismatch_blocks_before_screen_capture(self):
        runner = FakeProcessRunner(picker_cert="f" * 64)
        adapter = self.make_adapter(runner)
        result = self.make_probe(adapter).select(RecipientTarget(SNAPSHOT, 0, KEY))
        self.assertEqual(result.status, ProbeStatus.UNSAFE_DEVICE)
        self.assertFalse(any("uiautomator dump" in " ".join(c[0]) for c in runner.calls))

    def test_capture_exposes_non_control_picker_text_for_diagnostics(self):
        runner = FakeProcessRunner()
        adapter = self.make_adapter(runner)
        frame = adapter.capture(SERIAL)
        self.assertIn("Synthetic Contact 001", frame.visible_text)

    def test_source_capture_binding_rejects_stale_candidate_without_tap(self):
        runner = FakeProcessRunner()
        adapter = self.make_adapter(runner)
        candidates = adapter.capture(SERIAL).candidates
        self.assertEqual(len(candidates), 1)
        result = adapter.select_candidate(SERIAL, "stale-capture", candidates[0].candidate_id)
        self.assertIs(result, SelectionActionResult.STALE_CAPTURE)
        self.assertFalse(any("input tap" in " ".join(c[0]) for c in runner.calls))

    def test_layout_change_between_capture_and_click_does_not_tap(self):
        runner = FakeProcessRunner()
        adapter = self.make_adapter(runner)
        adapter.inspect_device(SERIAL)
        frame = adapter.capture(SERIAL)
        runner.layout_changes_before_tap = True
        result = adapter.select_candidate(SERIAL, frame.capture_id, frame.candidates[0].candidate_id)
        self.assertIs(result, SelectionActionResult.LAYOUT_CHANGED)
        self.assertFalse(any("input tap" in " ".join(c[0]) for c in runner.calls))

    def test_out_of_screenshot_candidate_bounds_are_never_tapped(self):
        runner = FakeProcessRunner()
        runner.screenshot_bytes = PNG_100_100
        adapter = self.make_adapter(runner)
        result = self.make_probe(adapter).select(RecipientTarget(SNAPSHOT, 0, KEY))
        self.assertEqual(result.status, ProbeStatus.NOT_FOUND)
        self.assertFalse(any("input tap" in " ".join(c[0]) for c in runner.calls))

    def test_picker_certificate_change_between_observation_and_tap_is_rejected(self):
        runner = FakeProcessRunner()
        adapter = self.make_adapter(runner)
        identity = adapter.inspect_device(SERIAL)
        self.assertEqual(identity.foreground_package, PICKER_PACKAGE)
        frame = adapter.capture(SERIAL)
        runner.picker_cert = "f" * 64
        result = adapter.select_candidate(SERIAL, frame.capture_id, frame.candidates[0].candidate_id)
        self.assertIs(result, SelectionActionResult.REJECTED)
        self.assertFalse(any("input tap" in " ".join(c[0]) for c in runner.calls))

    def test_post_selection_display_name_mismatch_is_unverified(self):
        runner = FakeProcessRunner()
        runner.selected_name = "Different Synthetic Contact"
        adapter = self.make_adapter(runner)
        result = self.make_probe(adapter).select(RecipientTarget(SNAPSHOT, 0, KEY))
        self.assertEqual(result.status, ProbeStatus.UNSAFE_UI)
        self.assertIsNone(result.evidence)

    def test_extra_surface_after_picker_return_is_rejected(self):
        runner = FakeProcessRunner(returned_extra_surface=True)
        adapter = self.make_adapter(runner)
        result = self.make_probe(adapter).select(RecipientTarget(SNAPSHOT, 0, KEY))
        self.assertEqual(result.status, ProbeStatus.UNSAFE_UI)
        self.assertEqual(sum("input tap" in " ".join(c[0]) for c in runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
