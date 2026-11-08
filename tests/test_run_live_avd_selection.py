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
