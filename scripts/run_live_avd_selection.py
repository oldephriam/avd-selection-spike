from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from campaign.adb_selection_adapter import AdbSelectionAdapter, RosterBinding
from campaign.device_observer import DeviceObserver
from campaign.recipient_probe import ProbeStatus, RecipientSelectionProbe, RecipientTarget
from campaign.roster import normalize_us_number

SERIAL = "emulator-5554"
PROBE = "com.pacer.test.probe"
SEEDER = "com.pacer.test.seeder"
PICKER = "com.android.contacts"
SNAPSHOT = "pacer-avd-synthetic-fixture-v1"


def run(args: list[str], *, text: bool = True, timeout: int = 30) -> str | bytes:
    result = subprocess.run(args, capture_output=True, text=text, timeout=timeout, check=False, shell=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({Path(args[0]).name}, status {result.returncode})")
    return result.stdout


class AVD:
    def __init__(self, adb: str):
        self.adb_path = adb
        self._dump_counter = 0

    def command(self, *args: str, text: bool = True, timeout: int = 30) -> str | bytes:
        return run([self.adb_path, "-s", SERIAL, *args], text=text, timeout=timeout)

    def foreground(self) -> str:
        report = str(self.command("shell", "dumpsys", "activity", "activities"))
        for line in report.splitlines():
            if not any(marker in line for marker in ("topResumedActivity", "mResumedActivity", "mCurrentFocus")):
                continue
            match = re.search(r"\bu\d+\s+([A-Za-z0-9_.]+)/", line)
            if match:
                return match.group(1)
        raise RuntimeError("foreground activity could not be attested")

    def hierarchy(self) -> bytes:
        self._dump_counter += 1
        remote = f"/sdcard/pacer-live-bootstrap-{self._dump_counter}.xml"
        self.command("shell", "uiautomator", "dump", remote)
        result = self.command("exec-out", "cat", remote, text=False)
        if not isinstance(result, bytes) or not result:
            raise RuntimeError("accessibility hierarchy unavailable")
        return result

    def _button_bounds(self, hierarchy: bytes, label: str) -> tuple[int, int, int, int]:
        root = ET.fromstring(hierarchy)
        parents = {child: parent for parent in root.iter() for child in parent}
        matches = []
        for node in root.iter():
            values = {
                node.attrib.get("text", "").strip().casefold(),
                node.attrib.get("content-desc", "").strip().casefold(),
            }
            if label.strip().casefold() not in values:
                continue
            if node.attrib.get("visible-to-user") == "false" or node.attrib.get("enabled") == "false":
                continue
            clickable = node
            while clickable is not None and clickable.attrib.get("clickable") != "true":
                clickable = parents.get(clickable)
            if clickable is None or clickable.attrib.get("enabled") != "true":
                continue
            bounds = clickable.attrib.get("bounds", "")
            parsed = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if parsed:
                rect = tuple(map(int, parsed.groups()))
                if rect[2] > rect[0] and rect[3] > rect[1]:
                    matches.append(rect)
        unique = set(matches)
        if len(unique) != 1:
            observed_labels = [
                value[:80]
                for node in root.iter()
                for value in (
                    node.attrib.get("text", "").strip(),
                    node.attrib.get("content-desc", "").strip(),
                )
                if value
            ][:20]
            raise RuntimeError(
                f"expected one visible enabled control labelled {label!r}; "
                f"observed accessibility labels={observed_labels!r}"
            )
        return next(iter(unique))

    def tap_label(self, package: str, label: str) -> None:
        if self.foreground() != package:
            raise RuntimeError("refusing bootstrap tap outside the expected foreground package")
        first = self.hierarchy()
        bounds = self._button_bounds(first, label)
        second = self.hierarchy()
        if first != second or self._button_bounds(second, label) != bounds:
            raise RuntimeError("bootstrap UI changed between observation and tap")
        if self.foreground() != package:
            raise RuntimeError("foreground changed before bootstrap tap")
        left, top, right, bottom = bounds
        self.command("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))

    def wait_for_text(self, phrase: str, timeout_seconds: int = 60) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_values = ""
        while time.monotonic() < deadline:
            try:
                xml = ET.fromstring(self.hierarchy())
                last_values = " ".join(
                    " ".join((node.attrib.get("text", ""), node.attrib.get("content-desc", "")))
                    for node in xml.iter()
                )
                if phrase in last_values:
                    return
            except (ET.ParseError, RuntimeError) as exc:
                last_values = f"hierarchy unavailable: {type(exc).__name__}"
            time.sleep(1)
        raise RuntimeError(
            f"expected activity status did not appear before the bounded deadline; "
            f"observed accessibility text={last_values[:500]!r}"
        )


def module_version(module: str) -> str:
    source = (ROOT / "android" / module / "build.gradle.kts").read_text(encoding="utf-8")
    name_line = next((line for line in source.splitlines() if line.strip().startswith("versionName =")), "")
    code_line = next((line for line in source.splitlines() if line.strip().startswith("versionCode =")), "")
    name = name_line.split('"')[1] if name_line.count('"') >= 2 else ""
    code = code_line.split("=", 1)[1].strip() if code_line else ""
    if not name or not code.isdigit():
        raise RuntimeError(f"{module} APK version name/code is not pinned in Gradle config")
    return f"{name}:{code}"


def cert_digest(apksigner: str, apk: Path) -> str:
    output = str(run([apksigner, "verify", "--print-certs", str(apk)]))
    match = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F:]+)", output, re.I)
    if not match:
        raise RuntimeError("built APK signing certificate could not be attested")
    digest = match.group(1).replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("built APK signing certificate digest is malformed")
    return digest


