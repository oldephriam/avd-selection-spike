from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "android"
ANDROID = "http://schemas.android.com/apk/res/android"


def attr(element: ET.Element, name: str) -> str | None:
    return element.get(f"{{{ANDROID}}}{name}")


class AndroidContractTests(unittest.TestCase):
    def manifest(self, module: str) -> ET.Element:
        path = ROOT / module / "src" / "main" / "AndroidManifest.xml"
        return ET.parse(path).getroot()

    def source(self, module: str, filename: str) -> str:
        path = ROOT / module / "src" / "main" / "java" / "com" / "pacer" / "test" / module / filename
        return path.read_text(encoding="utf-8")

    def test_probe_package_is_zero_permission_and_uses_only_launcher_entry(self) -> None:
        manifest = self.manifest("probe")
        build = (ROOT / "probe" / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('applicationId = "com.pacer.test.probe"', build)
        self.assertEqual(manifest.findall("uses-permission"), [])
        activities = manifest.findall("application/activity")
        self.assertEqual(len(activities), 1)
        self.assertEqual(attr(activities[0], "exported"), "true")
        actions = [node.get("{http://schemas.android.com/apk/res/android}name")
                   for node in activities[0].iter("action")]
        categories = [node.get("{http://schemas.android.com/apk/res/android}name")
                      for node in activities[0].iter("category")]
        self.assertEqual(actions, ["android.intent.action.MAIN"])
        self.assertEqual(categories, ["android.intent.category.LAUNCHER"])

    def test_probe_launches_one_phone_picker_and_exposes_selection_readback(self) -> None:
        source = self.source("probe", "ProbeActivity.java")
        self.assertEqual(source.count("Intent.ACTION_PICK"), 1)
        self.assertIn("ContactsContract.CommonDataKinds.Phone.CONTENT_URI", source)
        self.assertIn("onActivityResult", source)
        self.assertIn("RESULT_OK: BOUND CANDIDATE", source)
        self.assertIn("picker returned RESULT_CANCELED", source)
        self.assertIn("picker returned RESULT_OK without a URI", source)
        self.assertIn('Log.i("PacerProbeResult"', source)
        self.assertIn("checkUriPermission", source)
        self.assertIn("recipient_key=", source)
        self.assertIn("setContentDescription(\"selection_readback\")", source)
        self.assertIn("pacer_test_launch_picker", source)
        self.assertIn("onPostResume", source)
        self.assertIn("launchPickerOnResume", source)
        on_post_resume = source.index("protected void onPostResume")
        self.assertNotIn("pickerButton.performClick", source[:on_post_resume])
        self.assertIn("pickerButton.performClick", source[on_post_resume:])
        self.assertIn("getBooleanExtra", source)
        launch_start = source.index("private void launchContactPicker")
        clear_old = source.index("awaiting new picker result", launch_start)
        launch_intent = source.index("startActivityForResult", launch_start)
        self.assertLess(clear_old, launch_intent)
        self.assertNotIn("requestPermissions(", source)
        self.assertNotIn("startActivity(new Intent(Intent.ACTION_SEND", source)

    def test_picker_item_click_harness_uses_androidx_instrumentation_without_permissions(self) -> None:
        module = "pickerharness"
        manifest = self.manifest(module)
        build = (ROOT / module / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('applicationId = "com.android.contacts"', build)
        self.assertIn('testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"', build)
        self.assertIn('androidTestImplementation("androidx.test:runner:', build)
        self.assertNotIn('androidx.test.ext:junit', build)
        gradle_properties = (ROOT / "gradle.properties").read_text(encoding="utf-8")
        self.assertIn("android.useAndroidX=true", gradle_properties)
        self.assertNotIn("android.test.", build)
        self.assertEqual(manifest.findall("uses-permission"), [])
        self.assertIsNone(manifest.find("instrumentation"))
        android_test_manifest = ET.parse(
            ROOT / module / "src/androidTest/AndroidManifest.xml").getroot()
        self.assertEqual(android_test_manifest.findall("uses-permission"), [])
        instrumentation = android_test_manifest.find("instrumentation")
        self.assertIsNotNone(instrumentation)
        self.assertEqual(attr(instrumentation, "targetPackage"), "com.android.contacts")
        self.assertEqual(attr(instrumentation, "name"), "androidx.test.runner.AndroidJUnitRunner")
        self.assertEqual(instrumentation.get("{http://schemas.android.com/tools}node"), "replace")
        launcher = android_test_manifest.find("application/activity")
        self.assertIsNotNone(launcher)
        self.assertEqual(attr(launcher, "name"), "com.pacer.test.pickerharness.ProbeLaunchActivity")
        self.assertEqual(attr(launcher, "exported"), "true")
        self.assertIsNone(launcher.find("intent-filter"))
        self.assertIsNone(launcher.get("{http://schemas.android.com/apk/res/android}launchMode"))
        self.assertIsNone(launcher.get("{http://schemas.android.com/apk/res/android}taskAffinity"))
        launcher_source = (ROOT / module / "src/androidTest/java/com/pacer/test/pickerharness/"
                           "ProbeLaunchActivity.java").read_text(encoding="utf-8")
        self.assertIn("startActivityForResult(probe, PROBE_REQUEST_CODE)", launcher_source)
        self.assertNotIn("FLAG_ACTIVITY_NEW_TASK", launcher_source)
        harness = (ROOT / module / "src/androidTest/java/com/pacer/test/pickerharness/"
                   "PhonePickerItemClickTest.java").read_text(encoding="utf-8")
        self.assertIn("@RunWith(JUnit4.class)", harness)
        self.assertIn("org.junit.runners.JUnit4", harness)
        self.assertIn("androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry", harness)
        self.assertIn("getActivitiesInStage(Stage.RESUMED)", harness)
        self.assertIn("runOnMainSync(() -> pickerState[0] = findResumedPicker(lifecycleMonitor))", harness)
        self.assertIn("did not reach RESUMED", harness)
        self.assertIn("!list.isAttachedToWindow() || !list.isShown()", harness)
        self.assertIn("!row.isAttachedToWindow() || !row.isShown()", harness)
        self.assertIn("Looper.myLooper() != Looper.getMainLooper()", harness)
        self.assertIn("list.getOnItemClickListener() == null", harness)
        self.assertIn("list.getHeaderViewsCount()", harness)
        self.assertIn("matchPosition < headerCount", harness)
        self.assertIn("originalListener.onItemClick", harness)
        self.assertIn("list.setOnItemClickListener(traceListener)", harness)
        self.assertIn("instrumentation.sendStatus(0, trace)", harness)
        self.assertIn('getDeclaredField("mResultCode")', harness)
        self.assertIn('getDeclaredField("mResultData")', harness)
        self.assertIn("getCallingActivity()", harness)
        self.assertIn("describeActivityLaunchContext(picker)", harness)
        self.assertIn("launchFlags{newTask=", harness)
        self.assertLess(
            harness.index("String launchContext = describeActivityLaunchContext(picker);"),
            harness.index("originalListener.onItemClick(parent, clickedView, position, id);"),
        )
        self.assertIn("FLAG_ACTIVITY_NEW_TASK", harness)
        self.assertIn('getMethod("getDataUri", int.class)', harness)
        self.assertIn("contactDataUri=", harness)
        self.assertIn("FLAG_GRANT_READ_URI_PERMISSION", harness)
        self.assertIn("idCol=", harness)
        self.assertIn("nameCol=", harness)
        self.assertIn("idMatch=", harness)
        self.assertIn("phoneCol=", harness)
        self.assertIn("phoneMatch=", harness)
        self.assertIn('DISPLAY_NAME_ARGUMENT = "fixture_display_name_b64"', harness)
        self.assertIn('PHONE_NUMBER_ARGUMENT = "fixture_phone_b64"', harness)
        self.assertIn("normalizePhoneNumber", harness)
        self.assertIn("phoneNumberMatched", harness)
        self.assertIn("performItemClick", harness)
        self.assertIn("getItemIdAtPosition", harness)
        self.assertIn("describePickerViews", harness)
        self.assertIn("android.R.id.list missing", harness)
        self.assertIn("PICKER_VIEW_DIAGNOSTIC_BEGIN", harness)
        self.assertIn("Base64.decode", harness)
        self.assertNotIn("android.test.", harness)
        self.assertNotIn("input tap", harness)
        runner_script = (ROOT.parent / "scripts/run_live_avd_selection.py").read_text(encoding="utf-8")
        self.assertIn("contacts_trace_", runner_script)
        self.assertIn('"PacerProbeResult:I"', runner_script)
        self.assertIn("pickerharness-debug-androidTest.apk", runner_script)
        self.assertIn("com.android.contacts.test/androidx.test.runner.AndroidJUnitRunner", runner_script)
        self.assertIn('"-e", "fixture_phone_b64", encode_instrumentation_value(fixture_phone)', runner_script)
        self.assertIn("if not instrumentation_test_passed(harness_output):", runner_script)
        self.assertIn("def instrumentation_test_passed(output: str)", runner_script)
        self.assertNotIn('"INSTRUMENTATION_CODE: -1" not in harness_output', runner_script)
        self.assertIn('"shell", "pm", "list", "packages", "com.android.contacts.test"', runner_script)
        self.assertIn("harness_installed_by_probe = False", runner_script)
        self.assertIn("harness_installed_by_probe = True", runner_script)
        workflow = (ROOT.parent / ".github/workflows/avd-selection-spike.yml").read_text(encoding="utf-8")
        self.assertIn(":pickerharness:assembleDebugAndroidTest", workflow)

    def test_seeder_has_only_write_contacts_permission_and_is_separate_package(self) -> None:
        manifest = self.manifest("seeder")
        build = (ROOT / "seeder" / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('applicationId = "com.pacer.test.seeder"', build)
        permissions = {attr(node, "name") for node in manifest.findall("uses-permission")}
        self.assertEqual(permissions, {"android.permission.WRITE_CONTACTS"})

    def test_seeder_is_fixed_fixture_only_and_has_seed_and_cleanup_paths(self) -> None:
        source = self.source("seeder", "SeederActivity.java")
        self.assertIn("ContactsContract.RawContacts.CONTENT_URI", source)
        self.assertIn("ContactsContract.Data.CONTENT_URI", source)
        self.assertIn("Manifest.permission.WRITE_CONTACTS", source)
        self.assertIn("FIXTURE_DISPLAY_NAME", source)
        self.assertIn("FIXTURE_RECIPIENT_KEY", source)
        self.assertIn("ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE", source)
        self.assertIn("ContactsContract.RawContacts.ACCOUNT_NAME", source)
        self.assertIn("ContactsContract.RawContacts.ACCOUNT_TYPE", source)
        self.assertIn("clearFixture", source)
        self.assertNotIn("AGGREGATION_MODE_DISABLED", source)
        self.assertNotIn("verifyFixtureVisible", source)
        self.assertNotIn("INTERNET", source)
        self.assertNotIn("SEND_SMS", source)

    def test_avd_workflow_pins_aosp_before_build_and_runs_live_probe(self) -> None:
        root = ROOT.parent
        workflow = (root / ".github" / "workflows" / "avd-selection-spike.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("system-images;android-35;default;x86_64", workflow)
        self.assertIn("test -c /dev/kvm", workflow)
        self.assertIn("KVM before access setup", workflow)
        self.assertIn("sudo chgrp kvm /dev/kvm", workflow)
        self.assertIn("sudo chmod 0666 /dev/kvm", workflow)
        self.assertIn('sudo -u "$RUNNER_USER" test -r /dev/kvm', workflow)
        self.assertIn('sudo -u "$RUNNER_USER" test -w /dev/kvm', workflow)
        self.assertIn("sudo -u pacer-avd test -r /dev/kvm", workflow)
        self.assertIn("sudo -u pacer-avd test -w /dev/kvm", workflow)
        self.assertNotIn("sg kvm", workflow)
        self.assertNotIn("sudo -u pacer-avd -g", workflow)
        self.assertNotIn("sudo -u pacer-avd -G", workflow)
        self.assertNotIn('sudo -u "$RUNNER_USER" -g', workflow)
        self.assertIn('sudo chmod -R a+rx "$ANDROID_SDK_ROOT"', workflow)
        self.assertIn("/home/runner/.android", workflow)
        self.assertIn("-accel on", workflow)
        self.assertIn("pacer-avd", workflow)
        self.assertIn("ip6tables", workflow)
        self.assertLess(workflow.index("Inspect and pin exact AOSP picker identity"),
                        workflow.index("Build probe, seeder, and picker harness APKs"))
        self.assertIn("PIN_MODE: ${{ github.event.inputs.pin_mode || 'verify' }}", workflow)
        self.assertIn("default: discover", workflow)
        self.assertIn("AOSP_BUILD_FINGERPRINT", (root / "scripts" / "pin_aosp_picker.py").read_text(encoding="utf-8"))
        self.assertIn("PIN_DISCOVERY_REQUIRED", (root / "scripts" / "pin_aosp_picker.py").read_text(encoding="utf-8"))
        self.assertIn("aosp-picker-identity.json", workflow)
        self.assertIn("run_live_avd_selection.py", workflow)
        self.assertIn('tee "$RUNNER_TEMP/avd-logs/live-selection.log"', workflow)
        self.assertIn("Capture focused logcat on failure", workflow)
        self.assertIn('adb -s emulator-5554 logcat -d -v threadtime', workflow)
        self.assertIn('failure-logcat.txt', workflow)
        self.assertIn("live-selection.log", workflow)
        self.assertIn("failure-logcat.txt", workflow)
        self.assertIn(":pickerharness:assembleDebugAndroidTest", workflow)
        self.assertIn(":pickerharness:assembleDebugAndroidTest", (root / "avd-selection-spike.txt").read_text(encoding="utf-8"))
        self.assertIn("Refresh ADB transport before picker inspection", workflow)
        self.assertIn("adb -s emulator-5554 get-state", workflow)
        self.assertIn("sys.boot_completed", workflow)
        self.assertEqual(workflow, (root / "avd-selection-spike.txt").read_text(encoding="utf-8"))
        self.assertTrue((root / "scripts" / "pin_aosp_picker.py").is_file())
        self.assertTrue((root / "scripts" / "run_live_avd_selection.py").is_file())

    def test_adapter_contract_is_exact_serial_and_selection_only(self) -> None:
        contract = (ROOT.parent / "docs" / "adb-selection-adapter-contract.md").read_text(encoding="utf-8")
        self.assertIn("emulator-5554", contract)
        self.assertIn("5554", contract)
        self.assertIn("5555", contract)
        self.assertIn("arbitrary shell", contract.lower())
        self.assertIn("no message dispatch", contract.lower())


if __name__ == "__main__":
    unittest.main()
