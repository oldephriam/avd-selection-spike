from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

SERIAL = "emulator-5554"
PACKAGE = "com.android.contacts"
SYSTEM_IMAGE = "system-images;android-35;default;x86_64"


def run(args: list[str], *, text: bool = True, timeout: int = 30) -> str | bytes:
    result = subprocess.run(args, capture_output=True, text=text, timeout=timeout, check=False, shell=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({Path(args[0]).name}, status {result.returncode})")
    return result.stdout


def adb(adb_path: str, *args: str, text: bool = True) -> str | bytes:
    return run([adb_path, "-s", SERIAL, *args], text=text)


def discover(adb_path: str, apksigner_path: str) -> dict[str, str]:
    devices = run([adb_path, "devices", "-l"]).splitlines()
    serials = [line.split()[0] for line in devices[1:] if len(line.split()) >= 2]
    states = {line.split()[0]: line.split()[1] for line in devices[1:] if len(line.split()) >= 2}
    if serials != [SERIAL] or states.get(SERIAL) != "device":
        raise RuntimeError("ADB device set is not exactly the booted emulator-5554")

    fingerprint = str(adb(adb_path, "shell", "getprop", "ro.build.fingerprint")).strip()
    sdk_level = str(adb(adb_path, "shell", "getprop", "ro.build.version.sdk")).strip()
    cpu_abi = str(adb(adb_path, "shell", "getprop", "ro.product.cpu.abi")).strip()
    if not fingerprint or sdk_level != "35" or cpu_abi != "x86_64":
        raise RuntimeError("emulator fingerprint/API level/CPU ABI is missing or unexpected")

    package_list = str(adb(adb_path, "shell", "pm", "list", "packages", PACKAGE)).splitlines()
    if package_list != [f"package:{PACKAGE}"]:
        raise RuntimeError("the pinned com.android.contacts package is not installed exactly once")

    package_dump = str(adb(adb_path, "shell", "dumpsys", "package", PACKAGE))
    package_header = re.search(rf"Package \[{re.escape(PACKAGE)}\]", package_dump)
    version_name = re.search(r"\bversionName=([^\s]+)", package_dump)
    version_code = re.search(r"\bversionCode=(\d+)", package_dump)
    if not package_header or not version_name or not version_code:
        raise RuntimeError("picker package/version evidence is incomplete")

    path_lines = str(adb(adb_path, "shell", "pm", "path", PACKAGE)).splitlines()
    apk_paths = [line.removeprefix("package:").strip() for line in path_lines if line.startswith("package:")]
    base_apk = next((path for path in apk_paths if PurePosixPath(path).name == "base.apk"), None)
    apk_path = base_apk or (apk_paths[0] if len(apk_paths) == 1 else None)
    if not apk_path or not apk_path.endswith(".apk") or ".." in PurePosixPath(apk_path).parts:
        raise RuntimeError("picker APK path is missing or ambiguous")
    if not apk_path.startswith(("/data/app/", "/system/", "/system_ext/", "/product/")):
        raise RuntimeError("picker APK path is outside approved Android package locations")

    with tempfile.TemporaryDirectory(prefix="pacer-picker-pin-") as temp:
        local_apk = Path(temp) / "contacts.apk"
        adb(adb_path, "pull", apk_path, str(local_apk))
        signer = str(run([apksigner_path, "verify", "--print-certs", str(local_apk)]))
    cert_match = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F:]+)", signer, re.I)
    if not cert_match:
        raise RuntimeError("picker signing certificate evidence is missing")
    certificate = cert_match.group(1).replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", certificate):
        raise RuntimeError("picker signing certificate digest is malformed")

    resolver = str(adb(
        adb_path, "shell", "cmd", "package", "resolve-activity", "--brief",
        "-a", "android.intent.action.PICK", "-d", "content://com.android.contacts/data/phones",
    ))
    resolved = next((line.strip() for line in resolver.splitlines() if "/" in line and PACKAGE in line), "")
    if not resolved.startswith(PACKAGE + "/"):
        raise RuntimeError("phone contact-picker intent does not resolve to com.android.contacts")

    installed = str(adb(adb_path, "shell", "pm", "list", "packages"))
    forbidden = ("com.google.android.gms", "com.google.android.gsf", "com.google.android.play.games", "com.android.vending")
    if any(re.search(rf"^package:{re.escape(name)}$", installed, re.M) for name in forbidden):
        raise RuntimeError("Google services package detected; this is not the required AOSP image")

    return {
        "systemImage": SYSTEM_IMAGE,
        "apiLevel": sdk_level,
        "cpuAbi": cpu_abi,
        "buildFingerprint": fingerprint,
        "pickerPackage": PACKAGE,
        "pickerVersionName": version_name.group(1),
        "pickerVersionCode": version_code.group(1),
        "pickerVersion": f"{version_name.group(1)}:{version_code.group(1)}",
        "pickerCertificateSha256": certificate,
        "resolvedPickerActivity": resolved,
        "serial": SERIAL,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--apksigner", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("discover", "verify"), required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        identity = discover(args.adb, args.apksigner)
        output.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
        if args.mode == "discover":
            print("Picker identity discovered and written to the workflow artifact; verification/build intentionally skipped.")
            verified = False
        else:
            expected = {
                "buildFingerprint": os.environ.get("AOSP_35_BUILD_FINGERPRINT", "").strip(),
                "pickerVersionName": os.environ.get("AOSP_CONTACTS_VERSION_NAME", "").strip(),
                "pickerVersionCode": os.environ.get("AOSP_CONTACTS_VERSION_CODE", "").strip(),
                "pickerCertificateSha256": os.environ.get("AOSP_CONTACTS_CERT_SHA256", "").strip().lower(),
            }
            if any(not value for value in expected.values()):
                raise RuntimeError("repository pin variables are incomplete; run discover, set pins, then verify")
            if any(identity[key] != value for key, value in expected.items()):
                raise RuntimeError("observed AOSP build/picker identity differs from configured repository pins")
            verified = True
            print("AOSP build and com.android.contacts version/certificate match the configured immutable pins.")

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as stream:
                stream.write(f"verified={'true' if verified else 'false'}\n")
        if verified:
            env_path = os.environ.get("GITHUB_ENV")
            if env_path:
                with open(env_path, "a", encoding="utf-8") as stream:
                    stream.write(f"AOSP_BUILD_FINGERPRINT={identity['buildFingerprint']}\n")
                    stream.write(f"AOSP_CONTACTS_VERSION={identity['pickerVersion']}\n")
                    stream.write(f"AOSP_CONTACTS_CERT_SHA256={identity['pickerCertificateSha256']}\n")
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"AOSP picker pin failed closed: {exc}")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as stream:
                stream.write("verified=false\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