def fixture_binding() -> RosterBinding:
    source = (ROOT / "android/seeder/src/main/java/com/pacer/test/seeder/SeederActivity.java").read_text(encoding="utf-8")
    name = re.search(r'FIXTURE_DISPLAY_NAME\s*=\s*"([^"]+)"', source)
    key = re.search(r'FIXTURE_RECIPIENT_KEY\s*=\s*"([^"]+)"', source)
    if not name or not key:
        raise RuntimeError("synthetic fixture source contract is incomplete")
    return RosterBinding(SNAPSHOT, 0, normalize_us_number(key.group(1)), name.group(1))


def fixture_visible_in_provider(avd: AVD) -> bool:
    binding = fixture_binding()
    source = (ROOT / "android/seeder/src/main/java/com/pacer/test/seeder/SeederActivity.java").read_text(encoding="utf-8")
    phone = re.search(r'FIXTURE_RECIPIENT_KEY\s*=\s*"([^"]+)"', source)
    if not phone:
        raise RuntimeError("synthetic fixture phone value is missing")

    def query(path: str) -> str:
        return str(avd.command(
            "shell", "content", "query",
            "--uri", f"content://com.android.contacts/{path}",
        ))

    phone_rows = query("data/phones")
    raw_rows = query("raw_contacts")
    contact_rows = query("contacts")
    data_matches = (
        binding.display_name in phone_rows
        and phone.group(1) in phone_rows
        and "vnd.android.cursor.item/phone" in phone_rows
    )
    data_raw_id = re.search(r"\braw_contact_id=(\d+)\b", phone_rows)
    data_contact_id = re.search(r"\bcontact_id=(\d+)\b", phone_rows)
    if not data_matches or not data_raw_id or not data_contact_id:
        return False

    raw_row = next((line for line in raw_rows.splitlines()
                    if re.search(rf"\b_id={re.escape(data_raw_id.group(1))}\b", line)), "")
    raw_contact_id = re.search(r"\bcontact_id=(\d+)\b", raw_row)
    raw_mode = re.search(r"\baggregation_mode=(\d+)\b", raw_row)
    raw_account_name = re.search(r"\baccount_name=([^ ,]+)", raw_row, re.I)
    raw_account_type = re.search(r"\baccount_type=([^ ,]+)", raw_row, re.I)
    aggregated = (
        bool(raw_contact_id)
        and raw_contact_id.group(1) == data_contact_id.group(1)
        and raw_mode is not None and raw_mode.group(1) == "0"
        and raw_account_name is not None and raw_account_name.group(1).casefold() == "null"
        and raw_account_type is not None and raw_account_type.group(1).casefold() == "null"
        and re.search(rf"\b_id={re.escape(data_contact_id.group(1))}\b", contact_rows) is not None
        and binding.display_name in contact_rows
    )
    return aggregated


