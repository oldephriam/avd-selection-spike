from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pin_aosp_picker import (
    discover,
    main,
    missing_pin_variables,
    picker_intent_matches,
    pin_variable_values,
)


IDENTITY = {
    "buildFingerprint": "aosp/fingerprint",
    "apiLevel": "35",
    "cpuAbi": "x86_64",
    "pickerPackage": "com.android.contacts",
    "pickerVersionName": "2.0",
    "pickerVersionCode": "35",
    "pickerCertificateSha256": "a" * 64,
    "pickerIntentResolverOutput": "No activity found",
    "pickerIntentMatchesPackage": False,
}


class PinAospPickerTests(unittest.TestCase):
    def test_missing_repository_pins_trigger_discovery(self) -> None:
        values = {
            "AOSP_35_BUILD_FINGERPRINT": "",
            "AOSP_CONTACTS_VERSION_NAME": "2.0",
            "AOSP_CONTACTS_VERSION_CODE": "35",
            "AOSP_CONTACTS_CERT_SHA256": "abcd",
        }
        self.assertEqual(missing_pin_variables(values), ["AOSP_35_BUILD_FINGERPRINT"])

    def test_discovered_identity_exports_exact_repository_variable_names(self) -> None:
        identity = {
            "buildFingerprint": "aosp/fingerprint",
            "pickerVersionName": "2.0",
            "pickerVersionCode": "35",
            "pickerCertificateSha256": "a" * 64,
        }
        self.assertEqual(
            pin_variable_values(identity),
            {
                "AOSP_35_BUILD_FINGERPRINT": "aosp/fingerprint",
                "AOSP_CONTACTS_VERSION_NAME": "2.0",
                "AOSP_CONTACTS_VERSION_CODE": "35",
                "AOSP_CONTACTS_CERT_SHA256": "a" * 64,
            },
        )

    def test_picker_intent_resolution_is_reported_without_assuming_package(self) -> None:
        self.assertTrue(picker_intent_matches("com.android.contacts/.activities.PeopleActivity"))
        self.assertFalse(picker_intent_matches("No activity found"))
        self.assertFalse(picker_intent_matches("com.android.dialer/.PickerActivity"))

    def test_discovery_resolves_picker_with_provider_mime_type(self) -> None:
        uri = "content://com.android.contacts/data/phones"
        mime_type = "vnd.android.cursor.dir/phone_v2"
        device_info = "List of devices attached\nemulator-5554 device product:sdk_phone64_x86_64\n"
        signer = "Signer #1 certificate SHA-256 digest: " + "a" * 64
        adb_outputs = [
            "aosp/fingerprint", "35", "x86_64", "package:com.android.contacts",
            "Package [com.android.contacts] versionName=2.0 versionCode=35",
            "package:/data/app/com.android.contacts/base.apk", "", f"Result: {mime_type}\n",
            "com.android.contacts/.activities.ContactSelectionActivity", "package:com.android.contacts\n",
        ]
        with patch("scripts.pin_aosp_picker.adb", side_effect=adb_outputs) as adb_mock, \
                patch("scripts.pin_aosp_picker.run", side_effect=[device_info, signer]):
            identity = discover("adb", "apksigner")

        operations = [operation.args[1:] for operation in adb_mock.call_args_list]
        self.assertIn(("shell", "content", "gettype", "--uri", uri), operations)
        resolver_call = next(op for op in operations if "resolve-activity" in op)
        self.assertIn("-t", resolver_call)
        self.assertEqual(resolver_call[resolver_call.index("-t") + 1], mime_type)
        self.assertEqual(identity["phonePickerMimeType"], mime_type)
        self.assertTrue(identity["pickerIntentMatchesPackage"])

    def test_verify_with_missing_pins_discovers_and_writes_diagnostics_without_claiming_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "picker.json"
            github_output = Path(directory) / "github-output"
            args = ["pin_aosp_picker.py", "--adb", "adb", "--apksigner", "apksigner",
                    "--output", str(output_path), "--mode", "verify"]
            with patch("sys.argv", args), patch.dict(os.environ, {"GITHUB_OUTPUT": str(github_output)}, clear=True), \
                    patch("scripts.pin_aosp_picker.discover", return_value=IDENTITY), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = main()
            self.assertEqual(result, 0)
            self.assertIn("PIN_DISCOVERY_REQUIRED", stdout.getvalue())
            self.assertIn("AOSP_CONTACTS_CERT_SHA256=" + "a" * 64, stdout.getvalue())
            self.assertFalse(json.loads(output_path.read_text(encoding="utf-8"))["pickerIntentMatchesPackage"])
            self.assertIn("verified=false", github_output.read_text(encoding="utf-8"))
            self.assertIn("discovery_required=true", github_output.read_text(encoding="utf-8"))

    def test_verify_with_complete_pins_fails_closed_on_wrong_resolver_but_keeps_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "picker.json"
            github_output = Path(directory) / "github-output"
            env = {
                "AOSP_35_BUILD_FINGERPRINT": IDENTITY["buildFingerprint"],
                "AOSP_CONTACTS_VERSION_NAME": IDENTITY["pickerVersionName"],
                "AOSP_CONTACTS_VERSION_CODE": IDENTITY["pickerVersionCode"],
                "AOSP_CONTACTS_CERT_SHA256": IDENTITY["pickerCertificateSha256"],
                "GITHUB_OUTPUT": str(github_output),
            }
            args = ["pin_aosp_picker.py", "--adb", "adb", "--apksigner", "apksigner",
                    "--output", str(output_path), "--mode", "verify"]
            with patch("sys.argv", args), patch.dict(os.environ, env, clear=True), \
                    patch("scripts.pin_aosp_picker.discover", return_value=IDENTITY), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                result = main()
            self.assertEqual(result, 1)
            self.assertIn("phone contact-picker intent does not resolve", stdout.getvalue())
            self.assertTrue(output_path.is_file())
            self.assertIn("verified=false", github_output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
