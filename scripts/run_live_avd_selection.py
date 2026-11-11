from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from campaign.adb_selection_adapter import AdbSelectionAdapter, RosterBinding
from campaign.device_observer import DeviceObserver
from campaign.roster import normalize_us_number

SERIAL = "emulator-5554"
PROBE = "com.pacer.test.probe"
SEEDER = "com.pacer.test.seeder"
PICKER = "com.android.contacts"
SNAPSHOT = "pacer-avd-synthetic-fixture-v1"
TEST_PACKAGE = "com.android.contacts.test"
PROBE_LAUNCHER = "com.pacer.test.pickerharness.ProbeLaunchActivity"
EXPECTED_PICKER_CALLER = "com.pacer.test.probe/com.pacer.test.probe.ProbeActivity"


def run(args: list[str], *, text: bool = True, timeout: int = 30, check: bool = True) -> str | bytes:
    result = subprocess.run(args, capture_output=True, text=text, timeout=timeout, check=False, shell=False)
    if result.returncode:
        if not check:
            return result.stdout + result.stderr
        raise RuntimeError(f"command failed ({Path(args[0]).name}, status {result.returncode})")
    return result.stdout


class AVD:
    def __init__(self, adb: str):
        self.adb_path = adb
        self._dump_counter = 0

    def command(self, *args: str, text: bool = True, timeout: int = 30, check: bool = True) -> str | bytes:
        return run([self.adb_path, "-s", SERIAL, *args], text=text, timeout=timeout, check=check)

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

    def selection_diagnostics(self) -> str:
        root = ET.fromstring(self.hierarchy())
        parents = {child: parent for parent in root.iter() for child in parent}
        visible_text = " ".join(
            value.strip()
            for node in root.iter()
            for value in (node.attrib.get("text", ""), node.attrib.get("content-desc", ""))
            if value.strip()
        )
        readback = "selection_readback" in visible_text
        matches = []
        for node in root.iter():
            text = " ".join((node.attrib.get("text", ""), node.attrib.get("content-desc", "")))
            if "Pacer Probe Fixture" not in text:
                continue
            chain = []
            current = node
            list_row = None
            while current is not None and len(chain) < 8:
                chain.append(current)
                if current.attrib.get("class") == "android.widget.ListView":
                    list_row = chain[-2] if len(chain) >= 2 else None
                    break
                current = parents.get(current)
            target = next((item for item in chain if item.attrib.get("clickable") == "true"), list_row)
            target_desc = "none"
            if target is not None:
                target_desc = "class={}:clickable={}:enabled={}:bounds={}".format(
                    target.attrib.get("class", ""),
                    target.attrib.get("clickable", "false"),
                    target.attrib.get("enabled", "false"),
                    target.attrib.get("bounds", ""),
                )
            chain_desc = " > ".join(
                f"{item.attrib.get('class', '')}(clickable={item.attrib.get('clickable', 'false')},"
                f"enabled={item.attrib.get('enabled', 'false')},bounds={item.attrib.get('bounds', '')})"
                for item in chain
            )
            matches.append(f"target=[{target_desc}] chain=[{chain_desc}]")
        text_excerpt = re.sub(r"\d", "", visible_text)[:500]
        return (
            f"foreground={self.foreground()} readback={readback} "
            f"fixtureRows={matches[:5]!r} visibleText={text_excerpt!r}"
        )


