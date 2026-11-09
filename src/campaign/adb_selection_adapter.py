from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from .device_observer import ContactCandidate, DeviceFrame, DeviceIdentity, ObservationError, SelectionActionResult, UnsafeDeviceError
from .roster import RosterError, normalize_us_number

APPROVED_SERIAL = "emulator-5554"
PROBE_PACKAGE = "com.pacer.test.probe"
SEEDER_PACKAGE = "com.pacer.test.seeder"
HIERARCHY_PATH_PREFIX = "/sdcard/pacer-selection-window-"


@dataclass(frozen=True, slots=True)
class RosterBinding:
    snapshot_id: str
    roster_index: int
    recipient_key: str
    display_name: str

    def __post_init__(self) -> None:
        if not self.snapshot_id or self.roster_index < 0 or not self.display_name:
            raise ValueError("roster binding is incomplete")
        if normalize_us_number(self.recipient_key) != self.recipient_key:
            raise ValueError("roster binding key must already be normalized")


@dataclass(frozen=True, slots=True)
class _CandidateHandle:
    candidate: ContactCandidate
    bounds: tuple[int, int, int, int]
    layout_signature: str
    session_id: str


class AdbSelectionAdapter:
    """Narrow ADB port for the pinned, non-sending contact-picker spike."""

    def __init__(
        self,
        adb_path: str | Path,
        apksigner_path: str | Path,
        *,
        picker_package: str,
        bindings: Sequence[RosterBinding],
        process_runner: Callable = subprocess.run,
        timeout_seconds: float = 8.0,
    ) -> None:
        if (not re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9_]*\.)+[A-Za-z][A-Za-z0-9_]*", picker_package)
                or picker_package == PROBE_PACKAGE):
            raise ValueError("picker package pin is not a valid Android package name")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("ADB/tool timeout must be between 0 and 30 seconds")
        binding_map: dict[str, RosterBinding] = {}
        for binding in bindings:
            if binding.recipient_key in binding_map:
                raise ValueError("duplicate normalized recipient key in adapter roster bindings")
            binding_map[binding.recipient_key] = binding
        if not binding_map:
            raise ValueError("at least one immutable roster binding is required")

        self._adb_path = str(Path(adb_path))
        self._apksigner_path = str(Path(apksigner_path))
        self._picker_package = picker_package
        self._bindings: Mapping[str, RosterBinding] = binding_map
        self._process_runner = process_runner
        self._timeout_seconds = timeout_seconds
        self._device_states: dict[str, str] = {}
        self._capture_counter = 0
        self._latest_capture_id: str | None = None
        self._latest_handles: dict[str, _CandidateHandle] = {}
        self._pending_selection: _CandidateHandle | None = None
        self._last_identity: DeviceIdentity | None = None

    def list_serials(self) -> tuple[str, ...]:
        output = self._invoke(self._adb_path, ("devices", "-l"), text=True)
        serials: list[str] = []
        states: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0] == "List":
                continue
            serials.append(fields[0])
            state = " ".join(fields[1:3]) if fields[1] == "no" else fields[1]
            states[fields[0]] = state
        self._device_states = states
        return tuple(serials)

    def inspect_device(self, serial: str) -> DeviceIdentity:
        if serial != APPROVED_SERIAL:
            raise UnsafeDeviceError("adapter refuses any serial except emulator-5554")
        self._require_only_approved_device()
        fingerprint = self._adb_text("shell", "getprop", "ro.build.fingerprint").strip()
        if not fingerprint:
            raise ObservationError("device fingerprint is missing")

        probe_version, probe_cert, requested, granted = self._package_attestation(PROBE_PACKAGE)
        picker_version, picker_cert, _, _ = self._package_attestation(self._picker_package)
        foreground = self._foreground_package()
        if foreground not in {PROBE_PACKAGE, self._picker_package}:
            raise UnsafeDeviceError("foreground package is outside the pinned probe/picker allowlist")
        if foreground == PROBE_PACKAGE:
            foreground_version, foreground_cert = probe_version, probe_cert
        else:
            foreground_version, foreground_cert = picker_version, picker_cert

        seeder_output = self._adb_text("shell", "pm", "list", "packages", SEEDER_PACKAGE)
        seeder_installed = any(
            line.strip() == f"package:{SEEDER_PACKAGE}" for line in seeder_output.splitlines()
        )
        identity = DeviceIdentity(
            serial=APPROVED_SERIAL,
            fingerprint=fingerprint,
            package_name=PROBE_PACKAGE,
            package_version=probe_version,
            certificate_sha256=probe_cert,
            declared_permissions=requested,
            granted_permissions=granted,
            foreground_package=foreground,
            foreground_package_version=foreground_version,
            foreground_certificate_sha256=foreground_cert,
            seeder_installed=seeder_installed,
        )
        if self._last_identity is not None and self._stable_identity(identity) != self._stable_identity(self._last_identity):
            raise UnsafeDeviceError("device or installed package identity changed during the selection session")
        self._last_identity = identity
        return identity

    def capture(self, serial: str) -> DeviceFrame:
        if serial != APPROVED_SERIAL:
            raise UnsafeDeviceError("capture refused for a non-allowlisted serial")
        frame, handles, _ = self._capture_snapshot()
        self._latest_capture_id = frame.capture_id
        self._latest_handles = handles
        return frame

    def select_candidate(self, serial: str, capture_id: str, candidate_id: str) -> SelectionActionResult:
        if serial != APPROVED_SERIAL:
            return SelectionActionResult.REJECTED
        if capture_id != self._latest_capture_id:
            return SelectionActionResult.STALE_CAPTURE
        original = self._latest_handles.get(candidate_id)
        if original is None:
            return SelectionActionResult.STALE_CAPTURE
        try:
            prior_identity = self._last_identity
            if prior_identity is None or prior_identity.foreground_package != self._picker_package:
                return SelectionActionResult.REJECTED
            identity = self.inspect_device(APPROVED_SERIAL)
            if (not self._same_foreground_identity(prior_identity, identity)
                    or identity.foreground_package != self._picker_package
                    or identity.declared_permissions or identity.granted_permissions
                    or identity.seeder_installed):
                return SelectionActionResult.REJECTED
            fresh_frame, fresh_handles, foreground = self._capture_snapshot()
            if fresh_frame.session_id != original.session_id:
                return SelectionActionResult.SESSION_CHANGED
            if foreground != self._picker_package:
                return SelectionActionResult.REJECTED
            if fresh_frame.surface_packages != (self._picker_package,) or fresh_frame.external_intent_leak:
                return SelectionActionResult.REJECTED
            if fresh_frame.send_surface_present:
                return SelectionActionResult.REJECTED
            if fresh_frame.layout_signature != original.layout_signature:
                return SelectionActionResult.LAYOUT_CHANGED
            fresh = fresh_handles.get(candidate_id)
            if fresh is None or fresh.candidate.recipient_key != original.candidate.recipient_key:
                return SelectionActionResult.STALE_CAPTURE
            final_identity = self.inspect_device(APPROVED_SERIAL)
            if not self._same_foreground_identity(identity, final_identity):
                return SelectionActionResult.REJECTED
            left, top, right, bottom = fresh.bounds
            x = left + (right - left) // 2
            y = top + (bottom - top) // 2
            if x < 0 or y < 0 or right <= left or bottom <= top:
                return SelectionActionResult.REJECTED
            self._adb_text("shell", "input", "tap", str(x), str(y))
            self._pending_selection = fresh
            return SelectionActionResult.SELECTED
        except (ObservationError, OSError, subprocess.SubprocessError, ValueError):
            return SelectionActionResult.REJECTED

    def _capture_snapshot(self) -> tuple[DeviceFrame, dict[str, _CandidateHandle], str]:
        self._require_only_approved_device()
        self._capture_counter += 1
        hierarchy_path = f"{HIERARCHY_PATH_PREFIX}{self._capture_counter}.xml"
        self._adb_text("shell", "uiautomator", "dump", hierarchy_path)
        hierarchy = self._adb_bytes("exec-out", "cat", hierarchy_path)
        screenshot = self._adb_bytes("exec-out", "screencap", "-p")
        screen_size = self._png_dimensions(screenshot)
        activity_report = self._adb_text("shell", "dumpsys", "activity", "activities")
        foreground = self._parse_foreground_package(activity_report)
        pid = self._adb_text("shell", "pidof", PROBE_PACKAGE).strip().split()
        if not pid or not pid[0].isdigit():
            raise ObservationError("probe process/session ID is unavailable")
        try:
            root = ET.fromstring(hierarchy)
        except ET.ParseError as exc:
            raise ObservationError("accessibility hierarchy is malformed") from exc

        layout_signature = hashlib.sha256(
            hierarchy + f"|{screen_size[0]}x{screen_size[1]}".encode("ascii")
        ).hexdigest()
        capture_id = f"capture-{self._capture_counter}-{layout_signature[:16]}"
        session_id = f"{APPROVED_SERIAL}:{pid[0]}"
        packages = tuple(sorted({node.attrib.get("package", "") for node in root.iter()
                                 if node.attrib.get("package")}))
        root_package = next((node.attrib.get("package") for node in root.iter()
                             if node.attrib.get("package")), "")
        handles = self._parse_candidates(root, layout_signature, session_id, screen_size)
        candidates = tuple(handle.candidate for handle in handles.values())
        parents = {child: parent for parent in root.iter() for child in parent}
        candidate_diagnostics = []
        for node in root.iter():
            matching_key = next((parsed for value in
                                 (node.attrib.get("text", ""), node.attrib.get("content-desc", ""))
                                 if (parsed := self._normalizable_key(value)) in self._bindings), None)
            if matching_key is None:
                continue
            click_node = node
            while click_node is not None and click_node.attrib.get("clickable") != "true":
                click_node = parents.get(click_node)
            bounds = self._parse_bounds(click_node.attrib.get("bounds", "")) if click_node is not None else None
            valid_bounds = bool(bounds and bounds[2] <= screen_size[0] and bounds[3] <= screen_size[1])
            visible = node.attrib.get("visible-to-user") != "false" and click_node is not None
            if visible and click_node is not None:
                visible = all(ancestor.attrib.get("visible-to-user") != "false"
                              for ancestor in self._ancestors(click_node, parents))
            chain = []
            ancestor = node
            while ancestor is not None and len(chain) < 6:
                chain.append(
                    f"{ancestor.attrib.get('class', '')}:{ancestor.attrib.get('resource-id', '')}:"
                    f"clickable={str(ancestor.attrib.get('clickable') == 'true').lower()}"
                )
                ancestor = parents.get(ancestor)
            candidate_diagnostics.append(
                "key-match:clickable={}:enabled={}:bounds={}:visible={}:hierarchy={}".format(
                    str(click_node is not None).lower(),
                    str(click_node is not None and click_node.attrib.get("enabled") == "true").lower(),
                    str(valid_bounds).lower(),
                    str(visible).lower(),
                    chain,
                )
            )
        all_text = " ".join(" ".join((node.attrib.get("text", ""),
                                      node.attrib.get("content-desc", ""))) for node in root.iter())
        labels = tuple(text for node in root.iter() if node.attrib.get("clickable") == "true"
                       for text in (node.attrib.get("text", ""), node.attrib.get("content-desc", "")) if text)
        visible_text = tuple(
            value.strip()[:120]
            for node in root.iter()
            for value in (node.attrib.get("text", ""), node.attrib.get("content-desc", ""))
            if value.strip()
        )[:80]
        send_surface = any(re.search(r"\b(send|compose|message body|sms|rcs)\b", text, re.I)
                           for text in (*labels, all_text))
        readback_visible = root_package == PROBE_PACKAGE and "selection_readback" in all_text and "BOUND CANDIDATE" in all_text
        selected = self._parse_selected_readback(root) if readback_visible else None
        external_intent_leak = foreground != root_package or len(packages) != 1

        frame = DeviceFrame(
            serial=APPROVED_SERIAL,
            capture_id=capture_id,
            session_id=session_id,
            layout_signature=layout_signature,
            captured_at=datetime.now(timezone.utc),
            candidates=candidates,
            selected=selected,
            hierarchy_present=True,
            screenshot_sha256=hashlib.sha256(screenshot).hexdigest(),
            hierarchy_sha256=hashlib.sha256(hierarchy).hexdigest(),
            send_surface_present=send_surface,
            active_control_labels=labels,
            visible_text=visible_text,
            candidate_diagnostics=tuple(candidate_diagnostics[:40]),
            surface_packages=packages,
            external_intent_leak=external_intent_leak,
            readback_visible=readback_visible and selected is not None,
        )
        return frame, handles, foreground

    def _parse_candidates(self, root: ET.Element, layout_signature: str,
                          session_id: str, screen_size: tuple[int, int]) -> dict[str, _CandidateHandle]:
        parents = {child: parent for parent in root.iter() for child in parent}
        handles: dict[str, _CandidateHandle] = {}
        for node in root.iter():
            values = (node.attrib.get("text", ""), node.attrib.get("content-desc", ""))
            key = next((parsed for value in values
                        if (parsed := self._normalizable_key(value)) in self._bindings), None)
            if key is None:
                continue
            click_node = node
            while click_node is not None and click_node.attrib.get("clickable") != "true":
                click_node = parents.get(click_node)
            if click_node is None or click_node.attrib.get("enabled") != "true":
                continue
            bounds = self._parse_bounds(click_node.attrib.get("bounds", ""))
            if (bounds is None or bounds[2] > screen_size[0] or bounds[3] > screen_size[1]
                    or node.attrib.get("visible-to-user") == "false"
                    or click_node.attrib.get("visible-to-user") == "false"
                    or any(ancestor.attrib.get("visible-to-user") == "false"
                           for ancestor in self._ancestors(click_node, parents))):
                continue
            binding = self._bindings[key]
            row_text = " ".join(desc.attrib.get("text", "") for desc in click_node.iter()
                                if desc.attrib.get("text"))
            material = "|".join((click_node.attrib.get("resource-id", ""), str(bounds), key, row_text))
            candidate_id = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
            candidate = ContactCandidate(candidate_id, binding.roster_index, binding.recipient_key,
                                         binding.display_name, binding.snapshot_id, True)
            handles[candidate_id] = _CandidateHandle(candidate, bounds, layout_signature, session_id)
        return handles

    def _parse_selected_readback(self, root: ET.Element) -> ContactCandidate | None:
        if self._pending_selection is None:
            return None
        text = " ".join(" ".join((node.attrib.get("text", ""),
                                  node.attrib.get("content-desc", ""))) for node in root.iter())
        key_match = re.search(r"recipient_key=(\+?[0-9]+)", text)
        row_match = re.search(r"contact_row_id=([A-Za-z0-9_-]+)", text)
        display_match = re.search(r"display_name=(.*?)recipient_key=", text, re.S)
        display_name = display_match.group(1).strip() if display_match else None
        if not key_match or not row_match or not display_name:
            return None
        key = self._normalizable_key(key_match.group(1))
        if key not in self._bindings:
            return None
        binding = self._bindings[key]
        if display_name != binding.display_name:
            return None
        return ContactCandidate(self._pending_selection.candidate.candidate_id,
                                binding.roster_index, key, display_name,
                                binding.snapshot_id, True)

    @staticmethod
    def _normalizable_key(value: str) -> str | None:
        try:
            return normalize_us_number(value)
        except RosterError:
            return None

    @staticmethod
    def _png_dimensions(image: bytes) -> tuple[int, int]:
        if len(image) < 24 or image[:8] != b"\x89PNG\r\n\x1a\n" or image[12:16] != b"IHDR":
            raise ObservationError("screenshot is not a valid PNG with an IHDR")
        width, height = struct.unpack(">II", image[16:24])
        if not (1 <= width <= 16384 and 1 <= height <= 16384):
            raise ObservationError("screenshot dimensions are invalid")
        return width, height

    @staticmethod
    def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
        if not match:
            return None
        left, top, right, bottom = map(int, match.groups())
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    @staticmethod
    def _ancestors(node: ET.Element, parents: Mapping[ET.Element, ET.Element]):
        current = node
        while current in parents:
            current = parents[current]
            yield current

    def _package_attestation(self, package: str) -> tuple[str, str, frozenset[str], frozenset[str]]:
        dump = self._adb_text("shell", "dumpsys", "package", package)
        version_match = re.search(r"\bversionName=([^\s]+)", dump)
        version_code_match = re.search(r"\bversionCode=(\d+)", dump)
        if not version_match or not version_code_match:
            raise ObservationError(f"package version name/code missing for {package}")
        requested_match = re.search(
            r"requested permissions:\s*(.*?)(?=\n\s*(?:install permissions:|runtime permissions:|User \d+:)|\Z)",
            dump, re.S | re.I)
        if requested_match is None:
            # AOSP omits this heading when ParsedPackage has no requested permissions;
            # require a later package section to rule out a truncated dump.
            if not re.search(r"(?m)^\s*(?:install permissions:|User \d+:)", dump, re.I):
                raise ObservationError(f"requested-permission evidence missing for {package}")
            requested = frozenset()
        else:
            requested = frozenset(
                re.findall(r"android\.permission\.[A-Z0-9_.]+", requested_match.group(1))
            )
        granted = frozenset(name for name, value in re.findall(
            r"(android\.permission\.[A-Z0-9_.]+):\s*granted=(true|false)", dump, re.I)
            if value.lower() == "true")
        return (f"{version_match.group(1)}:{version_code_match.group(1)}",
                self._package_certificate_sha256(package), requested, granted)

    def _package_certificate_sha256(self, package: str) -> str:
        path_output = self._adb_text("shell", "pm", "path", package)
        remote_apk = next((line.removeprefix("package:").strip() for line in path_output.splitlines()
                           if line.startswith("package:")), "")
        pure_path = PurePosixPath(remote_apk)
        if (not remote_apk.endswith(".apk") or ".." in pure_path.parts
                or not remote_apk.startswith(("/data/app/", "/system/", "/system_ext/", "/product/"))):
            raise ObservationError(f"package APK path is not approved for {package}")
        with tempfile.TemporaryDirectory(prefix="pacer-adb-attest-") as directory:
            local_apk = Path(directory) / f"{package}.apk"
            self._adb_text("pull", remote_apk, str(local_apk))
            if not local_apk.is_file() or local_apk.stat().st_size == 0:
                raise ObservationError(f"package APK could not be read for {package}")
            output = self._invoke(self._apksigner_path,
                                  ("verify", "--print-certs", str(local_apk)), text=True)
        match = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F:]+)", output, re.I)
        if not match:
            raise ObservationError(f"signing-certificate evidence missing for {package}")
        digest = match.group(1).replace(":", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ObservationError(f"signing-certificate digest malformed for {package}")
        return digest

    def _foreground_package(self) -> str:
        return self._parse_foreground_package(
            self._adb_text("shell", "dumpsys", "activity", "activities"))

    @staticmethod
    def _parse_foreground_package(report: str) -> str:
        for line in report.splitlines():
            if not any(marker in line for marker in ("topResumedActivity", "mResumedActivity", "mCurrentFocus")):
                continue
            match = re.search(r"\bu\d+\s+([A-Za-z0-9_.]+)/", line)
            if match:
                return match.group(1)
        raise ObservationError("foreground activity package could not be attested")

    @staticmethod
    def _same_foreground_identity(left: DeviceIdentity, right: DeviceIdentity) -> bool:
        return (
            AdbSelectionAdapter._stable_identity(left) == AdbSelectionAdapter._stable_identity(right)
            and left.foreground_package == right.foreground_package
            and left.foreground_package_version == right.foreground_package_version
            and left.foreground_certificate_sha256 == right.foreground_certificate_sha256
        )

    @staticmethod
    def _stable_identity(identity: DeviceIdentity) -> tuple:
        return (identity.serial, identity.fingerprint, identity.package_name,
                identity.package_version, identity.certificate_sha256,
                identity.declared_permissions, identity.granted_permissions,
                identity.seeder_installed)

    def _require_only_approved_device(self) -> None:
        serials = self.list_serials()
        if serials != (APPROVED_SERIAL,):
            raise UnsafeDeviceError("ADB device set differs from the single approved serial")
        if self._device_states.get(APPROVED_SERIAL) != "device":
            raise UnsafeDeviceError("approved ADB serial is not in the connected device state")

    def _adb_text(self, *args: str) -> str:
        return self._invoke(self._adb_path, ("-s", APPROVED_SERIAL, *args), text=True)

    def _adb_bytes(self, *args: str) -> bytes:
        return self._invoke(self._adb_path, ("-s", APPROVED_SERIAL, *args), text=False)

    def _invoke(self, executable: str, args: Sequence[str], *, text: bool):
        try:
            completed = self._process_runner(
                [executable, *map(str, args)], capture_output=True, text=text,
                timeout=self._timeout_seconds, check=False, shell=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ObservationError("bounded ADB/tool invocation failed") from exc
        if completed.returncode != 0:
            raise ObservationError("bounded ADB/tool invocation returned a non-zero status")
        return completed.stdout