def wait_for_fixture_provider_visibility(avd: AVD, timeout_seconds: int = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if fixture_visible_in_provider(avd):
            return
        time.sleep(0.5)
    raise RuntimeError(
        "Contacts provider did not expose a joined fixture row: phone Data row, "
        "RawContacts account/aggregation linkage, or aggregated Contacts row missing"
    )


def main() -> int:
    sdk = Path(os.environ["ANDROID_SDK_ROOT"])
    adb_path = str(sdk / "platform-tools" / "adb")
    apksigner = str(sdk / "build-tools" / "35.0.0" / "apksigner")
    pin_path = Path(os.environ["AOSP_PICKER_PIN_FILE"])
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    if pin.get("pickerPackage") != PICKER or pin.get("serial") != SERIAL:
        raise RuntimeError("live probe pin file does not match the required package/serial")

    probe_apk = ROOT / "android/probe/build/outputs/apk/debug/probe-debug.apk"
    seeder_apk = ROOT / "android/seeder/build/outputs/apk/debug/seeder-debug.apk"
    if not probe_apk.is_file() or not seeder_apk.is_file():
        raise RuntimeError("debug APK artifacts are missing")

    avd = AVD(adb_path)
    devices = str(run([adb_path, "devices", "-l"])).splitlines()
    serials = [line.split()[0] for line in devices[1:] if len(line.split()) >= 2]
    if serials != [SERIAL]:
        raise RuntimeError("live probe requires exactly the approved emulator-5554 ADB device")

    binding = fixture_binding()
    try:
        avd.command("install", "-r", str(seeder_apk), timeout=120)
        avd.command("shell", "pm", "grant", SEEDER, "android.permission.WRITE_CONTACTS")
        avd.command("shell", "am", "start", "-W", "-n", f"{SEEDER}/.SeederActivity", timeout=30)
        avd.tap_label(SEEDER, "Seed synthetic contact")
        avd.wait_for_text("SEEDED: one synthetic contact is ready")
        wait_for_fixture_provider_visibility(avd)
        print("Provider verified: phone Data row links to a default-aggregated local RawContact and Contacts row.")
        avd.command("uninstall", SEEDER, timeout=60)
        installed = str(avd.command("shell", "pm", "list", "packages", SEEDER)).strip()
        if installed:
            raise RuntimeError("temporary seeder remained installed after fixture setup")

        avd.command("install", "-r", str(probe_apk), timeout=120)
        avd.command("shell", "am", "start", "-W", "-n", f"{PROBE}/.ProbeActivity", timeout=30)
        avd.tap_label(PROBE, "Choose one contact")
        deadline = time.monotonic() + 30
        foreground = ""
        while time.monotonic() < deadline:
            try:
                foreground = avd.foreground()
            except RuntimeError:
                foreground = ""
            if foreground == PICKER:
                break
            time.sleep(0.5)
        if foreground != PICKER:
            raise RuntimeError("platform picker did not become the foreground package")

        probe_version = module_version("probe")
        probe_cert = cert_digest(apksigner, probe_apk)
        adapter = AdbSelectionAdapter(
            adb_path, apksigner, picker_package=PICKER, bindings=(binding,), timeout_seconds=20,
        )
        observer = DeviceObserver(
            adapter,
            expected_serial=SERIAL,
            expected_fingerprint=pin["buildFingerprint"],
            expected_package=PROBE,
            expected_package_version=probe_version,
            expected_certificate_sha256=probe_cert,
            expected_picker_package=PICKER,
            expected_picker_package_version=pin["pickerVersion"],
            expected_picker_certificate_sha256=pin["pickerCertificateSha256"],
            max_polls=4,
            poll_interval_seconds=1,
        )
        result = RecipientSelectionProbe(observer).select(
            RecipientTarget(binding.snapshot_id, binding.roster_index, binding.recipient_key)
        )
        if result.status is ProbeStatus.NOT_FOUND:
            frame = adapter.capture(SERIAL)
            labels = [re.sub(r"\d", "", label)[:80] for label in frame.active_control_labels[:20]]
            print(
                f"Picker diagnostic: surfaces={frame.surface_packages!r}, "
                f"candidateCount={len(frame.candidates)}, controls={labels!r}"
            )
        if result.status is not ProbeStatus.BOUND or result.evidence is None:
            raise RuntimeError(f"live selection probe failed closed: {result.status.value}: {result.detail}")
        print("Live selection probe BOUND; returned probe surface/readback and pinned identities verified.")
        print(f"Evidence SHA-256: screenshot={result.evidence.screenshot_sha256}, hierarchy={result.evidence.hierarchy_sha256}")
        return 0
    finally:
        remaining = str(avd.command("shell", "pm", "list", "packages", SEEDER)).strip()
        if remaining:
            avd.command("uninstall", SEEDER, timeout=60)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError, ValueError, ET.ParseError) as exc:
        print(f"Live AVD selection probe failed closed: {exc}")
        raise SystemExit(1)