class FrameworkLaunchTraceCapture:
    MAX_LOGCAT_LINES = 1200

    def __init__(self, avd: AVD):
        self.avd = avd
        self.trace_started = False
        self.logcat_started = False
        self.trace_output = ""
        self.logcat_lines: list[str] = []
        self.logcat_process: subprocess.Popen[str] | None = None
        self.reader: threading.Thread | None = None

    def start(self) -> None:
        try:
            categories = str(self.avd.command("shell", "atrace", "--list_categories", timeout=10))
            if not all(re.search(rf"(?m)^\s*{category}\s+-", categories) for category in ("am", "wm")):
                raise RuntimeError("required atrace categories unavailable")
            self.avd.command("shell", "atrace", "--async_start", "-b", "4096", "am", "wm", timeout=10)
            self.trace_started = True
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self.trace_started = False

        try:
            self.logcat_process = subprocess.Popen(
                [
                    self.avd.adb_path, "-s", SERIAL, "logcat", "-v", "threadtime", "-T", "1", "-s",
                    "ActivityTaskManager:V", "ActivityManager:V",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                errors="replace",
                bufsize=1,
            )
            self.logcat_started = True
            self.reader = threading.Thread(target=self._read_logcat, daemon=True)
            self.reader.start()
        except (OSError, subprocess.SubprocessError):
            self.logcat_started = False

    def _read_logcat(self) -> None:
        process = self.logcat_process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            self.logcat_lines.append(line.rstrip("\r\n"))
            if len(self.logcat_lines) > self.MAX_LOGCAT_LINES:
                del self.logcat_lines[:len(self.logcat_lines) - self.MAX_LOGCAT_LINES]

    def stop(self) -> tuple[str, str]:
        if self.trace_started:
            try:
                self.trace_output = str(self.avd.command(
                    "shell", "atrace", "--async_stop", timeout=20, check=False,
                ))[:1_000_000]
            except (OSError, RuntimeError, subprocess.SubprocessError):
                self.trace_output = ""
        self._stop_logcat()
        capture_status = (
            "FRAMEWORK_TRACE_CAPTURE atrace=" + ("captured" if self.trace_output else "unavailable")
            + ";logcat=" + ("captured" if self.logcat_started else "unavailable")
            + ";logcatLines=" + str(len(self.logcat_lines))
        )
        return capture_status, format_framework_activity_trace(self.trace_output, self.logcat_lines)

    def _stop_logcat(self) -> None:
        process = self.logcat_process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.reader is not None:
            self.reader.join(timeout=5)


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


def enable_root_adb_for_instrumentation(avd: AVD) -> None:
    debuggable = str(avd.command("shell", "getprop", "ro.debuggable")).strip()
    if debuggable != "1":
        raise RuntimeError("cross-signature instrumentation requires the pinned debuggable AOSP image")
    root_output = str(avd.command("root", timeout=30, check=False)).casefold()
    if "cannot run as root" in root_output:
        raise RuntimeError("pinned AOSP emulator refused root adbd required for cross-signature instrumentation")
    avd.command("wait-for-device", timeout=45)
    if str(avd.command("shell", "id", "-u")).strip() != "0":
        raise RuntimeError("root adbd did not yield uid 0 for instrumentation launch")


def encode_instrumentation_value(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def encode_instrumentation_display_name(display_name: str) -> str:
    return encode_instrumentation_value(display_name)


def launch_probe_wrapper_via_host_adb(avd: AVD) -> None:
    output = str(avd.command(
        "shell", "am", "start", "-W", "-n", f"{TEST_PACKAGE}/{PROBE_LAUNCHER}",
        "--ez", "pacer_test_launch_probe", "true", timeout=30,
    ))
    if not re.search(r"(?m)^Status:\s*ok\s*$", output):
        raise RuntimeError("host-ADB Variant B launcher did not report Status: ok")


def wait_for_harness_ready(
    avd: AVD,
    launch_session: str,
    *,
    timeout_seconds: float = 20,
    poll_interval_seconds: float = 0.5,
) -> None:
    if not re.fullmatch(r"[0-9a-f]{32}", launch_session):
        raise ValueError("host-ADB launch session must be a 32-character lowercase hex token")
    deadline = time.monotonic() + timeout_seconds
    marker = f"LAUNCH_READY session={launch_session}"
    while time.monotonic() < deadline:
        output = str(avd.command("shell", "logcat", "-d", "-s", "PacerHarnessReady:I", check=False))
        if marker in output:
            return
        time.sleep(poll_interval_seconds)
    raise RuntimeError("instrumentation lifecycle monitor did not become ready before host-ADB launch")


def caller_gate_rejected(output: str) -> bool:
    if "contacts_trace_stage=caller_gate" not in output:
        return False
    caller = re.search(r"contacts_trace_actual_caller=([^\s\r\n]+)", output)
    return caller is not None and caller.group(1) != EXPECTED_PICKER_CALLER


def caller_gate_outcome(output: str) -> str:
    if "contacts_trace_stage=caller_gate" not in output:
        return "not-observed"
    caller_line = next((line for line in output.splitlines() if "contacts_trace_actual_caller=" in line), "")
    caller = caller_line.split("contacts_trace_actual_caller=", 1)[1].split()[0] if "=" in caller_line else ""
    if not caller:
        return "caller-unavailable"
    if caller == EXPECTED_PICKER_CALLER:
        return "caller-matched"
    return "caller-missing" if caller == "none" else "caller-mismatch"


def report_variant_traces(
    avd: AVD,
    output: str,
    variant: str,
    launch_trace_offset: int,
    result_trace_offset: int,
) -> tuple[int, int]:
    callback_trace = [
        line.strip() for line in output.splitlines()
        if "INSTRUMENTATION_STATUS: contacts_trace_" in line
    ]
    if callback_trace:
        print(f"Launch variant {variant}: Contacts pre-gate/callback trace:")
        print(" | ".join(callback_trace))
    launch_log = str(avd.command(
        "shell", "logcat", "-d", "-s", "PacerLaunchTrace:I", check=False,
    ))
    source_lines = [line for line in launch_log.splitlines() if "PacerLaunchTrace:" in line]
    for line in source_lines[launch_trace_offset:]:
        formatted = format_activity_launch_trace(line)
        if formatted is not None:
            print(f"Launch variant {variant}: {formatted}")
    result_log = str(avd.command(
        "shell", "logcat", "-d", "-s", "PacerProbeResult:I", check=False,
    ))
    result_lines = [line for line in result_log.splitlines() if "PacerProbeResult:" in line]
    for line in result_lines[result_trace_offset:]:
        formatted = format_probe_result_trace(line)
        if formatted is not None:
            print(f"Launch variant {variant}: {formatted}")
    return len(source_lines), len(result_lines)


def run_picker_instrumentation(
    avd: AVD,
    *,
    fixture_data_id: str,
    display_name: str,
    phone: str,
    launch_variant: str,
    framework_trace: FrameworkLaunchTraceCapture | None = None,
    launch_session: str = "",
) -> str:
    if launch_variant not in ("instrumentation", "host-adb"):
        raise ValueError("unknown hosted picker launch variant")
    owns_trace = framework_trace is None
    if framework_trace is None:
        framework_trace = FrameworkLaunchTraceCapture(avd)
    if owns_trace:
        framework_trace.start()
    try:
        command_args = [
            "shell", "am", "instrument", "-w",
            "-e", "class", "com.pacer.test.pickerharness.PhonePickerItemClickTest",
            "-e", "fixture_data_id", fixture_data_id,
            "-e", "fixture_display_name_b64", encode_instrumentation_display_name(display_name),
            "-e", "fixture_phone_b64", encode_instrumentation_value(phone),
            "-e", "launch_variant", launch_variant,
        ]
        if launch_session:
            command_args.extend(("-e", "launch_session", launch_session))
        command_args.append(f"{TEST_PACKAGE}/androidx.test.runner.AndroidJUnitRunner")
        output = str(avd.command(*command_args, timeout=120, check=False))
    finally:
        if owns_trace:
            trace_status, framework_summary = framework_trace.stop()
            print(f"Launch variant {launch_variant}: {trace_status}")
            print(f"Launch variant {launch_variant}: {framework_summary}")
    return output


def fixture_phone_number() -> str:
    source = (ROOT / "android/seeder/src/main/java/com/pacer/test/seeder/SeederActivity.java").read_text(encoding="utf-8")
    phone = re.search(r'FIXTURE_RECIPIENT_KEY[ ]*=[ ]*"([^"]+)"', source)
    if not phone:
        raise RuntimeError("synthetic fixture phone value is missing")
    return phone.group(1)


def format_activity_launch_trace(line: str) -> str | None:
    marker = "PacerLaunchTrace:"
    if marker not in line:
        return None
    message = line.split(marker, 1)[1].strip()
    if not message.startswith("ACTIVITY_LAUNCH_TRACE "):
        return None
    fields = dict(re.findall(
        r"([A-Za-z][A-Za-z0-9]*)=([^;\s]+)",
        message.removeprefix("ACTIVITY_LAUNCH_TRACE "),
    ))
    components = {
        "com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity",
        "com.pacer.test.probe/com.pacer.test.probe.ProbeActivity",
    }

    def component_value(key: str, allow_implicit: bool = False) -> str:
        value = fields.get(key, "")
        if value in components or value == "none" or (allow_implicit and value == "implicit"):
            return value
        return "other" if value else "unknown"

    def flag_value(key: str) -> str:
        value = fields.get(key, "")
        return value.lower() if re.fullmatch(r"0x[0-9a-fA-F]{1,8}", value) else "unknown"

    stage = fields.get("stage", "")
    if stage not in ("before_probe_launch", "before_picker_launch"):
        stage = "other"
    task_id = fields.get("taskId", "")
    task_id = str(int(task_id)) if re.fullmatch(r"[0-9]{1,6}", task_id) else "unknown"
    root_state = fields.get("isTaskRoot", "")
    if root_state not in ("true", "false"):
        root_state = "unknown"
    expected_request = "73" if stage == "before_probe_launch" else "41"
    request_code = fields.get("requestCode", "")
    if request_code not in ("41", "73"):
        request_code = "unknown"
    elif request_code != expected_request:
        request_code = "unexpected"
    target_action = fields.get("targetAction", "none")
    if target_action not in ("none", "android.intent.action.PICK"):
        target_action = "other"
    return (
        "ACTIVITY_LAUNCH_TRACE stage=" + stage
        + ";sourceComponent=" + component_value("sourceComponent")
        + ";targetComponent=" + component_value("targetComponent", allow_implicit=True)
        + (";targetAction=" + target_action if "targetAction" in fields else "")
        + ";taskId=" + task_id
        + ";isTaskRoot=" + root_state
        + ";callingActivity=" + component_value("callingActivity")
        + ";requestCode=" + request_code
        + ";launchIntentFlags=" + flag_value("launchIntentFlags")
        + ";incomingIntentFlags=" + flag_value("incomingIntentFlags")
    )


def format_framework_activity_trace(trace: str, logcat_lines: list[str]) -> str:
    raw = (str(trace)[:1_000_000] + "\n" + "\n".join(logcat_lines[-1200:]))[:1_000_000]
    allowed_events = {
        "am_create_activity", "am_resume_activity", "am_pause_activity", "am_finish_activity",
        "wm_create_activity", "wm_resume_activity", "wm_pause_activity", "wm_task_created",
        "wm_task_moved", "wm_finish_activity",
    }
    events = sorted(set(re.findall(r"\b(?:am|wm)_[a-z0-9_]+\b", raw)) & allowed_events)
    components = {
        "contacts": (
            "com.android.contacts/com.android.contacts.activities.ContactSelectionActivity",
            "com.android.contacts/.activities.ContactSelectionActivity",
        ),
        "probe": (
            "com.pacer.test.probe/com.pacer.test.probe.ProbeActivity",
            "com.pacer.test.probe/.ProbeActivity",
        ),
        "wrapper": (
            "com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity",
            "com.android.contacts.test/.ProbeLaunchActivity",
        ),
    }
    seen = {label: any(value in raw for value in forms) for label, forms in components.items()}
    result_to = "not-shown"
    canonical = {
        "contacts": "com.android.contacts/com.android.contacts.activities.ContactSelectionActivity",
        "probe": "com.pacer.test.probe/com.pacer.test.probe.ProbeActivity",
        "wrapper": "com.android.contacts.test/com.pacer.test.pickerharness.ProbeLaunchActivity",
    }
    pattern = r"\b(?:mResultTo|resultTo)\s*=\s*(ActivityRecord\{[^}]*\}|[^\s,]+)"
    for match in re.finditer(pattern, raw, re.I):
        for label, forms in components.items():
            if any(value in match.group(1) for value in forms):
                result_to = canonical[label]
                break
        if result_to != "not-shown":
            break
    request_codes = sorted(set(re.findall(r"\b(?:mRequestCode|requestCode)\s*=\s*(41|73)\b", raw, re.I)))
    return (
        "FRAMEWORK_ACTIVITY_TRACE events=" + (",".join(events) if events else "none")
        + ";contacts=" + ("seen" if seen["contacts"] else "not-shown")
        + ";probe=" + ("seen" if seen["probe"] else "not-shown")
        + ";wrapper=" + ("seen" if seen["wrapper"] else "not-shown")
        + ";resultTo=" + result_to
        + ";requestCodes=" + (",".join(request_codes) if request_codes else "not-shown")
        + ";logcatLines=" + str(min(len(logcat_lines), FrameworkLaunchTraceCapture.MAX_LOGCAT_LINES))
    )


def format_probe_result_trace(line: str) -> str | None:
    marker = "PacerProbeResult:"
    if marker not in line:
        return None
    message = line.split(marker, 1)[1].strip()
    if message.startswith("RESULT_TRACE "):
        payload = message.removeprefix("RESULT_TRACE ")
        fields = dict(re.findall(r"([A-Za-z][A-Za-z0-9]*)=([^;\s]+)", payload))
        result_code = {"-1": "RESULT_OK", "0": "RESULT_CANCELED"}.get(
            fields.get("resultCode", ""), "RESULT_OTHER",
        )
        request_match = fields.get("requestCode") == "41"
        data_present = fields.get("dataPresent") if fields.get("dataPresent") in ("true", "false") else "unknown"
        uri_present = fields.get("uriPresent") if fields.get("uriPresent") in ("true", "false") else "unknown"
        scheme = fields.get("scheme") if fields.get("scheme") in ("content", "none") else "other"
        authority = fields.get("authority") if fields.get("authority") in ("com.android.contacts", "none") else "other"
        read_grant = fields.get("readGrantFlag") if fields.get("readGrantFlag") in ("true", "false") else "unknown"
        uri_permission = fields.get("uriPermission") if fields.get("uriPermission") in (
            "granted", "denied", "not-checkable",
        ) else "other"
        return (
            "PROBE_RESULT_TRACE requestMatches=" + str(request_match).lower()
            + ";resultCode=" + result_code
            + ";dataPresent=" + data_present
            + ";uriPresent=" + uri_present
            + ";scheme=" + scheme
            + ";authority=" + authority
            + ";readGrantFlag=" + read_grant
            + ";uriPermission=" + uri_permission
        )
    if message.startswith("READBACK_TRACE "):
        fields = dict(re.findall(r"([A-Za-z][A-Za-z0-9]*)=([^;\s]+)", message.removeprefix("READBACK_TRACE ")))
        outcome = fields.get("outcome")
        if outcome in ("security_exception", "invalid_uri"):
            return "PROBE_READBACK_TRACE outcome=" + outcome
    return None


def readback_status(readback: str) -> str:
    if "RESULT_OK: BOUND CANDIDATE" in readback:
        return "result_ok"
    if "RESULT_CANCELED" in readback:
        return "result_canceled"
    if "RESULT_OK" in readback:
        return "result_ok_unbound"
    if "did not grant readable selection evidence" in readback:
        return "permission_denied"
    if "selected row could not be read" in readback:
        return "uri_unreadable"
    if "selected URI is invalid" in readback:
        return "uri_invalid"
    if "UNVERIFIED" in readback:
        return "unverified"
    return "status_missing"


def missing_readback_fields(
    readback: str,
    expected_fields: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    return tuple(label for label, expected in expected_fields if expected not in readback)


def instrumentation_test_passed(output: str) -> bool:
    text = str(output)
    if re.search(r"(?m)^(?:FAILURES!!!|INSTRUMENTATION_FAILED|INSTRUMENTATION_ABORTED)(?:\s|:|$)", text):
        return False
    instrumentation_code = re.search(r"(?m)^INSTRUMENTATION_CODE:\s*(-?\d+)\s*$", text)
    if instrumentation_code and instrumentation_code.group(1) != "-1":
        return False
    return re.search(r"(?m)^OK \(1 test\)\s*$", text) is not None


def sanitized_instrumentation_output(output: str, binding: RosterBinding, fixture_data_id: str) -> str:
    text = str(output)
    fixture_phone = fixture_phone_number()
    fixture_phone_b64 = encode_instrumentation_value(fixture_phone)
    for private_value in (
        binding.display_name, binding.recipient_key, fixture_data_id, fixture_phone, fixture_phone_b64,
    ):
        if private_value:
            text = text.replace(private_value, "[fixture]")
    source_lines = text.splitlines()
    selected_lines = source_lines if len(source_lines) <= 40 else source_lines[:8] + source_lines[-32:]
    lines = []
    for line in selected_lines:
        safe_line = re.sub(r"\d+", "[n]", line).strip()
        if safe_line:
            lines.append(safe_line[:240])
    return "\n".join(lines)[:8000]


def fixture_phone_data_id(avd: AVD) -> str:
    binding = fixture_binding()
    phone_number = fixture_phone_number()
    rows = str(avd.command(
        "shell", "content", "query",
        "--uri", "content://com.android.contacts/data/phones",
    ))
    matches = []
    for line in rows.splitlines():
        if binding.display_name not in line or phone_number not in line:
            continue
        if "vnd.android.cursor.item/phone" not in line:
            continue
        match = re.search("_id=([0-9]+)", line)
        if match:
            matches.append(match.group(1))
    if len(matches) != 1:
        raise RuntimeError("fixture phone Data row ID is missing or ambiguous")
    return matches[0]


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
    harness_apk = ROOT / "android/pickerharness/build/outputs/apk/androidTest/debug/pickerharness-debug-androidTest.apk"
    if not probe_apk.is_file() or not seeder_apk.is_file() or not harness_apk.is_file():
        raise RuntimeError("probe, seeder, or AndroidX picker instrumentation test APK artifact is missing")

    avd = AVD(adb_path)
    devices = str(run([adb_path, "devices", "-l"])).splitlines()
    serials = [line.split()[0] for line in devices[1:] if len(line.split()) >= 2]
    if serials != [SERIAL]:
        raise RuntimeError("live probe requires exactly the approved emulator-5554 ADB device")

    binding = fixture_binding()
    test_package = "com.android.contacts.test"
    harness_installed_by_probe = False
    try:
        avd.command("install", "-r", str(seeder_apk), timeout=120)
        avd.command("shell", "pm", "grant", SEEDER, "android.permission.WRITE_CONTACTS")
        avd.command("shell", "am", "start", "-W", "-n", f"{SEEDER}/.SeederActivity", timeout=30)
        avd.tap_label(SEEDER, "Seed synthetic contact")
        avd.wait_for_text("SEEDED: one synthetic contact is ready")
        wait_for_fixture_provider_visibility(avd)
        fixture_data_id = fixture_phone_data_id(avd)
        fixture_phone = fixture_phone_number()
        print("Provider verified: one joined phone Data row and exact fixture identifier captured.")
        avd.command("uninstall", SEEDER, timeout=60)
        installed = str(avd.command("shell", "pm", "list", "packages", SEEDER)).strip()
        if installed:
            raise RuntimeError("temporary seeder remained installed after fixture setup")

        avd.command("install", "-r", str(probe_apk), timeout=120)
        existing_test_package = str(avd.command("shell", "pm", "list", "packages", test_package)).strip()
        if existing_test_package:
            raise RuntimeError("refusing to replace a pre-existing Contacts instrumentation package")
        avd.command("install", "-r", str(harness_apk), timeout=120)
        harness_installed_by_probe = True
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
        enable_root_adb_for_instrumentation(avd)
        parser_test_output = str(avd.command(
            "shell", "am", "instrument", "-w",
            "-e", "class", "com.pacer.test.pickerharness.ActivityTaskDumpParserTest",
            "com.android.contacts.test/androidx.test.runner.AndroidJUnitRunner",
            timeout=60,
            check=False,
        ))
        if not instrumentation_test_passed(parser_test_output):
            print("ActivityTaskManager API 35 parser fixture result (sanitized):")
            for line in parser_test_output.splitlines():
                marker = "ACTIVITY_TASK_PARSER_SUMMARY "
                if marker in line:
                    print(line[line.index(marker):])
            print(sanitized_instrumentation_output(parser_test_output, binding, fixture_data_id))
            raise RuntimeError("bounded ActivityTaskManager parser fixture did not pass")
        print("ActivityTaskManager API 35 parser fixture passed.")
        launch_trace_count, result_trace_count = 0, 0

        def run_variant(
            variant: str,
            framework_trace: FrameworkLaunchTraceCapture | None = None,
            launch_wrapper_after_attach: bool = False,
        ) -> tuple[str, int, int]:
            launch_session = uuid.uuid4().hex if launch_wrapper_after_attach else ""

            def execute_instrumentation() -> None:
                try:
                    async_result["output"] = run_picker_instrumentation(
                        avd,
                        fixture_data_id=fixture_data_id,
                        display_name=binding.display_name,
                        phone=fixture_phone,
                        launch_variant=variant,
                        framework_trace=framework_trace,
                        launch_session=launch_session,
                    )
                except BaseException as failure:
                    async_result["failure"] = failure

            if launch_wrapper_after_attach:
                async_result: dict[str, object] = {}
                instrumentation_thread = threading.Thread(
                    target=execute_instrumentation, name="host-adb-picker-instrumentation", daemon=True,
                )
                instrumentation_thread.start()
                try:
                    wait_for_harness_ready(avd, launch_session)
                    launch_probe_wrapper_via_host_adb(avd)
                finally:
                    instrumentation_thread.join(timeout=40)
                if instrumentation_thread.is_alive():
                    raise RuntimeError("host-ADB instrumentation did not exit after the bounded launch wait")
                failure = async_result.get("failure")
                if isinstance(failure, BaseException):
                    raise RuntimeError("host-ADB instrumentation runner failed") from failure
                output = str(async_result.get("output", ""))
            else:
                output = run_picker_instrumentation(
                    avd,
                    fixture_data_id=fixture_data_id,
                    display_name=binding.display_name,
                    phone=fixture_phone,
                    launch_variant=variant,
                    framework_trace=framework_trace,
                )
            next_launch_count, next_result_count = report_variant_traces(
                avd, output, variant, launch_trace_count, result_trace_count,
            )
            gate_outcome = caller_gate_outcome(output)
            print(f"LAUNCH_VARIANT_RESULT variant={variant};caller={gate_outcome};"
                  f"instrumentation={'passed' if instrumentation_test_passed(output) else 'failed'}")
            if not instrumentation_test_passed(output) and not caller_gate_rejected(output):
                print(f"Launch variant {variant}: instrumentation failure (sanitized):")
                print(sanitized_instrumentation_output(output, binding, fixture_data_id))
                raise RuntimeError(f"launch variant {variant} failed before the caller gate")
            return output, next_launch_count, next_result_count

        baseline_output, launch_trace_count, result_trace_count = run_variant("instrumentation")
        for package in (PICKER, PROBE, TEST_PACKAGE):
            avd.command("shell", "am", "force-stop", package, timeout=15)
        variant_b_trace = FrameworkLaunchTraceCapture(avd)
        variant_b_trace.start()
        try:
            variant_b_output, launch_trace_count, result_trace_count = run_variant(
                "host-adb", variant_b_trace, launch_wrapper_after_attach=True,
            )
        finally:
            trace_status, framework_summary = variant_b_trace.stop()
            print(f"Launch variant host-adb: {trace_status}")
            print(f"Launch variant host-adb: {framework_summary}")
        print("LAUNCH_VARIANT_COMPARISON baseline=" + caller_gate_outcome(baseline_output)
              + ";hostAdb=" + caller_gate_outcome(variant_b_output))
        if caller_gate_rejected(variant_b_output):
            raise RuntimeError("Variant B reached the unchanged caller gate without ProbeActivity as caller")
        if not instrumentation_test_passed(variant_b_output):
            raise RuntimeError("Variant B instrumentation did not complete the unchanged picker click path")

        deadline = time.monotonic() + 30
        foreground = ""
        frame = None
        while time.monotonic() < deadline:
            try:
                foreground = avd.foreground()
                if foreground == PROBE:
                    frame = observer.capture()
                    if any("RESULT_OK: BOUND CANDIDATE" in text for text in frame.visible_text):
                        break
            except RuntimeError:
                frame = None
            time.sleep(0.5)
        if foreground != PROBE or frame is None:
            raise RuntimeError("instrumented picker did not return to the pinned probe surface")
        if frame.send_surface_present or frame.surface_packages != (PROBE,):
            raise RuntimeError("unexpected surface appeared after instrumented picker activation")
        readback = "\n".join(frame.visible_text)
        probe_result_log = str(avd.command(
            "shell", "logcat", "-d", "-s", "PacerProbeResult:I", check=False,
        ))
        probe_result_trace = []
        for line in probe_result_log.splitlines():
            formatted_trace = format_probe_result_trace(line)
            if formatted_trace is not None:
                probe_result_trace.append(formatted_trace)
        if probe_result_trace:
            print("Probe result-delivery trace:")
            print("\n".join(probe_result_trace))
        expected_fields = (
            ("status", "RESULT_OK: BOUND CANDIDATE"),
            ("contact_row_id", f"contact_row_id={fixture_data_id}"),
            ("display_name", f"display_name={binding.display_name}"),
            ("recipient_key", f"recipient_key={binding.recipient_key}"),
        )
        missing_fields = missing_readback_fields(readback, expected_fields)
        if missing_fields:
            raise RuntimeError(
                "RESULT_OK readback did not match the exact seeded fixture identity; "
                "unmatched_fields=" + ",".join(missing_fields)
                + "; readback_state=" + readback_status(readback)
            )
        print("Hosted-path instrumentation invoked ListView.performItemClick on the exact provider-bound phone row.")
        print("RESULT_OK readback matched the exact fixture row; picker and probe package pins remained verified.")
        print(f"Evidence SHA-256: screenshot={frame.screenshot_sha256}, hierarchy={frame.hierarchy_sha256}")
        return 0
    finally:
        remaining = str(avd.command("shell", "pm", "list", "packages", SEEDER)).strip()
        if remaining:
            avd.command("uninstall", SEEDER, timeout=60)
        if harness_installed_by_probe:
            harness_installed = str(avd.command("shell", "pm", "list", "packages", test_package)).strip()
            if harness_installed:
                avd.command("uninstall", test_package, timeout=60)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError, ValueError, ET.ParseError) as exc:
        print(f"Live AVD selection probe failed closed: {exc}")
        raise SystemExit(1)
