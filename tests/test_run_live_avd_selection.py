from __future__ import annotations

from pathlib import Path
import base64
import re
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import run_live_avd_selection as live


class LiveAvdCommandTests(unittest.TestCase):
    def test_selection_diagnostics_show_readback_and_matching_row_targets_safely(self) -> None:
        avd = live.AVD("adb")
        avd.hierarchy = lambda: (
            b'<hierarchy><node class="android.widget.ListView" clickable="false" '
            b'enabled="true" bounds="[0,10][400,800]"><node class="android.view.ViewGroup" '
            b'clickable="false" enabled="true" bounds="[0,20][400,100]">'
            b'<node text="Pacer Probe Fixture 001" clickable="false" enabled="true" '
            b'bounds="[10,30][300,70]"/></node></node><node content-desc="selection_readback: '
            b'BOUND CANDIDATE recipient_key=+12055550100"/></hierarchy>'
        )
        avd.foreground = lambda: live.PROBE

        diagnostic = avd.selection_diagnostics()

        self.assertIn("foreground=com.pacer.test.probe", diagnostic)
        self.assertIn("readback=True", diagnostic)
        self.assertIn("android.view.ViewGroup", diagnostic)
        self.assertIn("clickable=false", diagnostic)
        self.assertNotIn("12055550100", diagnostic)

    def test_button_bounds_reports_observed_control_when_label_is_absent(self) -> None:
        hierarchy = (
            b'<hierarchy><node text="Permission required" clickable="true" '
            b'enabled="true" visible-to-user="true" bounds="[10,20][210,80]"/></hierarchy>'
        )

        with self.assertRaisesRegex(RuntimeError, "Permission required"):
            live.AVD("adb")._button_bounds(hierarchy, "Seed synthetic contact")

    def test_wait_for_text_timeout_includes_observed_status_text(self) -> None:
        avd = live.AVD("adb")
        hierarchy = b'<hierarchy><node text="FAILED: synthetic provider insert"/></hierarchy>'
        with patch.object(avd, "hierarchy", return_value=hierarchy), \
                patch.object(live.time, "monotonic", side_effect=[0.0, 0.5, 2.0]), \
                patch.object(live.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "FAILED: synthetic provider insert"):
                avd.wait_for_text("SEEDED", timeout_seconds=1)

    def test_provider_visibility_check_uses_contacts_phone_view(self) -> None:
        class FakeAvd:
            def __init__(self):
                self.commands = []

            def command(self, *args, **kwargs):
                self.commands.append((args, kwargs))
                uri = args[args.index("--uri") + 1]
                if uri.endswith("/data/phones"):
                    return ("Row: 0 _id=9 raw_contact_id=4 contact_id=7 "
                            "mimetype=vnd.android.cursor.item/phone_v2 "
                            "display_name=Pacer Probe Fixture 001 data1=+12025550100")
                if uri.endswith("/raw_contacts"):
                    return ("Row: 0 _id=4 account_name=NULL account_type=NULL "
                            "contact_id=7 aggregation_mode=0 deleted=0")
                if uri.endswith("/contacts"):
                    return "Row: 0 _id=7 display_name=Pacer Probe Fixture 001"
                raise AssertionError(f"unexpected provider uri: {uri}")

        avd = FakeAvd()
        self.assertTrue(live.fixture_visible_in_provider(avd))
        self.assertEqual(len(avd.commands), 3)
        uris = [args[args.index("--uri") + 1] for args, _ in avd.commands]
        self.assertEqual(uris, [
            "content://com.android.contacts/data/phones",
            "content://com.android.contacts/raw_contacts",
            "content://com.android.contacts/contacts",
        ])

    def test_instrumentation_root_requires_debuggable_image_and_verified_root_uid(self) -> None:
        class FakeAvd:
            def __init__(self):
                self.commands = []

            def command(self, *args, **kwargs):
                self.commands.append(args)
                if args == ("shell", "getprop", "ro.debuggable"):
                    return "1"
                if args == ("root",):
                    return "restarting adbd as root"
                if args == ("shell", "id", "-u"):
                    return "0"
                return ""

        avd = FakeAvd()
        live.enable_root_adb_for_instrumentation(avd)
        self.assertIn(("root",), avd.commands)
        self.assertIn(("wait-for-device",), avd.commands)
        self.assertEqual(avd.commands[-1], ("shell", "id", "-u"))

    def test_instrumentation_root_is_refused_on_non_debuggable_image(self) -> None:
        class FakeAvd:
            def __init__(self):
                self.commands = []

            def command(self, *args, **kwargs):
                self.commands.append(args)
                return "0" if args == ("shell", "getprop", "ro.debuggable") else ""

        avd = FakeAvd()
        with self.assertRaisesRegex(RuntimeError, "debuggable AOSP image"):
            live.enable_root_adb_for_instrumentation(avd)
        self.assertNotIn(("root",), avd.commands)

    def test_command_can_capture_nonzero_instrumentation_output(self) -> None:
        avd = live.AVD("adb")
        with patch.object(live, "run", return_value="usage from am") as run_command:
            output = avd.command("shell", "am", "instrument", "-w", check=False)
        self.assertEqual(output, "usage from am")
        self.assertFalse(run_command.call_args.kwargs["check"])

    def test_fixture_phone_data_id_is_bound_to_unique_phone_provider_row(self) -> None:
        binding = live.fixture_binding()
        source = (live.ROOT / "android/seeder/src/main/java/com/pacer/test/seeder/SeederActivity.java").read_text(encoding="utf-8")
        phone = re.search(
            r'FIXTURE_RECIPIENT_KEY[ ]*=[ ]*"([^"]+)"', source
        ).group(1)

        class FakeAvd:
            def command(self, *args, **kwargs):
                return (
                    f"Row: 0 _id=900 raw_contact_id=4 contact_id=7 "
                    f"mimetype=vnd.android.cursor.item/phone_v2 "
                    f"display_name={binding.display_name} data1={phone}"
                )

        self.assertEqual(live.fixture_phone_data_id(FakeAvd()), "900")

    def test_readback_status_classification_omits_fixture_values(self) -> None:
        self.assertEqual(live.readback_status("UNVERIFIED: picker returned RESULT_CANCELED"), "result_canceled")
        self.assertEqual(live.readback_status("UNVERIFIED: picker did not grant readable selection evidence"), "permission_denied")
        self.assertEqual(live.readback_status("UNVERIFIED: selected row could not be read"), "uri_unreadable")
        self.assertEqual(live.readback_status("RESULT_OK: BOUND CANDIDATE"), "result_ok")
        self.assertEqual(live.readback_status("no selection data"), "status_missing")

    def test_readback_mismatch_reports_only_field_labels(self) -> None:
        expected = (
            ("status", "RESULT_OK: BOUND CANDIDATE"),
            ("contact_row_id", "contact_row_id=42"),
            ("display_name", "display_name=fixture contact"),
        )
        missing = live.missing_readback_fields(
            "RESULT_OK: BOUND CANDIDATE\ncontact_row_id=99\ndisplay_name=fixture contact", expected,
        )
        self.assertEqual(missing, ("contact_row_id",))

    def test_instrumentation_pass_accepts_junit_success_without_optional_am_result_marker(self) -> None:
        output = "com.pacer.test.pickerharness.PhonePickerItemClickTest:.\nTime: 0.02\n\nOK (1 test)\n"
        self.assertTrue(live.instrumentation_test_passed(output))

    def test_instrumentation_pass_rejects_failures_and_unexpected_am_result_codes(self) -> None:
        self.assertFalse(live.instrumentation_test_passed("OK (1 test)\nFAILURES!!!\n"))
        self.assertFalse(live.instrumentation_test_passed("OK (0 tests)\n"))
        self.assertFalse(live.instrumentation_test_passed("OK (1 test)\nINSTRUMENTATION_CODE: 0\n"))

    def test_instrumentation_failure_output_is_sanitized_and_bounded(self) -> None:
        binding = live.fixture_binding()
        phone = live.fixture_phone_number()
        encoded_phone = live.encode_instrumentation_value(phone)
        raw_output = (
            f"ERROR: invalid invocation for {binding.display_name} {binding.recipient_key} "
            f"fixture_data_id=778899 phone={phone} fixture_phone_b64={encoded_phone}\n"
            + "usage\n" * 50
        )
        safe = live.sanitized_instrumentation_output(raw_output, binding, "778899")
        self.assertTrue(safe.startswith("ERROR: invalid invocation"))
        self.assertNotIn(binding.display_name, safe)
        self.assertNotIn(binding.recipient_key, safe)
        self.assertNotIn("778899", safe)
        self.assertNotIn(phone, safe)
        self.assertNotIn(encoded_phone, safe)
        self.assertLessEqual(len(safe), 8000)

    def test_instrumentation_display_name_argument_is_shell_safe_and_lossless(self) -> None:
        binding = live.fixture_binding()
        encoded = live.encode_instrumentation_display_name(binding.display_name)
        self.assertNotIn(" ", encoded)
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), binding.display_name)

    def test_instrumentation_phone_argument_is_shell_safe_and_lossless(self) -> None:
        phone = live.fixture_phone_number()
        encoded = live.encode_instrumentation_value(phone)
        self.assertNotIn(phone, encoded)
        self.assertNotIn("+", encoded)
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), phone)

    def test_phone_row_alone_is_not_enough_without_aggregated_contact(self) -> None:
        class FakeAvd:
            def command(self, *args, **kwargs):
                uri = args[args.index("--uri") + 1]
                if uri.endswith("/data/phones"):
                    return ("Row: 0 raw_contact_id=4 contact_id=7 "
                            "mimetype=vnd.android.cursor.item/phone_v2 "
                            "display_name=Pacer Probe Fixture 001 data1=+12025550100")
                if uri.endswith("/raw_contacts"):
                    return ("Row: 0 _id=4 account_name=NULL account_type=NULL "
                            "contact_id=7 aggregation_mode=0")
                return "No result found."

        self.assertFalse(live.fixture_visible_in_provider(FakeAvd()))

    def test_command_forwards_timeout_to_subprocess_runner(self) -> None:
        avd = live.AVD("adb")
        with patch.object(live, "run", return_value="ok") as run:
            result = avd.command("install", "-r", "probe.apk", timeout=120)

        self.assertEqual(result, "ok")
        run.assert_called_once_with(
            ["adb", "-s", live.SERIAL, "install", "-r", "probe.apk"],
            text=True,
            timeout=120,
            check=True,
        )
    def test_button_bounds_recognizes_android_all_caps_button_text(self) -> None:
        hierarchy = (
            b'<hierarchy><node text="SEED SYNTHETIC CONTACT" clickable="true" '
            b'enabled="true" visible-to-user="true" bounds="[10,20][210,80]"/></hierarchy>'
        )

        bounds = live.AVD("adb")._button_bounds(hierarchy, "Seed synthetic contact")

        self.assertEqual(bounds, (10, 20, 210, 80))


if __name__ == "__main__":
    unittest.main()
