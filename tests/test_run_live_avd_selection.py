from __future__ import annotations

from pathlib import Path
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
