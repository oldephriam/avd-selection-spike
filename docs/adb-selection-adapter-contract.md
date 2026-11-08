# ADB selection-adapter contract (v1)

## Device and port binding

- Approved device serial: exactly `emulator-5554`.
- Use `adb devices -l` and require the complete device set to be exactly that one serial, in state `device`; reject unauthorized, offline, or additional entries.
- The first Android Emulator instance normally pairs console port `5554` with ADB transport port `5555`. In the ADB device list its serial is `emulator-5554`. Pin the emulator to console port `5554`; do not replace the serial allowlist with a broad `localhost:*` or USB-device rule.
- Every device-specific ADB invocation must use the literal `-s emulator-5554`. The host ADB server's local control port is separate and is not an approved device identity.

## Narrow interface

The adapter implements only the existing `SelectionOnlyPort` methods:

1. `list_serials()` — list attached ADB devices; do not connect to new devices.
2. `inspect_device(serial)` — accept only the allowlisted serial; read the pinned build fingerprint, probe and picker package versions/signing certificates, the probe manifest/granted permission sets, seeder absence, and foreground package. The implementation reads installed APKs with `adb pull` and verifies certificates with `apksigner`; unsupported output or any mismatch fails closed.
3. `capture(serial)` — collect a fresh screenshot and accessibility hierarchy; bind both hashes, session, capture ID, layout signature, candidate handles, and timestamp to that capture.
4. `select_candidate(serial, capture_id, candidate_id)` — accept only a current capture and a candidate handle found in its hierarchy; take a second fresh hierarchy/screenshot, reject session/layout/surface changes, re-attest foreground package/version/certificate, then click only the current candidate bounds. Bounds must fit within the PNG screenshot dimensions; then require an independent fresh readback.

The adapter must not expose arbitrary command execution, arbitrary shell strings, `input text`, generic `am start`, package installation/removal, message composition, SMS/RCS, submit, or dispatch operations. Any `input tap` implementation must be generated internally from a validated candidate node and current capture; callers cannot supply coordinates. Invoke subprocesses with argument arrays and `shell=False`, fixed executable path, fixed serial, bounded timeouts, and checked exit codes.

The initial activity launch and fixture installation/seeding are provisioning operations outside `SelectionOnlyPort`; they must use only the two reviewed package IDs and the dedicated fixture procedure. The probe app remains a separate zero-permission package. Remove the seeder after fixture creation and before selection.

## Package and UI identity

- Probe package: `com.pacer.test.probe`; source manifest declares no permissions, and both requested and granted permission sets must be empty at runtime.
- Seeder package: `com.pacer.test.seeder`; source manifest declares only `android.permission.WRITE_CONTACTS`; it has no network or messaging capability. The seeder must be absent before probing.
- During `PICKER_ACTIVE`, foreground identity must exactly match the configured picker package/version/certificate. The pinned version value is the exact `versionName:versionCode` pair. During `PROBE_RETURNED`, it must exactly match the probe package/version/certificate, with only the probe package surface visible and the selection readback visible.
- The probe displays `contact_row_id`, `display_name`, and normalized `recipient_key` in the accessibility readback view. Bind roster index and snapshot ID from the immutable fixture mapping; reject if the selected key is absent, duplicated, stale, or mismatched.

## Failure behavior

Any extra serial, unexpected foreground package, missing permission/build/certificate evidence, stale capture, missing hierarchy, ambiguous candidate, tap outside candidate bounds, failed picker result, or unreadable post-selection identity returns an unverified/fail-closed result. The adapter has no message-send capability, and the probe app contains no message composer or send path. There is no message dispatch operation.
