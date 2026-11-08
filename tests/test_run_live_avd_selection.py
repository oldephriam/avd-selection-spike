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


if __name__ == "__main__":
    unittest.main()
